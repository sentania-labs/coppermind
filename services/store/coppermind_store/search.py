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
metadata mirror row of the note. A source projection has no such row, so a
frontmatter filter matches notes only, while `folder` and `state` apply to
both kinds.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import REGCONFIG
from sqlalchemy.exc import SQLAlchemyError

from coppermind.db.models import Note, SearchDocument
from coppermind.store_protocol import (
    MetadataUnavailable,
    NoteState,
    Page,
    SearchHit,
    SearchKind,
    SearchQuery,
    ValidationFailed,
)

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


def query_digest(q: str) -> str:
    """Ties a cursor to the question it paged, without carrying the question."""
    return hashlib.sha256(q.encode("utf-8")).hexdigest()[:16]


def encode_cursor(cursor: SearchCursor) -> str:
    payload = json.dumps(
        {
            "v": _CURSOR_VERSION,
            "q": cursor.query_digest,
            "r": cursor.rank,
            "k": cursor.kind,
            "id": cursor.ref_id,
        },
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_cursor(value: str, q: str) -> SearchCursor:
    """Read a cursor back, refusing one minted for a different question."""
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True))
        if not isinstance(payload, dict) or payload.get("v") != _CURSOR_VERSION:
            raise ValueError
        digest, rank, kind, ref_id = (payload.get(key) for key in ("q", "r", "k", "id"))
        if (
            not isinstance(digest, str)
            or isinstance(rank, bool)
            or not isinstance(rank, int | float)
            or not math.isfinite(rank)
            or kind not in ("note", "source")
            or not isinstance(ref_id, str)
        ):
            raise ValueError
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        raise ValidationFailed(["cursor: invalid or expired"]) from exc
    if digest != query_digest(q):
        raise ValidationFailed(["cursor: it pages a different q"])
    return SearchCursor(query_digest=digest, rank=float(rank), kind=kind, ref_id=ref_id)


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
    conditions: list[sa.ColumnElement[bool]] = [SearchDocument.search_vector.op("@@")(ts_query(q))]
    for term in excluded_terms(q):
        conditions.append(sa.not_(SearchDocument.search_vector.op("@@")(ts_query(term))))
    return conditions


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
        conditions.append(Note.account == query.account)
    if query.from_date is not None:
        conditions.append(Note.date >= query.from_date)
    if query.to_date is not None:
        conditions.append(Note.date <= query.to_date)
    if query.tag is not None:
        conditions.append(sa.literal(query.tag) == sa.func.any(Note.tags))
    if query.state is not None:
        conditions.append(note_state() == query.state)
    return conditions


def build_search(query: SearchQuery, after: SearchCursor | None = None) -> sa.Select[Any]:
    """One page of hits plus one, so the caller knows whether another follows.

    The excerpt is computed in the outer query, over the page only, because
    building a highlighted passage is the expensive part of the answer.
    """
    tsquery = ts_query(query.q)
    rank = sa.func.ts_rank(SearchDocument.search_vector, tsquery)
    state = note_state()
    matches = (
        sa.select(
            SearchDocument.kind.label("kind"),
            SearchDocument.ref_id.label("ref_id"),
            SearchDocument.path.label("path"),
            SearchDocument.title.label("title"),
            rank.label("rank"),
            state.label("state"),
        )
        .select_from(SearchDocument)
        .outerjoin(Note, sa.and_(SearchDocument.kind == "note", Note.id == SearchDocument.ref_id))
        .where(*match_conditions(query.q))
        # A note entry the mirror no longer holds as present is never a hit,
        # whatever a catch-up step has not yet removed.
        .where(sa.or_(SearchDocument.kind == "source", Note.id.is_not(None)))
        .where(state != "missing")
        .where(*filter_conditions(query))
    )
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


def folder_of(path: str) -> str:
    """The folder a hit sits in, empty at the notes filesystem root."""
    return path.rpartition("/")[0]


def tidy_excerpt(text: str | None) -> str:
    """One line: a passage spanning paragraphs reads as one in a result list."""
    return " ".join((text or "").split())


def page_of(rows: list[Any], query: SearchQuery) -> Page[SearchHit]:
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
        )
        for row in rows[: query.limit]
    ]
    next_cursor = None
    if len(rows) > query.limit and hits:
        last = hits[-1]
        next_cursor = encode_cursor(
            SearchCursor(
                query_digest=query_digest(query.q),
                rank=last.rank,
                kind=last.kind,
                ref_id=last.id,
            )
        )
    return Page[SearchHit](items=hits, next_cursor=next_cursor)


async def search(store: LocalStore, query: SearchQuery) -> Page[SearchHit]:
    after = decode_cursor(query.cursor, query.q) if query.cursor else None
    statement = build_search(query, after)
    try:
        async with store.session_factory() as session:
            rows = list((await session.execute(statement)).all())
    except (SQLAlchemyError, OSError) as exc:
        raise MetadataUnavailable(str(exc)) from exc
    return page_of(rows, query)
