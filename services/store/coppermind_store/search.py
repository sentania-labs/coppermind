"""Full-text search over the index, with listing's filters and paging.

The question is parsed twice, by PostgreSQL's English configuration (stems,
drops stop words) and by 'simple' (every word as written, which is what a
name needs), and a document matches either. A `-word` or `-"phrase"` the
question excludes is then refused under both readings, so neither reading
lets back in what the other shuts out: `budget -runs` finds no note that
says `running`. Rank orders the answer; the
opaque cursor resumes after the last hit by rank and then by kind and
identifier, which never change, so no hit is skipped or repeated between
pages while the index stands still.

Every filter listing takes narrows the answer the same way, read from the
metadata mirror row of the note. A source projection or an artifact has no
such row, so a frontmatter filter matches notes only, while `folder` and
`state` apply to every kind, `tag` to notes and artifacts, and `kind` and
`citing_note` pick out one kind directly.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import re
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import REGCONFIG
from sqlalchemy.exc import SQLAlchemyError

from coppermind.db.models import Artifact, EntityRecord, Note, SearchDocument, SearchEmbedding
from coppermind.store_protocol import (
    FilterCursorConflict,
    MetadataUnavailable,
    NoteState,
    Page,
    SearchHit,
    SearchKind,
    SearchMatch,
    SearchQuery,
    ValidationFailed,
)
from coppermind_store.eligibility import current_source, eligible
from coppermind_store.embeddings import QueryVector, vector_value

if TYPE_CHECKING:
    from coppermind_store.notes import LocalStore

# The passage shown with each hit. Matched words are wrapped in `**`, which a
# Markdown reader shows as bold and a plain one reads past.
EXCERPT_OPTIONS = (
    'StartSel="**", StopSel="**", MaxWords=30, MinWords=12, ShortWord=2, '
    'MaxFragments=2, FragmentDelimiter=" ... "'
)

_CURSOR_VERSION = 1


@dataclass(frozen=True)
class SearchCursor:
    """Where the previous page stopped."""

    query_digest: str
    rank: float
    kind: str
    ref_id: str
    filtered: bool = False
    hybrid: bool = False


def query_digest(q: str) -> str:
    """Ties a cursor to the question it paged, without carrying the question."""
    return hashlib.sha256(q.encode("utf-8")).hexdigest()[:16]


def encode_cursor(cursor: SearchCursor) -> str:
    payload = json.dumps(
        {
            "v": 2 if cursor.filtered else _CURSOR_VERSION,
            "q": cursor.query_digest,
            "r": cursor.rank,
            "k": cursor.kind,
            "id": cursor.ref_id,
            "m": "hybrid" if cursor.hybrid else "keyword",
        },
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_cursor(value: str, q: str) -> SearchCursor:
    """Read a cursor back, refusing one minted for a different question."""
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True))
        if not isinstance(payload, dict) or payload.get("v") not in (_CURSOR_VERSION, 2):
            raise ValueError
        digest, rank, kind, ref_id = (payload.get(key) for key in ("q", "r", "k", "id"))
        scoring = payload.get("m", "keyword")
        if (
            not isinstance(digest, str)
            or isinstance(rank, bool)
            or not isinstance(rank, int | float)
            or not math.isfinite(rank)
            or kind not in ("note", "source", "artifact", "entity")
            or not isinstance(ref_id, str)
            or scoring not in ("keyword", "hybrid")
        ):
            raise ValueError
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        raise ValidationFailed(["cursor: invalid or expired"]) from exc
    if digest != query_digest(q):
        if payload["v"] == 2:
            raise FilterCursorConflict()
        raise ValidationFailed(["cursor: it pages a different q"])
    return SearchCursor(
        query_digest=digest,
        rank=float(rank),
        kind=kind,
        ref_id=ref_id,
        hybrid=scoring == "hybrid",
    )


# A word or a quoted phrase, with the `-` that excludes it, as
# websearch_to_tsquery reads them: a phrase runs to its closing quote, or to
# the end of the question when there is none.
_TERM = re.compile(r'(-*)("[^"]*"?|[^\s"]+)')


def excluded_terms(q: str) -> list[str]:
    """The words and phrases the question excludes from every hit.

    A `-` excludes only at the start of a term; inside a word it is a hyphen.
    An exclusion beside `or` is one side of an alternative, not a condition
    on every hit, so it stays where the parser puts it.
    """
    terms = [(match.group(1), match.group(2)) for match in _TERM.finditer(q)]
    excluded: list[str] = []
    for index, (dashes, term) in enumerate(terms):
        if not dashes or not term.strip('"').strip():
            continue
        neighbours = terms[index - 1 : index] + terms[index + 1 : index + 2]
        if any(not other_dashes and other.lower() == "or" for other_dashes, other in neighbours):
            continue
        excluded.append(term)
    return excluded


def ts_query(q: str) -> sa.ColumnElement[Any]:
    """The question under both configurations, either of which may match."""
    english = sa.func.websearch_to_tsquery(sa.cast("english", REGCONFIG), q)
    simple = sa.func.websearch_to_tsquery(sa.cast("simple", REGCONFIG), q)
    return english.op("||")(simple)


def match_conditions(q: str) -> list[sa.ColumnElement[bool]]:
    """What a hit must satisfy: the question, and none of its exclusions.

    Each exclusion is asked under both configurations, so a document holding
    the excluded word as written or any inflection of it is no hit.
    """
    return [SearchDocument.search_vector.op("@@")(ts_query(q)), *exclusion_conditions(q)]


def exclusion_conditions(q: str) -> list[sa.ColumnElement[bool]]:
    """Only the question's exclusions, which a hit found by meaning must satisfy too."""
    return [
        sa.not_(SearchDocument.search_vector.op("@@")(ts_query(term))) for term in excluded_terms(q)
    ]


