"""What actually reaches the notes filesystem and the database, and in what order."""

from __future__ import annotations

import asyncio
import os
from datetime import date

import coppermind_store.notes as notes_module
import httpx
import pytest
import sqlalchemy as sa
from coppermind_api.auth import ApiKeyAuthenticator
from coppermind_api.main import create_app as create_api_app
from coppermind_store.control import ControlState
from coppermind_store.fs import content_hash
from coppermind_store.notes import LocalStore
from sqlalchemy.ext.asyncio import AsyncSession

from coppermind import frontmatter as fm
from coppermind.api_keys import API_SCOPES, ApiKeySet, create_key
from coppermind.db.models import Note
from coppermind.ids import is_valid_id
from coppermind.schema import default_schema
from coppermind.store_protocol import (
    CreateNote,
    MetadataUnavailable,
    NotesFilesystemUnavailable,
    NoteUnparseable,
    NotFound,
    PatchFrontmatter,
    PathCollision,
    ReplaceNote,
    ValidationFailed,
    VersionConflict,
)

MEETING = {
    "date": "2026-09-08",
    "type": "meeting",
    "context": "customer",
    "account": "Ameren",
    "tags": ["architecture"],
}


def authorized_api(store: LocalStore):
    record, credential = create_key(
        "integration test",
        list(API_SCOPES),
        key_id="d1e2f3a4b5c6d7e8",
        secret="integration-test-secret",
    )
    store.control.store.write(
        "keys", ApiKeySet(keys=[record]).model_dump(mode="json"), if_revision=None
    )
    app = create_api_app()
    app.state.store = store
    app.state.api_key_auth = ApiKeyAuthenticator(store)
    return app, {"Authorization": f"Bearer {credential}"}


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
    store = LocalStore(wiring.notes_dir, control, session_factory, wiring.sources_dir)
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
    app, authorization = authorized_api(store)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api"
    ) as client:
        response = await client.post(
            "/v1/notes",
            content='{"title":"Weekly","frontmatter":{"weight":1e999}}',
            headers={"Content-Type": "application/json", **authorization},
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


def edited(note, **frontmatter) -> ReplaceNote:
    """The document a read returned, sent back with a line added and keys changed."""
    return ReplaceNote(
        frontmatter={**note.frontmatter, **frontmatter},
        body=note.body + "\n- Corrected on review\n",
    )


async def test_a_stale_etag_cannot_overwrite_an_edit_made_on_a_device(
    store: LocalStore, session_factory
):
    """T-CE-1. A person edits the file between the client's read and its write.

    The client's ETag names bytes that are no longer there, so the write is
    refused with the ETag the file has now, and the person's edit survives.
    """
    created = await store.create_note(CreateNote(title="Runbook", frontmatter=MEETING))
    path = store.notes_root / created.path
    on_device = path.read_bytes().replace(b"reviewed: false", b"reviewed: true")
    path.write_bytes(on_device)

    with pytest.raises(VersionConflict) as raised:
        await store.replace_note(created.id, edited(created), created.content_hash)

    assert raised.value.current_etag == content_hash(on_device)
    assert path.read_bytes() == on_device
    async with session_factory() as session:
        row = (await session.execute(sa.select(Note).where(Note.id == created.id))).scalar_one()
    assert row.content_hash == created.content_hash


async def test_a_current_etag_replaces_the_file_first_and_the_row_second(
    store: LocalStore, session_factory
):
    """Read, edit on a device, read again, write with the fresh ETag: it lands.

    The file is rewritten in place under the same identifier and path, dates
    stay YAML dates rather than becoming quoted strings, including a key the
    schema does not know, and the mirror row follows the file.
    """
    created = await store.create_note(
        CreateNote(title="Ameren Architecture Sync", body="## Key points\n", frontmatter=MEETING)
    )
    path = store.notes_root / created.path
    path.write_bytes(
        path.read_bytes().replace(b"reviewed: false", b"reviewed: true\ndue: 2026-09-10")
    )
    current = await store.get_note(created.id)
    assert current.content_hash != created.content_hash

    replaced = await store.replace_note(created.id, edited(current), current.content_hash)

    assert replaced.id == created.id
    assert replaced.path == created.path
    on_disk = path.read_bytes()
    assert content_hash(on_disk) == replaced.content_hash
    assert replaced.content_hash != current.content_hash
    frontmatter, body = fm.parse(on_disk.decode("utf-8"))
    assert frontmatter["id"] == created.id
    assert frontmatter["reviewed"] is True
    assert b"date: 2026-09-08\n" in on_disk
    assert b"due: 2026-09-10\n" in on_disk
    assert body.endswith("- Corrected on review\n")
    async with session_factory() as session:
        row = (await session.execute(sa.select(Note).where(Note.id == created.id))).scalar_one()
    assert row.content_hash == replaced.content_hash
    assert row.reviewed is True
    assert row.size_bytes == len(on_disk)

    with pytest.raises(VersionConflict) as raised:
        await store.replace_note(created.id, edited(current), current.content_hash)
    assert raised.value.current_etag == replaced.content_hash


async def test_a_value_corrected_from_1_to_true_lands_as_true(store: LocalStore):
    """A file carrying `reviewed: 1` corrected to `reviewed: true` is written as true."""
    created = await store.create_note(CreateNote(title="Runbook", frontmatter=MEETING))
    path = store.notes_root / created.path
    path.write_bytes(path.read_bytes().replace(b"reviewed: false", b"reviewed: 1"))
    current = await store.get_note(created.id)

    replaced = await store.replace_note(
        created.id, edited(current, reviewed=True), current.content_hash
    )

    frontmatter, _ = fm.parse(path.read_bytes().decode("utf-8"))
    assert frontmatter["reviewed"] is True
    assert replaced.frontmatter["reviewed"] is True


async def test_an_edit_delivered_while_the_row_update_waits_is_not_overwritten(
    store: LocalStore, session_factory, monkeypatch
):
    """T-CE-1 at its narrowest: the device edit lands while PostgreSQL is being asked."""
    created = await store.create_note(CreateNote(title="Runbook", frontmatter=MEETING))
    path = store.notes_root / created.path
    on_device = path.read_bytes().replace(b"reviewed: false", b"reviewed: true")
    execute = AsyncSession.execute

    async def delivered_during_the_update(self, statement, *args, **kwargs):
        result = await execute(self, statement, *args, **kwargs)
        if isinstance(statement, sa.Update):
            path.write_bytes(on_device)
        return result

    monkeypatch.setattr(AsyncSession, "execute", delivered_during_the_update)
    with pytest.raises(VersionConflict) as raised:
        await store.replace_note(created.id, edited(created), created.content_hash)

    assert raised.value.current_etag == content_hash(on_device)
    assert path.read_bytes() == on_device
    async with session_factory() as session:
        row = (await session.execute(sa.select(Note).where(Note.id == created.id))).scalar_one()
    assert row.content_hash == created.content_hash


async def test_two_writers_holding_the_same_etag_cannot_both_win(store: LocalStore):
    """The compare and the write are one step under the note's lock."""
    created = await store.create_note(CreateNote(title="Runbook", frontmatter=MEETING))
    first = ReplaceNote(frontmatter=created.frontmatter, body="# Runbook\n\nfirst\n")
    second = ReplaceNote(frontmatter=created.frontmatter, body="# Runbook\n\nsecond\n")

    outcomes = await asyncio.gather(
        store.replace_note(created.id, first, created.content_hash),
        store.replace_note(created.id, second, created.content_hash),
        return_exceptions=True,
    )

    winners = [outcome for outcome in outcomes if not isinstance(outcome, BaseException)]
    losers = [outcome for outcome in outcomes if isinstance(outcome, VersionConflict)]
    assert len(winners) == 1 and len(losers) == 1
    on_disk = (store.notes_root / created.path).read_bytes()
    assert content_hash(on_disk) == winners[0].content_hash == losers[0].current_etag


async def test_a_replace_during_an_outage_is_refused_before_the_file_is_touched(
    store: LocalStore, unreachable_store: LocalStore
):
    """Both stores share the notes filesystem; only the second has lost PostgreSQL."""
    created = await store.create_note(CreateNote(title="Runbook", frontmatter=MEETING))
    path = store.notes_root / created.path
    before = path.read_bytes()

    with pytest.raises(MetadataUnavailable):
        await unreachable_store.replace_note(created.id, edited(created), created.content_hash)
    assert path.read_bytes() == before


async def test_a_replace_keeps_the_identifier_and_checks_the_schema(store: LocalStore):
    created = await store.create_note(CreateNote(title="Runbook", frontmatter=MEETING))
    path = store.notes_root / created.path
    before = path.read_bytes()

    with pytest.raises(ValidationFailed) as changed_id:
        await store.replace_note(
            created.id, edited(created, id="01K4Q8Z3N7V2X9M1B5C6D8E0F2"), created.content_hash
        )
    assert changed_id.value.errors == ["id: the identifier of a note cannot be changed"]

    with pytest.raises(ValidationFailed) as missing_account:
        await store.replace_note(created.id, edited(created, account=None), created.content_hash)
    assert any("account" in problem for problem in missing_account.value.errors)
    assert path.read_bytes() == before


async def test_a_replace_of_a_row_whose_file_is_now_another_note_is_a_miss(
    store: LocalStore,
):
    """A miss whatever the ETag, and the other note's file is untouched.

    The ETag from the earlier read must not come back as a conflict naming
    the other note's hash, and a hash computed off the volume must not
    overwrite that note.
    """
    runbook = await store.create_note(CreateNote(title="Runbook", frontmatter=MEETING))
    meeting = await store.create_note(CreateNote(title="Meeting", frontmatter=MEETING))
    (store.notes_root / runbook.path).unlink()
    (store.notes_root / meeting.path).rename(store.notes_root / runbook.path)
    swapped = (store.notes_root / runbook.path).read_bytes()

    for etag in (runbook.content_hash, content_hash(swapped)):
        with pytest.raises(NotFound):
            await store.replace_note(runbook.id, edited(runbook), etag)
    assert (store.notes_root / runbook.path).read_bytes() == swapped


async def test_public_replace_carries_the_etag_in_both_directions(store: LocalStore):
    """The public route against the real store: 428, then 409, then 200."""
    app, authorization = authorized_api(store)
    created = await store.create_note(CreateNote(title="Runbook", frontmatter=MEETING))
    path = store.notes_root / created.path
    path.write_bytes(path.read_bytes().replace(b"reviewed: false", b"reviewed: true"))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api"
    ) as client:
        client.headers.update(authorization)
        read = await client.get(f"/v1/notes/{created.id}")
        document = read.json()
        document["body"] += "\n- Corrected on review\n"

        unconditional = await client.put(f"/v1/notes/{created.id}", json=document)
        assert unconditional.status_code == 428

        stale = await client.put(
            f"/v1/notes/{created.id}",
            json=document,
            headers={"If-Match": f'"{created.content_hash}"'},
        )
        assert stale.status_code == 409
        assert stale.json()["error"] == "version_conflict"
        assert f'"{stale.json()["current_version"]}"' == read.headers["etag"]

        fresh = await client.put(
            f"/v1/notes/{created.id}", json=document, headers={"If-Match": read.headers["etag"]}
        )
        assert fresh.status_code == 200
        assert fresh.headers["etag"] == f'"{content_hash(path.read_bytes())}"'
        assert fresh.headers["etag"] != read.headers["etag"]


