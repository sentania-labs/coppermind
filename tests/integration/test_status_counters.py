"""Status counters, source listing and problems, computed from the mirror.

Every number here comes from PostgreSQL rows the store and the reconciler
wrote, or from a rejection the store recorded. None of it is written into a
note, so each test also holds that the note files are byte for byte unchanged.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from coppermind_store.notes import LocalStore
from coppermind_store.reconciler import reconcile_once

from coppermind.db.models import Note, RecordedRejection, Source
from coppermind.ids import new_id
from coppermind.store_protocol import (
    CreateNote,
    IngestRequest,
    NotFound,
    PayloadTooLarge,
    SourceQuery,
    ValidationFailed,
)


def _ingest(external_id: str, provider: str = "plaud") -> IngestRequest:
    return IngestRequest.model_validate(
        {
            "source": {
                "provider": provider,
                "external_source_id": external_id,
                "source_type": "transcript",
                "artifacts": [
                    {"name": "transcript.txt", "mime_type": "text/plain", "content": "Hello."}
                ],
            },
            "note": {
                "title": f"Call {external_id}",
                "frontmatter": {"date": "2026-09-08", "type": "meeting"},
            },
        }
    )


def _set(store: LocalStore, section: str, key: str, value: object) -> None:
    current = store.control.store.read("settings")
    body = dict(current.body)
    body.pop("revision", None)
    body[section] = {**body[section], key: value}
    store.control.store.write("settings", body, if_revision=current.revision)


async def _add_row(store: LocalStore, path: str, *, reviewed: bool, state: str = "ok") -> str:
    note_id = new_id()
    now = datetime.now(tz=UTC)
    async with store.session_factory() as session, session.begin():
        session.add(
            Note(
                id=note_id,
                path=path,
                content_hash="sha256:row",
                size_bytes=1,
                frontmatter={},
                reviewed=reviewed,
                tags=[],
                state=state,
                state_reason="frontmatter is not valid YAML" if state == "unparsed" else None,
                first_seen_at=now,
                updated_at=now,
            )
        )
    return note_id


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(path): path.read_bytes() for path in root.rglob("*.md")}


async def test_a_fresh_store_counts_nothing(store: LocalStore):
    status = await store.get_status()

    assert status.counters.model_dump() == {
        "notes_awaiting_review": 0,
        "notes_by_state": {},
        "sources": 0,
        "rejected_ingests": 0,
        "name_collisions": 0,
        "unparseable_files": 0,
    }


async def test_counters_change_when_a_note_or_a_source_is_added(store: LocalStore):
    await store.create_note(CreateNote(title="Kickoff", frontmatter={"type": "meeting"}))
    after_note = (await store.get_status()).counters
    assert after_note.notes_awaiting_review == 1
    assert after_note.notes_by_state == {"ok": 1}
    assert after_note.sources == 0

    await store.ingest(_ingest("rec-1"))
    after_source = (await store.get_status()).counters
    assert after_source.sources == 1
    assert after_source.notes_awaiting_review == 2
    assert after_source.notes_by_state == {"ok": 2}


async def test_awaiting_review_counts_the_configured_folder_not_a_literal(store: LocalStore):
    _set(store, "notes", "review_folder", "Inbox")
    created = await store.create_note(CreateNote(title="Kickoff", frontmatter={"type": "meeting"}))
    assert created.path.startswith("Inbox/")
    await _add_row(store, "Review/Old habit.md", reviewed=False)
    await _add_row(store, "Inbox/Already read.md", reviewed=True)
    await _add_row(store, "Inbox/Nested/Deeper.md", reviewed=False)
    await _add_row(store, "Inbox/Gone.md", reviewed=False, state="missing")

    status = await store.get_status()

    assert status.counters.notes_awaiting_review == 1
    assert status.counters.notes_by_state == {"ok": 4, "missing": 1}


async def test_refused_ingests_are_recorded_without_any_note_or_source(store: LocalStore):
    _set(store, "limits", "ingest_max_bytes", 512)
    oversize = _ingest("rec-big")
    oversize.source.artifacts[0].content = "x" * 1024
    with pytest.raises(PayloadTooLarge):
        await store.ingest(oversize)
    _set(store, "limits", "ingest_max_bytes", 10_000_000)
    invalid = _ingest("rec-bad")
    invalid.note.frontmatter["type"] = "not-a-shipped-type"
    with pytest.raises(ValidationFailed):
        await store.ingest(invalid)

    status = await store.get_status()
    problems = await store.get_problems()

    assert status.counters.rejected_ingests == 2
    assert status.counters.sources == 0
    assert status.counters.notes_by_state == {}
    assert {(item.kind, item.reference, item.reason) for item in problems} == {
        ("ingest", "plaud:rec-big", "payload_too_large"),
        ("ingest", "plaud:rec-bad", "validation_error"),
    }
    assert list(store.notes_root.rglob("*.md")) == []


async def test_unparseable_files_and_collisions_are_counted_and_listed(store: LocalStore):
    note = await store.create_note(CreateNote(title="Meeting", frontmatter={"type": "reference"}))
    await reconcile_once(store)
    copied = store.notes_root / "Meetings" / "2026-09-20.md"
    copied.parent.mkdir(parents=True, exist_ok=True)
    copied.write_bytes((store.notes_root / note.path).read_bytes())
    broken = await store.create_note(CreateNote(title="Broken", frontmatter={"type": "meeting"}))
    broken_path = store.notes_root / broken.path
    broken_path.write_bytes(broken_path.read_bytes().replace(b"tags: []", b"tags: ["))
    before = _snapshot(store.notes_root)

    counts = await reconcile_once(store)
    status = await store.get_status()
    problems = await store.get_problems()

    assert counts["duplicates"] == 1
    assert status.counters.name_collisions == 1
    assert status.counters.unparseable_files == 1
    collisions = [item for item in problems if item.kind == "collision"]
    assert collisions and {item.note_id for item in collisions} == {note.id}
    assert any("Meetings/2026-09-20.md" in item.reason for item in collisions)
    unparsed = [item for item in problems if item.kind == "unparsed"]
    assert [(item.note_id, item.reference) for item in unparsed] == [(broken.id, broken.path)]
    assert _snapshot(store.notes_root) == before

    copied.unlink()
    await reconcile_once(store)
    assert (await store.get_status()).counters.name_collisions == 0


async def test_a_refused_revision_of_a_known_source_links_to_that_source(store: LocalStore):
    result = await store.ingest(_ingest("rec-1"))
    _set(store, "limits", "ingest_max_bytes", 512)
    again = _ingest("rec-1")
    again.source.artifacts[0].content = "x" * 1024
    with pytest.raises(PayloadTooLarge):
        await store.ingest(again)

    problems = await store.get_problems()

    assert [(item.kind, item.source_id) for item in problems] == [("ingest", result.source.id)]


async def test_sources_page_newest_first_and_filter_by_provider_and_date(store: LocalStore):
    first = await store.ingest(_ingest("rec-1"))
    second = await store.ingest(_ingest("rec-2"))
    other = await store.ingest(_ingest("call-1", provider="granola"))
    async with store.session_factory() as session, session.begin():
        await session.execute(
            sa.update(Source)
            .where(Source.id == first.source.id)
            .values(created_at=datetime(2026, 9, 1, 12, tzinfo=UTC))
        )

    page_one = await store.list_sources(SourceQuery(limit=2))
    assert [item.id for item in page_one.items] == [other.source.id, second.source.id]
    assert page_one.next_cursor
    page_two = await store.list_sources(SourceQuery(limit=2, cursor=page_one.next_cursor))
    assert [item.id for item in page_two.items] == [first.source.id]
    assert page_two.next_cursor is None

    plaud = await store.list_sources(SourceQuery(provider="plaud"))
    assert {item.id for item in plaud.items} == {first.source.id, second.source.id}
    early = await store.list_sources(
        SourceQuery.model_validate({"from": date(2026, 9, 1), "to": date(2026, 9, 2)})
    )
    assert [item.id for item in early.items] == [first.source.id]

    with pytest.raises(ValidationFailed):
        await store.list_sources(SourceQuery(cursor="not-a-cursor"))


async def test_a_notes_sources_carry_their_projection_paths(store: LocalStore):
    result = await store.ingest(_ingest("rec-1"))
    plain = await store.create_note(CreateNote(title="Plain", frontmatter={"type": "meeting"}))

    cited = await store.get_note_sources(result.note.id)

    assert [(item.id, item.projection_path) for item in cited] == [
        (result.source.id, result.projection_path)
    ]
    assert await store.get_note_sources(plain.id) == []
    with pytest.raises(NotFound):
        await store.get_note_sources(new_id())


async def test_recorded_rejections_survive_only_as_rows(store: LocalStore):
    _set(store, "limits", "ingest_max_bytes", 512)
    oversize = _ingest("rec-big")
    oversize.source.artifacts[0].content = "x" * 1024
    with pytest.raises(PayloadTooLarge):
        await store.ingest(oversize)

    async with store.session_factory() as session:
        rows = (await session.scalars(sa.select(RecordedRejection))).all()

    assert [(row.kind, row.reference) for row in rows] == [("ingest", "plaud:rec-big")]
    assert rows[0].created_at is not None