def meaning_text(q: str) -> str:
    """What is embedded for a question: its words, without exclusions, quotes or `or`."""
    dropped = set(excluded_terms(q))
    words = [
        match.group(2).strip('"')
        for match in _TERM.finditer(q)
        if match.group(2) not in dropped or not match.group(1)
    ]
    return " ".join(word for word in words if word and word.lower() != "or")


def note_state() -> sa.ColumnElement[str]:
    """The state a hit reports: the mirror's for a note, `ok` for a source."""
    return sa.func.coalesce(Note.state, "ok")


def filter_conditions(query: SearchQuery) -> list[sa.ColumnElement[bool]]:
    """Listing's filters, as conditions on the index joined to the mirror."""
    conditions: list[sa.ColumnElement[bool]] = []
    if query.folder is not None:
        conditions.append(sa.func.starts_with(SearchDocument.path, f"{query.folder}/"))
    if query.reviewed is not None:
        conditions.append(Note.reviewed.is_(query.reviewed))
    if query.type is not None:
        conditions.append(Note.type == query.type)
    if query.context is not None:
        conditions.append(Note.context == query.context)
    if query.account is not None:
        conditions.append(sa.literal(query.account) == sa.func.any(Note.account))
    if query.from_date is not None:
        conditions.append(Note.date >= query.from_date)
    if query.to_date is not None:
        conditions.append(Note.date <= query.to_date)
    if query.tag is not None:
        conditions.append(
            sa.or_(
                sa.literal(query.tag) == sa.func.any(Note.tags),
                sa.literal(query.tag) == sa.func.any(Artifact.tags),
            )
        )
    if query.state is not None:
        conditions.append(note_state() == query.state)
    if query.kind is not None:
        conditions.append(SearchDocument.kind == query.kind)
    if not query.include_artifacts:
        conditions.append(SearchDocument.kind != "artifact")
    if query.citing_note is not None:
        cites = sa.exists(
            sa.select(Note.id).where(
                Note.id == query.citing_note,
                SearchDocument.ref_id == sa.func.any(Note.artifact_ids),
            )
        ).correlate_except(Note)
        conditions.append(sa.and_(SearchDocument.kind == "artifact", cites))
    return conditions