async def test_an_edit_that_lands_while_the_new_bytes_are_staged_is_not_overwritten(
    store: LocalStore, monkeypatch: pytest.MonkeyPatch
):
    """Obsidian Sync writes the file after the first compare passed.

    The new bytes are staged before the last compare, so the write that lands
    during staging is seen by that compare: the replace is refused with the
    hash of the edit, the edit stays on disk, and no staged file is left in
    the folder for Obsidian Sync to carry to every device.
    """
    created = await store.create_note(CreateNote(title="Runbook", frontmatter=MEETING))
    path = store.notes_root / created.path
    on_device = path.read_bytes().replace(b"reviewed: false", b"reviewed: true")
    real_stage = notes_module.stage_bytes

    def stage_after_a_device_write(target, data, **kwargs):
        path.write_bytes(on_device)
        return real_stage(target, data, **kwargs)

    monkeypatch.setattr(notes_module, "stage_bytes", stage_after_a_device_write)

    with pytest.raises(VersionConflict) as raised:
        await store.replace_note(created.id, edited(created), created.content_hash)

    assert raised.value.current_etag == content_hash(on_device)
    assert path.read_bytes() == on_device
    assert sorted(entry.name for entry in path.parent.iterdir()) == [path.name]


async def test_a_frontmatter_patch_changes_one_line_and_keeps_the_body_bytes(
    store: LocalStore, session_factory
):
    """The targeted review action preserves hand ordering, comments and the body."""
    created = await store.create_note(CreateNote(title="Runbook", frontmatter=MEETING))
    path = store.notes_root / created.path
    body = b"# Runbook\r\n\r\nOperator wording and  trailing spaces  \r\n"
    before = (
        b"---\n"
        b"tags: [architecture]\n"
        b"# keep this explanation beside the permanent identifier\n"
        + f"id: {created.id}\n".encode()
        + b"reviewed: false\n"
        b"context: customer\n"
        b"account: Ameren\n"
        b"date: 2026-09-08\n"
        b"schema_version: 1\n"
        b"sources: []\n"
        b"type: meeting\n"
        b"---\n" + body
    )
    path.write_bytes(before)

    patched = await store.patch_frontmatter(
        created.id,
        PatchFrontmatter(set={"reviewed": True}),
        content_hash(before),
    )

    after = path.read_bytes()
    assert after == before.replace(b"reviewed: false\n", b"reviewed: true\n", 1)
    assert fm.split(after.decode("utf-8"))[1].encode() == body
    assert patched.frontmatter["reviewed"] is True
    async with session_factory() as session:
        row = (await session.execute(sa.select(Note).where(Note.id == created.id))).scalar_one()
    assert row.reviewed is True
    assert row.content_hash == content_hash(after)


