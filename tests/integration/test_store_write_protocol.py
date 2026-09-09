"""What actually reaches the notes filesystem and the database, and in what order."""

from __future__ import annotations

import os

import httpx
import pytest
import sqlalchemy as sa
from coppermind_api.main import create_app as create_api_app
from coppermind_store.control import ControlState
from coppermind_store.fs import content_hash
from coppermind_store.notes import LocalStore

from coppermind import frontmatter as fm
from coppermind.db.models import Note
from coppermind.ids import is_valid_id
from coppermind.schema import default_schema
from coppermind.store_protocol import (
    CreateNote,
    MetadataUnavailable,
    NotesFilesystemUnavailable,
    NoteUnparseable,
    NotFound,
    PathCollision,
    ValidationFailed,
)

MEETING = {
    "date": "2026-09-08",
    "type": "meeting",
    "context": "customer",
    "account": "Ameren",
    "tags": ["architecture"],
}


async def test_a_created_note_is_a_file_first_and_a_row_second(store: LocalStore, session_factory):
    note = await store.create_note(
        CreateNote(title="Ameren Architecture Sync", body="## Key points\n", frontmatter=MEETING)
    )

    assert is_valid_id(note.id)
    assert note.path == "Review/2026-09-08 Ameren Architecture Sync.md"

    on_disk = (store.notes_root / note.path).read_bytes()
    assert content_hash(on_disk) == note.content_hash

    frontmatter, body = fm.parse(on_disk.decode("utf-8"))
    assert frontmatter["id"] == note.id
    assert frontmatter["reviewed"] is False
    assert frontmatter["sources"] == []
    assert body.startswith("# Ameren Architecture Sync")

    async with session_factory() as session:
        row = (await session.execute(sa.select(Note).where(Note.id == note.id))).scalar_one()
    assert row.path == note.path
    assert row.account == "Ameren"
    assert row.reviewed is False
    assert row.content_hash == note.content_hash


async def test_a_note_reads_back_by_its_identifier(store: LocalStore):
    request = CreateNote(title="Runbook", frontmatter={"type": "reference"})
    created = await store.create_note(request)
    fetched = await store.get_note(created.id)
    assert fetched.id == created.id
    assert fetched.path == "Review/Runbook.md"
    assert fetched.content_hash == created.content_hash
    assert fetched.body in (store.notes_root / created.path).read_text(encoding="utf-8")


async def test_an_unknown_identifier_is_a_miss_not_an_error(store: LocalStore):
    with pytest.raises(NotFound):
        await store.get_note("01K4Q8Z3N7V2X9M1B5C6D8E0F2")


async def test_two_notes_with_the_same_title_both_survive(store: LocalStore):
    first = await store.create_note(CreateNote(title="Weekly sync", frontmatter=MEETING))
    second = await store.create_note(CreateNote(title="Weekly sync", frontmatter=MEETING))
    assert first.id != second.id
    assert second.path.endswith("(2).md")
    assert (store.notes_root / first.path).is_file()
    assert (store.notes_root / second.path).is_file()


async def test_a_schema_violation_writes_nothing_at_all(store: LocalStore, session_factory):
    with pytest.raises(ValidationFailed) as raised:
        await store.create_note(CreateNote(title="No account", frontmatter={"context": "customer"}))
    assert any("account" in problem for problem in raised.value.errors)
    assert list((store.notes_root).rglob("*.md")) == []
    async with session_factory() as session:
        counted = await session.execute(sa.select(sa.func.count()).select_from(Note))
    assert counted.scalar_one() == 0


async def test_a_database_outage_refuses_the_write_and_leaves_no_orphan_file(
    unreachable_store: LocalStore,
):
    """The captain's rule, made concrete.

    Writes return a clean failure while PostgreSQL is away rather than half
    succeeding. The notes filesystem is untouched, so Obsidian Sync and anyone
    editing on a device carry on unaffected.
    """
    with pytest.raises(MetadataUnavailable):
        await unreachable_store.create_note(CreateNote(title="During an outage"))
    assert list(unreachable_store.notes_root.rglob("*.md")) == []


