"""The search index against PostgreSQL: it follows every write and rebuilds from files.

Each test drives the store the way the API or a device would, then asks the
real full-text index what it can find. Nothing here writes an index row by
hand except to break the index on purpose before a rebuild.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from coppermind_store import indexer
from coppermind_store.notes import LocalStore
from coppermind_store.reconciler import reconcile_once

from coppermind.db.models import SearchDocument, SearchIndexState
from coppermind.store_protocol import (
    CreateNote,
    IngestRequest,
    MoveNote,
    PatchFrontmatter,
    RenameNote,
    ReplaceNote,
    SearchHit,
    SearchQuery,
    ValidationFailed,
)


def note(title: str, body: str, **frontmatter: object) -> CreateNote:
    return CreateNote(
        title=title,
        body=body,
        frontmatter={"date": "2026-09-08", "type": "note", "context": "internal", **frontmatter},
    )


async def search(store: LocalStore, q: str, **filters: object) -> list[SearchHit]:
    page = await store.search(SearchQuery.model_validate({"q": q, **filters}))
    return page.items


async def ids(store: LocalStore, q: str, **filters: object) -> list[str]:
    return [hit.id for hit in await search(store, q, **filters)]


def ingest_request(external_id: str = "rec-1") -> IngestRequest:
    return IngestRequest.model_validate(
        {
            "source": {
                "provider": "plaud",
                "external_source_id": external_id,
                "source_type": "transcript",
                "artifacts": [
                    {
                        "name": "transcript.txt",
                        "mime_type": "text/plain",
                        "content": "Dana: the quarterly kerfuffle about the warehouse lease.",
                    }
                ],
            },
            "note": {
                "title": "Warehouse call",
                "body": "Opening note for the lease discussion.\n",
                "frontmatter": {"date": "2026-09-08", "type": "meeting", "context": "internal"},
            },
        }
    )


# --- writes through the store, as the API makes them ---------------------------


async def test_a_created_note_is_found_with_an_excerpt_and_its_place(store: LocalStore):
    created = await store.create_note(
        note("Budget planning", "We agreed the marmalade budget for next quarter.\n")
    )

    (hit,) = await search(store, "marmalade")

    assert hit.id == created.id
    assert hit.kind == "note"
    assert hit.title == "Budget planning"
    assert hit.path == created.path
    assert hit.folder == "Review"
    assert hit.state == "ok"
    assert hit.rank > 0
    assert "**marmalade**" in hit.excerpt
    assert "\n" not in hit.excerpt


async def test_english_stems_and_simple_keeps_names(store: LocalStore):
    running = await store.create_note(note("Training", "She was running the drills daily.\n"))
    named = await store.create_note(note("Call with Will", "Will said the plan stands.\n"))

    # English reads 'runs' and 'running' as one word.
    assert await ids(store, "runs") == [running.id]
    # 'Will' is an English stop word, so only the 'simple' reading finds the name.
    assert await ids(store, "Will") == [named.id]


async def test_a_title_match_outranks_a_body_match(store: LocalStore):
    body_only = await store.create_note(note("Weekly notes", "Some talk of the zeppelin.\n"))
    in_title = await store.create_note(note("Zeppelin plan", "Details follow.\n"))

    assert await ids(store, "zeppelin") == [in_title.id, body_only.id]


async def test_a_replaced_body_replaces_what_is_found(store: LocalStore):
    created = await store.create_note(note("Supplier", "Original quokka text.\n"))

    replaced = await store.replace_note(
        created.id,
        ReplaceNote(frontmatter=created.frontmatter, body="# Supplier\n\nNow about a wombat.\n"),
        created.content_hash,
    )

    assert await ids(store, "quokka") == []
    assert await ids(store, "wombat") == [replaced.id]


async def test_a_patch_moves_a_note_between_filters(store: LocalStore):
    created = await store.create_note(note("Review me", "The pangolin report.\n"))

    assert await ids(store, "pangolin", reviewed=True) == []
    await store.patch_frontmatter(
        created.id, PatchFrontmatter(set={"reviewed": True}), created.content_hash
    )

    assert await ids(store, "pangolin", reviewed=True) == [created.id]
    assert await ids(store, "pangolin", reviewed=False) == []


async def test_a_move_and_a_rename_are_followed(store: LocalStore):
    created = await store.create_note(note("Old name", "A note about the axolotl.\n"))

    moved = await store.move_note(created.id, MoveNote(target_folder="Work/Clients"), None)
    (hit,) = await search(store, "axolotl", folder="Work")
    assert (hit.path, hit.folder) == (moved.path, "Work/Clients")
    assert await ids(store, "axolotl", folder="Review") == []

    renamed = await store.rename_note(created.id, RenameNote(title="New name"), moved.content_hash)
    (hit,) = await search(store, "axolotl")
    assert (hit.title, hit.path) == ("New name", renamed.path)
    assert await ids(store, "new name") == [created.id]


async def test_a_refused_write_leaves_the_index_as_it_was(store: LocalStore):
    created = await store.create_note(note("Kept", "The original narwhal.\n"))

    with pytest.raises(ValidationFailed):
        await store.replace_note(
            created.id,
            ReplaceNote(frontmatter={**created.frontmatter, "id": "nope"}, body="A walrus.\n"),
            created.content_hash,
        )

    assert await ids(store, "narwhal") == [created.id]
    assert await ids(store, "walrus") == []


# --- changes a device makes, followed by the reconciler ---------------------------


async def test_device_edits_moves_and_deletes_are_followed(store: LocalStore):
    created = await store.create_note(note("Device", "The first capybara.\n"))
    path = store.notes_root / created.path

    path.write_bytes(path.read_bytes().replace(b"capybara", b"chinchilla"))
    await reconcile_once(store)
    assert await ids(store, "capybara") == []
    assert await ids(store, "chinchilla") == [created.id]

    moved = store.notes_root / "Archive" / "Device renamed.md"
    moved.parent.mkdir()
    path.rename(moved)
    await reconcile_once(store)
    (hit,) = await search(store, "chinchilla")
    assert (hit.path, hit.folder) == ("Archive/Device renamed.md", "Archive")

    moved.unlink()
    await reconcile_once(store)
    assert await ids(store, "chinchilla") == []
    async with store.session_factory() as session:
        assert await session.get(SearchDocument, ("note", created.id)) is None
    status = await store.get_search_index_status()
    assert status.notes == 0
    assert status.last_updated_at is not None


async def test_a_note_created_on_a_device_is_indexed_when_adopted(store: LocalStore):
    (store.notes_root / "Inbox").mkdir()
    (store.notes_root / "Inbox" / "Phone note.md").write_text(
        "# Phone note\n\nRemember the okapi.\n", encoding="utf-8"
    )

    counts = await reconcile_once(store)

    assert counts["adopted"] == 1
    (hit,) = await search(store, "okapi")
    assert (hit.kind, hit.path, hit.title) == ("note", "Inbox/Phone note.md", "Phone note")


async def test_an_excluded_word_is_excluded_in_every_form(store: LocalStore):
    running = await store.create_note(note("Track", "The budget for running shoes.\n"))
    plain = await store.create_note(note("Ledger", "The budget for office chairs.\n"))

    assert sorted(await ids(store, "budget")) == sorted([running.id, plain.id])
    # English reads 'runs' as 'running', so excluding it excludes both.
    assert await ids(store, "budget -runs") == [plain.id]
    assert await ids(store, "budget -running") == [plain.id]
    assert await ids(store, 'budget -"running shoes"') == [plain.id]


async def test_a_broken_edit_keeps_the_last_text_findable_as_unparsed(store: LocalStore):
    created = await store.create_note(note("Fragile", "About the tapir.\n"))
    path = store.notes_root / created.path
    path.write_text("---\nid: [unclosed\n---\nbroken\n", encoding="utf-8")

    await reconcile_once(store)

    (hit,) = await search(store, "tapir")
    assert hit.state == "unparsed"
    assert await ids(store, "tapir", state="ok") == []
    assert await ids(store, "tapir", state="unparsed") == [created.id]


# --- sources --------------------------------------------------------------------


async def test_a_source_projection_is_found_as_a_source(store: LocalStore):
    result = await store.ingest(ingest_request())

    hits = await search(store, "kerfuffle")

    assert [(hit.kind, hit.id) for hit in hits] == [("source", result.source.id)]
    assert hits[0].path == result.projection_path
    assert hits[0].folder.startswith("_Sources")
    # The opening note the ingest wrote is a note, found by its own words.
    assert await ids(store, "lease discussion") == [result.note.id]
    # A frontmatter filter matches notes only; folder and state apply to both.
    assert await ids(store, "kerfuffle", type="meeting") == []
    assert await ids(store, "kerfuffle", folder="_Sources") == [result.source.id]
    assert await ids(store, "kerfuffle", state="ok") == [result.source.id]


# --- filters and paging -----------------------------------------------------------


async def test_filters_combine_with_the_question(store: LocalStore):
    wanted = await store.create_note(
        note("Acme kickoff", "Kickoff for the gazebo.\n", type="meeting", account="Acme")
    )
    await store.create_note(note("Other kickoff", "Another gazebo.\n", type="meeting"))
    await store.create_note(note("Acme memo", "The gazebo memo.\n", account="Acme"))

    assert len(await ids(store, "gazebo")) == 3
    assert await ids(store, "gazebo", type="meeting", account="Acme") == [wanted.id]
    assert await ids(store, "gazebo", type="meeting", account="Acme", folder="Work") == []
    assert await ids(store, "gazebo", **{"from": "2026-09-08", "to": "2026-09-08"}) != []
    assert await ids(store, "gazebo", **{"from": "2026-09-09"}) == []


async def test_pages_walk_every_hit_once_in_rank_order(store: LocalStore):
    for index in range(7):
        await store.create_note(
            note(f"Entry {index}", "lemur " * (index + 1) + "and other words.\n")
        )

    seen: list[SearchHit] = []
    cursor = None
    while True:
        filters: dict[str, object] = {"limit": 3}
        if cursor:
            filters["cursor"] = cursor
        page = await store.search(SearchQuery.model_validate({"q": "lemur", **filters}))
        seen.extend(page.items)
        cursor = page.next_cursor
        if cursor is None:
            break

    assert len(seen) == 7
    assert len({hit.id for hit in seen}) == 7
    ranks = [hit.rank for hit in seen]
    assert ranks == sorted(ranks, reverse=True)
    with pytest.raises(ValidationFailed):
        await store.search(SearchQuery.model_validate({"q": "other", "cursor": page_cursor(seen)}))


def page_cursor(seen: list[SearchHit]) -> str:
    from coppermind_store.search import SearchCursor, encode_cursor, query_digest

    last = seen[-1]
    return encode_cursor(SearchCursor(query_digest("lemur"), last.rank, last.kind, last.id))


# --- catch-up and rebuild -----------------------------------------------------------


async def test_catch_up_indexes_what_the_mirror_knows_and_the_index_lost(store: LocalStore):
    created = await store.create_note(note("Lost", "The missing ibis.\n"))
    async with store.session_factory() as session, session.begin():
        await session.execute(sa.delete(SearchDocument))

    assert await ids(store, "ibis") == []
    counts = await indexer.catch_up(store)

    assert counts["indexed"] == 1
    assert await ids(store, "ibis") == [created.id]


async def test_a_rebuild_recreates_the_index_from_the_files(store: LocalStore):
    first = await store.create_note(note("First", "The heron note.\n"))
    second = await store.create_note(note("Second", "The egret note.\n"))
    result = await store.ingest(ingest_request("rec-rebuild"))
    # Break the index every way it can be broken: an entry for a note that
    # does not exist, one whose text is wrong, and one that is gone.
    async with store.session_factory() as session, session.begin():
        await session.execute(
            sa.update(SearchDocument)
            .where(SearchDocument.ref_id == first.id)
            .values(body="nothing useful", version="stale")
        )
        await session.execute(sa.delete(SearchDocument).where(SearchDocument.ref_id == second.id))
        await indexer.index_note(
            session,
            note_id="01K4Q8Z3N7V2X9M1B5C6D8E0F9",
            path="Review/Ghost.md",
            title="Ghost",
            body="heron ghost",
            version="none",
            now=first.updated_at,
        )

    counts = await indexer.rebuild(store)

    assert counts == {"notes": 3, "sources": 1}
    assert await ids(store, "heron") == [first.id]
    assert await ids(store, "egret") == [second.id]
    assert await ids(store, "kerfuffle") == [result.source.id]
    status = await store.get_search_index_status()
    assert (status.notes, status.sources) == (3, 1)
    assert status.rebuild_completed_at is not None
    assert status.rebuild_error is None


async def test_the_rebuild_job_runs_in_the_background_once(store: LocalStore):
    await store.create_note(note("Background", "The bittern note.\n"))

    started = await store.rebuild_search_index()
    again = await store.rebuild_search_index()
    assert started.started is True
    assert again.started is False
    assert store._search_rebuild is not None
    await store._search_rebuild

    status = await store.get_search_index_status()
    assert status.rebuild_running is False
    assert status.rebuild_completed_at is not None
    assert await ids(store, "bittern") != []


async def test_the_first_pass_after_the_upgrade_builds_the_index(store: LocalStore):
    created = await store.create_note(note("Before", "The kestrel note.\n"))
    async with store.session_factory() as session, session.begin():
        await session.execute(sa.delete(SearchDocument))
        state = await session.get(SearchIndexState, 1)
        assert state is not None and state.rebuild_completed_at is None

    counts = await indexer.after_pass(store)

    assert counts == {"notes": 1, "sources": 0}
    assert await ids(store, "kestrel") == [created.id]
    assert not await indexer.never_built(store)


async def test_a_rebuild_keeps_an_unparsed_note_findable(store: LocalStore):
    created = await store.create_note(note("Fragile", "About the pangolin.\n"))
    path = store.notes_root / created.path
    path.write_text("---\nid: [unclosed\n---\nbroken\n", encoding="utf-8")
    await reconcile_once(store)

    await indexer.rebuild(store)

    (hit,) = await search(store, "pangolin")
    assert (hit.id, hit.state) == (created.id, "unparsed")