async def test_a_stale_frontmatter_patch_changes_nothing(store: LocalStore):
    created = await store.create_note(CreateNote(title="Runbook", frontmatter=MEETING))
    path = store.notes_root / created.path
    on_device = path.read_bytes().replace(b"reviewed: false", b"reviewed: true")
    path.write_bytes(on_device)

    with pytest.raises(VersionConflict) as raised:
        await store.patch_frontmatter(
            created.id,
            PatchFrontmatter(set={"reviewed": True}),
            created.content_hash,
        )

    assert raised.value.current_etag == content_hash(on_device)
    assert path.read_bytes() == on_device


async def test_an_invalid_frontmatter_patch_names_the_field_and_changes_nothing(
    store: LocalStore,
):
    created = await store.create_note(CreateNote(title="Runbook", frontmatter=MEETING))
    path = store.notes_root / created.path
    before = path.read_bytes()

    with pytest.raises(ValidationFailed) as raised:
        await store.patch_frontmatter(
            created.id,
            PatchFrontmatter(set={"type": "incident"}),
            created.content_hash,
        )

    assert raised.value.errors == [
        "type: 'incident' is not one of meeting, journal, reference, note"
    ]
    assert path.read_bytes() == before

    with pytest.raises(ValidationFailed) as changed_id:
        await store.patch_frontmatter(
            created.id,
            PatchFrontmatter(set={"id": "01K4Q8Z3N7V2X9M1B5C6D8E0F2"}),
            created.content_hash,
        )
    assert changed_id.value.errors == ["id: the identifier of a note cannot be changed"]
    assert path.read_bytes() == before