async def test_a_read_during_an_outage_reports_the_outage(unreachable_store: LocalStore):
    with pytest.raises(MetadataUnavailable):
        await unreachable_store.get_note("01K4Q8Z3N7V2X9M1B5C6D8E0F2")


async def test_a_filesystem_failure_reports_the_filesystem_not_the_database(store: LocalStore):
    """A write that cannot land is not a database outage and must not say it is.

    A read only notes volume used to surface as `metadata_unavailable`, which
    pointed the operator at PostgreSQL and contradicted readiness.
    """
    if os.geteuid() == 0:
        pytest.skip("root ignores the directory mode this test relies on")
    original = store.notes_root.stat().st_mode
    os.chmod(store.notes_root, 0o555)
    try:
        with pytest.raises(NotesFilesystemUnavailable):
            await store.create_note(CreateNote(title="During a remount"))
    finally:
        os.chmod(store.notes_root, original)


async def test_an_unreadable_review_folder_reports_the_filesystem_on_create(store: LocalStore):
    if os.geteuid() == 0:
        pytest.skip("root ignores the directory mode this test relies on")
    review = store.notes_root / "Review"
    review.mkdir()
    review.chmod(0)
    try:
        with pytest.raises(NotesFilesystemUnavailable):
            await store.create_note(CreateNote(title="Unreadable review folder"))
    finally:
        review.chmod(0o755)


async def test_an_unreadable_note_path_reports_the_filesystem_on_read(store: LocalStore):
    if os.geteuid() == 0:
        pytest.skip("root ignores the directory mode this test relies on")
    note = await store.create_note(CreateNote(title="Unreadable note path"))
    review = (store.notes_root / note.path).parent
    review.chmod(0)
    try:
        with pytest.raises(NotesFilesystemUnavailable):
            await store.get_note(note.id)
    finally:
        review.chmod(0o755)


async def test_the_mirror_reads_the_schema_version_by_role_not_by_name(
    wiring, session_factory, control: ControlState
):
    """Renaming the schema version key in Admin keeps the mirror row correct."""
    renamed = default_schema()
    for definition in renamed.keys:
        if definition.name == "schema_version":
            definition.name = "format_version"
            definition.default = 2
    renamed.roles["schema_version_key"] = "format_version"
    control.store.write("schema", renamed.model_dump(mode="json"), if_revision=1)

    wiring.notes_dir.mkdir(parents=True, exist_ok=True)
    store = LocalStore(wiring.notes_dir, control, session_factory)
    note = await store.create_note(CreateNote(title="Renamed key"))

    assert note.frontmatter["format_version"] == 2
    async with session_factory() as session:
        row = (await session.execute(sa.select(Note).where(Note.id == note.id))).scalar_one()
        assert row.schema_version == 2


async def test_create_projections_ignore_role_values_that_are_not_lists(
    store: LocalStore, control: ControlState, session_factory
):
    changed = default_schema()
    for definition in changed.keys:
        if definition.name in {"sources", "tags"}:
            definition.kind = "string"
            definition.default = None
    control.store.write("schema", changed.model_dump(mode="json"), if_revision=1)

    note = await store.create_note(
        CreateNote(
            title="String roles",
            frontmatter={"sources": "source-1", "tags": "operations"},
        )
    )

    assert note.sources == []
    async with session_factory() as session:
        row = (await session.execute(sa.select(Note).where(Note.id == note.id))).scalar_one()
    assert row.tags == []


async def test_schema_version_projection_defaults_when_role_is_not_an_integer(
    store: LocalStore, control: ControlState, session_factory
):
    changed = default_schema()
    for definition in changed.keys:
        if definition.name == "schema_version":
            definition.kind = "string"
            definition.default = "v2"
    control.store.write("schema", changed.model_dump(mode="json"), if_revision=1)

    note = await store.create_note(CreateNote(title="String schema version"))

    assert note.frontmatter["schema_version"] == "v2"
    assert (store.notes_root / note.path).is_file()
    async with session_factory() as session:
        row = (await session.execute(sa.select(Note).where(Note.id == note.id))).scalar_one()
    assert row.schema_version == 1