def candidates(
    query: SearchQuery,
    notes_folder: str,
    extra: list[Any] | None,
    *,
    entities: bool,
    columns: list[Any],
) -> sa.Select[Any]:
    """The index joined to the mirror with every condition but the question itself.

    What may be shown at all, to this reader, under these filters: published, present,
    not deleted, and permitted by kind. Full-text and meaning searches both start here,
    so neither can surface what the other would hide.
    """
    state = note_state()
    entity_allowed = sa.exists(
        sa.select(EntityRecord.id).where(
            EntityRecord.id == SearchDocument.ref_id,
            EntityRecord.active.is_(True),
            sa.cast(EntityRecord.version, sa.Text) == SearchDocument.version,
        )
    ).correlate_except(EntityRecord)
    permitted = ["note", "source", "artifact"] + (["entity"] if entities else [])
    return (
        sa.select(*columns)
        .select_from(SearchDocument)
        .outerjoin(Note, sa.and_(SearchDocument.kind == "note", Note.id == SearchDocument.ref_id))
        .outerjoin(
            Artifact,
            sa.and_(SearchDocument.kind == "artifact", Artifact.id == SearchDocument.ref_id),
        )
        .where(SearchDocument.kind.in_(permitted))
        .where(
            sa.or_(
                eligible(SearchDocument.kind, SearchDocument.ref_id, notes_folder),
                sa.and_(SearchDocument.kind == "entity", entity_allowed),
            )
        )
        .where(
            sa.or_(
                SearchDocument.kind != "source",
                current_source(SearchDocument.ref_id, SearchDocument.version),
            )
        )
        # A note entry the mirror no longer holds as present is never a hit,
        # whatever a catch-up step has not yet removed.
        .where(sa.or_(SearchDocument.kind != "note", Note.id.is_not(None)))
        .where(state.not_in(("missing", "deleted")))
        .where(*filter_conditions(query))
        .where(*(extra or []))
    )


def build_search(
    query: SearchQuery,
    notes_folder: str,
    after: SearchCursor | None = None,
    extra: list[Any] | None = None,
    *,
    entities: bool = False,
) -> sa.Select[Any]:
    """One page of hits plus one, so the caller knows whether another follows.

    The excerpt is computed in the outer query, over the page only, because
    building a highlighted passage is the expensive part of the answer.
    """
    tsquery = ts_query(query.q)
    rank = sa.func.ts_rank(SearchDocument.search_vector, tsquery)
    matches = candidates(
        query,
        notes_folder,
        extra,
        entities=entities,
        columns=[
            SearchDocument.kind.label("kind"),
            SearchDocument.ref_id.label("ref_id"),
            SearchDocument.path.label("path"),
            SearchDocument.title.label("title"),
            rank.label("rank"),
            note_state().label("state"),
        ],
    ).where(*match_conditions(query.q))
    if after is not None:
        matches = matches.where(
            sa.or_(
                rank < after.rank,
                sa.and_(
                    rank == after.rank,
                    sa.tuple_(SearchDocument.kind, SearchDocument.ref_id)
                    > sa.tuple_(sa.literal(after.kind), sa.literal(after.ref_id)),
                ),
            )
        )
    page = (
        matches.order_by(rank.desc(), SearchDocument.kind, SearchDocument.ref_id)
        .limit(query.limit + 1)
        .subquery("page")
    )
    body = sa.select(SearchDocument.body).where(
        SearchDocument.kind == page.c.kind, SearchDocument.ref_id == page.c.ref_id
    )
    excerpt = sa.func.ts_headline(
        sa.cast("english", REGCONFIG),
        body.scalar_subquery(),
        tsquery,
        EXCERPT_OPTIONS,
    )
    return sa.select(page, excerpt.label("excerpt")).order_by(
        page.c.rank.desc(), page.c.kind, page.c.ref_id
    )


# Hybrid search fuses two ranked lists, the best keyword matches and the nearest
# passages by meaning, by reciprocal rank: a hit scores 1 / (K + position) in each
# list it is in. It needs no common scale between a text rank and a distance, and a
# hit both lists like beats one only a list likes.
RRF_K = 60
KEYWORD_CANDIDATES = 500
MEANING_CANDIDATES = 100
MEANING_EXCERPT_CHARS = 400
# A passage counts as near only when it is closer than unrelated (cosine distance
# under 1) and within this much of the nearest one. The margin keeps the scale of
# the model out of it: one whose unrelated text still scores 0.7 and one whose
# unrelated text scores 0.1 both keep the best matches and drop the filler.
MEANING_MARGIN = 0.25