async def test_a_frontmatter_patch_can_unset_an_optional_field(store: LocalStore):
    created = await store.create_note(CreateNote(title="Runbook", frontmatter=MEETING))
    path = store.notes_root / created.path
    before_body = fm.split(path.read_text(encoding="utf-8"))[1]

    patched = await store.patch_frontmatter(
        created.id,
        PatchFrontmatter(set={"context": "internal"}, unset=["account"]),
        created.content_hash,
    )

    after = path.read_text(encoding="utf-8")
    assert "account:" not in fm.split(after)[0]
    assert fm.split(after)[1] == before_body
    assert "account" not in patched.frontmatter


async def test_a_patched_date_lands_as_a_date_not_quoted_text(store: LocalStore, session_factory):
    """A date key set through the patch reads as a date, the way every write path writes one."""
    created = await store.create_note(CreateNote(title="Runbook", frontmatter=MEETING))
    path = store.notes_root / created.path

    patched = await store.patch_frontmatter(
        created.id,
        PatchFrontmatter(set={"date": "2026-09-10"}),
        created.content_hash,
    )

    assert "date: 2026-09-10\n" in fm.split(path.read_text(encoding="utf-8"))[0]
    assert patched.frontmatter["date"] == "2026-09-10"
    async with session_factory() as session:
        row = (await session.execute(sa.select(Note).where(Note.id == created.id))).scalar_one()
    assert row.date == date(2026, 9, 10)