async def test_public_create_rejects_unstorable_metadata_without_leaving_a_file(
    store: LocalStore,
):
    app = create_api_app()
    app.state.store = store

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api"
    ) as client:
        response = await client.post(
            "/v1/notes",
            content='{"title":"Weekly","frontmatter":{"weight":1e999}}',
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "validation_error"
    assert body["errors"] == ["request contains a value the metadata store cannot accept"]
    assert "unavailable" not in body["message"]
    assert "retry" not in body["message"]
    assert list(store.notes_root.rglob("*.md")) == []


async def test_a_row_that_outlived_its_file_is_a_collision_not_an_outage(store: LocalStore):
    """A note deleted on a device leaves a row behind until reconciliation runs.

    Creating the same title again then hits the unique path constraint. That is
    the notes filesystem and the mirror disagreeing, not PostgreSQL being away,
    and answering `metadata_unavailable` would send the operator after a
    database that is healthy.
    """
    first = await store.create_note(CreateNote(title="Runbook", frontmatter={"type": "reference"}))
    (store.notes_root / first.path).unlink()

    with pytest.raises(PathCollision) as raised:
        await store.create_note(CreateNote(title="Runbook", frontmatter={"type": "reference"}))
    assert raised.value.existing_path == first.path
    # The refused write must not leave its file behind. It would sync to every
    # device, and a read of the first identifier would then serve the second
    # note's content under the first note's name.
    assert list(store.notes_root.rglob("*.md")) == []


async def test_a_note_a_person_broke_on_a_device_is_not_our_error(store: LocalStore):
    """Editing in Obsidian and syncing back is the round trip this slice proves.

    A malformed frontmatter block used to come back as an unexpected store
    error, blaming Coppermind for the person's own file.
    """
    created = await store.create_note(
        CreateNote(title="Runbook", frontmatter={"type": "reference"})
    )
    path = store.notes_root / created.path
    before = path.read_text(encoding="utf-8")
    path.write_text(before.replace("\n---\n", "\n", 1), encoding="utf-8")

    with pytest.raises(NoteUnparseable) as raised:
        await store.get_note(created.id)
    assert raised.value.note_id == created.id
    assert "has not modified the file" in str(raised.value)
    assert path.read_text(encoding="utf-8") == before.replace("\n---\n", "\n", 1)


async def test_schema_deviation_does_not_corrupt_the_sources_projection(store: LocalStore):
    created = await store.create_note(
        CreateNote(title="Runbook", frontmatter={"type": "reference"})
    )
    path = store.notes_root / created.path
    changed = path.read_text(encoding="utf-8").replace("sources: []", "sources: source-1")
    path.write_text(changed, encoding="utf-8")

    fetched = await store.get_note(created.id)
    assert fetched.frontmatter["sources"] == "source-1"
    assert fetched.sources == []
    assert path.read_text(encoding="utf-8") == changed


async def test_a_row_whose_file_is_now_another_note_is_a_miss(store: LocalStore):
    """Deleting one note on a device and renaming another onto its filename.

    Nothing reconciles the mirror yet, so the first row still names that path.
    The read must answer a miss rather than the second note's body, frontmatter
    and content hash under the first note's identifier.
    """
    runbook = await store.create_note(
        CreateNote(title="Runbook", frontmatter={"type": "reference"})
    )
    meeting = await store.create_note(
        CreateNote(title="Meeting", frontmatter={"type": "reference"})
    )
    (store.notes_root / runbook.path).unlink()
    (store.notes_root / meeting.path).rename(store.notes_root / runbook.path)

    with pytest.raises(NotFound):
        await store.get_note(runbook.id)


async def test_a_row_pointing_outside_the_notes_filesystem_is_refused(
    store: LocalStore, session_factory
):
    """A mirrored path that escapes the root is a refusal, not a server error."""
    created = await store.create_note(
        CreateNote(title="Runbook", frontmatter={"type": "reference"})
    )
    async with session_factory() as session:
        await session.execute(
            sa.update(Note).where(Note.id == created.id).values(path="../outside.md")
        )
        await session.commit()

    with pytest.raises(NotFound):
        await store.get_note(created.id)