async def hybrid_rows(
    session: Any,
    query: SearchQuery,
    notes_folder: str,
    after: SearchCursor | None,
    predicates: list[Any],
    entities: bool,
    wanted: QueryVector,
) -> list[Any]:
    """`limit + 1` rows of the fused answer, in the shape `page_of` reads."""
    rank = sa.func.ts_rank(SearchDocument.search_vector, ts_query(query.q))
    keyword = list(
        (
            await session.execute(
                candidates(
                    query,
                    notes_folder,
                    predicates,
                    entities=entities,
                    columns=[
                        SearchDocument.kind.label("kind"),
                        SearchDocument.ref_id.label("ref_id"),
                        SearchDocument.path.label("path"),
                        SearchDocument.title.label("title"),
                        note_state().label("state"),
                    ],
                )
                .where(*match_conditions(query.q))
                .order_by(rank.desc(), SearchDocument.kind, SearchDocument.ref_id)
                .limit(KEYWORD_CANDIDATES)
            )
        ).all()
    )
    distance = SearchEmbedding.embedding.cosine_distance(vector_value(wanted.vector))
    nearest = (
        candidates(
            query,
            notes_folder,
            predicates,
            entities=entities,
            columns=[
                SearchDocument.kind.label("kind"),
                SearchDocument.ref_id.label("ref_id"),
                SearchDocument.path.label("path"),
                SearchDocument.title.label("title"),
                note_state().label("state"),
                SearchEmbedding.start_char.label("start_char"),
                SearchEmbedding.end_char.label("end_char"),
                distance.label("distance"),
            ],
        )
        .join(
            SearchEmbedding,
            sa.and_(
                SearchEmbedding.kind == SearchDocument.kind,
                SearchEmbedding.ref_id == SearchDocument.ref_id,
                SearchEmbedding.doc_version == SearchDocument.version,
                SearchEmbedding.model == wanted.model,
                sa.func.vector_dims(SearchEmbedding.embedding) == len(wanted.vector),
            ),
        )
        .where(*exclusion_conditions(query.q))
        .distinct(SearchDocument.kind, SearchDocument.ref_id)
        .order_by(SearchDocument.kind, SearchDocument.ref_id, distance)
        .subquery("nearest")
    )
    body = sa.select(SearchDocument.body).where(
        SearchDocument.kind == nearest.c.kind, SearchDocument.ref_id == nearest.c.ref_id
    )
    passage = sa.func.substr(
        body.scalar_subquery(),
        nearest.c.start_char + 1,
        sa.func.least(nearest.c.end_char - nearest.c.start_char, MEANING_EXCERPT_CHARS),
    )
    meaning = list(
        (
            await session.execute(
                sa.select(nearest, passage.label("passage"))
                .order_by(nearest.c.distance, nearest.c.kind, nearest.c.ref_id)
                .limit(MEANING_CANDIDATES)
            )
        ).all()
    )
    if meaning:
        limit = min(1.0, meaning[0].distance + MEANING_MARGIN)
        meaning = [row for row in meaning if row.distance < limit]
    return await _fused_page(session, query, after, keyword, meaning)


async def _fused_page(
    session: Any,
    query: SearchQuery,
    after: SearchCursor | None,
    keyword: list[Any],
    meaning: list[Any],
) -> list[Any]:
    scores: dict[tuple[str, str], float] = {}
    found: dict[tuple[str, str], Any] = {}
    in_keyword: set[tuple[str, str]] = set()
    in_meaning: set[tuple[str, str]] = set()
    for position, row in enumerate(keyword):
        key = (row.kind, row.ref_id)
        scores[key] = scores.get(key, 0.0) + 1 / (RRF_K + position + 1)
        found[key] = row
        in_keyword.add(key)
    for position, row in enumerate(meaning):
        key = (row.kind, row.ref_id)
        scores[key] = scores.get(key, 0.0) + 1 / (RRF_K + position + 1)
        found.setdefault(key, row)
        in_meaning.add(key)
    ordered = sorted(scores, key=lambda key: (-scores[key], key[0], key[1]))
    if after is not None:
        ordered = [
            key
            for key in ordered
            if scores[key] < after.rank
            or (scores[key] == after.rank and key > (after.kind, after.ref_id))
        ]
    chosen = ordered[: query.limit + 1]
    headlines: dict[tuple[str, str], str] = {}
    words = [key for key in chosen if key in in_keyword]
    if words:
        body = SearchDocument.body
        rows = await session.execute(
            sa.select(
                SearchDocument.kind,
                SearchDocument.ref_id,
                sa.func.ts_headline(
                    sa.cast("english", REGCONFIG), body, ts_query(query.q), EXCERPT_OPTIONS
                ),
            ).where(sa.tuple_(SearchDocument.kind, SearchDocument.ref_id).in_(words))
        )
        headlines = {(kind, ref_id): text for kind, ref_id, text in rows}
    rows_out: list[Any] = []
    for key in chosen:
        row = found[key]
        text = headlines.get(key) or getattr(row, "passage", "") or ""
        rows_out.append(
            SimpleNamespace(
                kind=row.kind,
                ref_id=row.ref_id,
                path=row.path,
                title=row.title,
                state=row.state,
                rank=scores[key],
                excerpt=text,
                match="both"
                if key in in_keyword and key in in_meaning
                else "keyword"
                if key in in_keyword
                else "meaning",
            )
        )
    return rows_out