async def test_a_patch_that_changes_nothing_leaves_the_file_alone(store: LocalStore):
    """An idempotent client re-marking a reviewed note must not make Obsidian Sync push it."""
    created = await store.create_note(CreateNote(title="Runbook", frontmatter=MEETING))
    path = store.notes_root / created.path
    before = path.read_bytes()
    stat_before = path.stat()

    patched = await store.patch_frontmatter(
        created.id,
        PatchFrontmatter(set={"reviewed": False}),
        created.content_hash,
    )

    stat_after = path.stat()
    assert path.read_bytes() == before
    assert stat_after.st_ino == stat_before.st_ino
    assert stat_after.st_mtime_ns == stat_before.st_mtime_ns
    assert patched.content_hash == created.content_hash


async def test_a_no_op_patch_repairs_a_stale_mirror(store: LocalStore, session_factory):
    """A retry repairs a row left behind when the earlier file write committed first."""
    created = await store.create_note(CreateNote(title="Runbook", frontmatter=MEETING))
    path = store.notes_root / created.path
    current_data = path.read_bytes().replace(b"reviewed: false", b"reviewed: true")
    path.write_bytes(current_data)
    stat_before = path.stat()

    patched = await store.patch_frontmatter(
        created.id,
        PatchFrontmatter(set={"reviewed": True}),
        content_hash(current_data),
    )

    assert path.read_bytes() == current_data
    assert path.stat().st_mtime_ns == stat_before.st_mtime_ns
    assert patched.content_hash == content_hash(current_data)
    async with session_factory() as session:
        row = (await session.execute(sa.select(Note).where(Note.id == created.id))).scalar_one()
    assert row.reviewed is True
    assert row.frontmatter["reviewed"] is True
    assert row.content_hash == content_hash(current_data)


async def test_a_no_op_patch_answers_from_the_verified_snapshot(
    store: LocalStore, monkeypatch: pytest.MonkeyPatch
):
    """A device write after verification cannot replace the response with newer contents."""
    created = await store.create_note(CreateNote(title="Runbook", frontmatter=MEETING))
    path = store.notes_root / created.path
    before = path.read_bytes()
    on_device = before.replace(b"# Runbook", b"# Device edit")
    execute = AsyncSession.execute

    async def device_write_during_mirror_update(self, statement, *args, **kwargs):
        result = await execute(self, statement, *args, **kwargs)
        if isinstance(statement, sa.Update):
            path.write_bytes(on_device)
        return result

    monkeypatch.setattr(AsyncSession, "execute", device_write_during_mirror_update)
    patched = await store.patch_frontmatter(
        created.id,
        PatchFrontmatter(set={"reviewed": False}),
        created.content_hash,
    )

    assert patched.body == created.body
    assert patched.title == created.title
    assert patched.content_hash == created.content_hash
    assert path.read_bytes() == on_device


@pytest.mark.parametrize(
    "patch",
    [PatchFrontmatter(set={"sources": []}), PatchFrontmatter(unset=["sources"])],
)
async def test_patching_source_associations_is_refused_and_changes_nothing(
    store: LocalStore, patch: PatchFrontmatter
):
    created = await store.create_note(CreateNote(title="Runbook", frontmatter=MEETING))
    path = store.notes_root / created.path
    before = path.read_bytes()

    with pytest.raises(ValidationFailed) as raised:
        await store.patch_frontmatter(created.id, patch, created.content_hash)

    assert raised.value.errors[0] == (
        "sources: source associations are managed by ingest and cannot be patched"
    )
    assert path.read_bytes() == before


async def test_unsetting_a_required_field_is_refused_and_changes_nothing(store: LocalStore):
    created = await store.create_note(CreateNote(title="Runbook", frontmatter=MEETING))
    path = store.notes_root / created.path
    before = path.read_bytes()

    with pytest.raises(ValidationFailed) as raised:
        await store.patch_frontmatter(
            created.id,
            PatchFrontmatter(unset=["type"]),
            created.content_hash,
        )

    assert raised.value.errors == ["type: required, so it cannot be removed"]
    assert path.read_bytes() == before


async def test_unsetting_the_identifier_is_refused_and_changes_nothing(store: LocalStore):
    created = await store.create_note(CreateNote(title="Runbook", frontmatter=MEETING))
    path = store.notes_root / created.path
    before = path.read_bytes()

    with pytest.raises(ValidationFailed) as raised:
        await store.patch_frontmatter(
            created.id,
            PatchFrontmatter(unset=["id"]),
            created.content_hash,
        )

    assert raised.value.errors == ["id: the identifier of a note cannot be removed"]
    assert path.read_bytes() == before


async def test_a_key_named_in_both_set_and_unset_is_refused_and_changes_nothing(
    store: LocalStore,
):
    created = await store.create_note(CreateNote(title="Runbook", frontmatter=MEETING))
    path = store.notes_root / created.path
    before = path.read_bytes()

    with pytest.raises(ValidationFailed) as raised:
        await store.patch_frontmatter(
            created.id,
            PatchFrontmatter(set={"account": "Ameren"}, unset=["account"]),
            created.content_hash,
        )

    assert raised.value.errors == ["account: named in both set and unset"]
    assert path.read_bytes() == before


async def test_a_null_value_in_set_is_refused_and_changes_nothing(store: LocalStore):
    """Removing a key has one name: a null in set would leave an empty property behind."""
    created = await store.create_note(CreateNote(title="Runbook", frontmatter=MEETING))
    path = store.notes_root / created.path
    before = path.read_bytes()

    with pytest.raises(ValidationFailed) as raised:
        await store.patch_frontmatter(
            created.id,
            PatchFrontmatter(set={"account": None}),
            created.content_hash,
        )

    assert raised.value.errors == [
        "account: null is not a value to write; name the key in unset to remove it"
    ]
    assert path.read_bytes() == before


async def test_a_patch_keeps_a_list_a_person_wrote_flush_with_its_key(store: LocalStore):
    """A hand written note must differ in the patched key alone, not in its list style."""
    created = await store.create_note(CreateNote(title="Runbook", frontmatter=MEETING))
    path = store.notes_root / created.path
    hand_written = path.read_text(encoding="utf-8").replace("  - architecture", "- architecture")
    path.write_text(hand_written, encoding="utf-8")

    patched = await store.patch_frontmatter(
        created.id,
        PatchFrontmatter(set={"reviewed": True}),
        content_hash(hand_written.encode("utf-8")),
    )

    after = path.read_text(encoding="utf-8")
    assert "\n- architecture\n" in after
    assert [line for line in after.splitlines() if line not in hand_written.splitlines()] == [
        "reviewed: true"
    ]
    assert patched.frontmatter["reviewed"] is True