def folder_of(path: str) -> str:
    """The folder a hit sits in, empty at the notes filesystem root."""
    return path.rpartition("/")[0]


def tidy_excerpt(text: str | None) -> str:
    """One line: a passage spanning paragraphs reads as one in a result list."""
    return " ".join((text or "").split())


def page_of(
    rows: list[Any], query: SearchQuery, context: str | None = None, *, hybrid: bool = False
) -> Page[SearchHit]:
    """Turn `limit + 1` rows into a page and the cursor for the next one."""
    hits = [
        SearchHit(
            id=row.ref_id,
            kind=cast(SearchKind, row.kind),
            title=row.title,
            path=row.path,
            folder=folder_of(row.path),
            excerpt=tidy_excerpt(row.excerpt),
            rank=float(row.rank),
            state=cast(NoteState, row.state),
            match=cast(SearchMatch, getattr(row, "match", "keyword")),
        )
        for row in rows[: query.limit]
    ]
    next_cursor = None
    if len(rows) > query.limit and hits:
        last = hits[-1]
        next_cursor = encode_cursor(
            SearchCursor(
                query_digest=query_digest(context if context is not None else query.q),
                rank=last.rank,
                kind=last.kind,
                ref_id=last.id,
                filtered=context is not None,
                hybrid=hybrid,
            )
        )
    return Page[SearchHit](items=hits, next_cursor=next_cursor)


async def search(
    store: LocalStore, query: SearchQuery, *, entities: bool = False
) -> Page[SearchHit]:
    from coppermind_store.field_index import conditions, query_context

    if entities and store.entities.purge_pending():
        raise ValidationFailed(["Finish or abandon the interrupted retention purge first"])
    revision = store.control.schema_document().revision
    context = query_context(query, revision) if query.filter else None
    try:
        after = (
            decode_cursor(query.cursor, context if context is not None else query.q)
            if query.cursor
            else None
        )
    except ValidationFailed as exc:
        if query.filter and "different q" in str(exc):
            raise FilterCursorConflict() from exc
        raise
    # Asked before any transaction opens, and never fatal: with no embeddings model, or one
    # that is not answering, the answer is the full-text answer.
    wanted = None
    if query.mode != "keyword":
        words = meaning_text(query.q)
        wanted = await store.embeddings.query_vector(words) if words else None
    if after is not None and after.hybrid != (wanted is not None):
        raise ValidationFailed(["cursor: the ranking changed since that page; search again"])
    try:
        async with store.session_factory() as session, session.begin():
            if query.filter:
                await session.execute(
                    sa.text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                )
            predicates = await conditions(store, query, session)
            notes_folder = store.control.settings().notes.notes_folder
            if wanted is not None:
                rows = await hybrid_rows(
                    session, query, notes_folder, after, predicates, entities, wanted
                )
            else:
                statement = build_search(query, notes_folder, after, predicates, entities=entities)
                rows = list((await session.execute(statement)).all())
    except (SQLAlchemyError, OSError) as exc:
        if wanted is not None:
            store.embeddings.forget_table()
        raise MetadataUnavailable(str(exc)) from exc
    if query.filter and store.control.schema_document().revision != revision:
        raise FilterCursorConflict()
    return page_of(rows, query, context, hybrid=wanted is not None)
