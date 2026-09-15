"""Source ingest ordering across the notes filesystem and PostgreSQL."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import coppermind_store.sources as sources_module
import pytest
import sqlalchemy as sa
from coppermind_store import projections as projections_module
from coppermind_store.notes import LocalStore

from coppermind import frontmatter as fm
from coppermind.db.models import Note, NoteSource, Source, SourceArtifact, SourceRevision
from coppermind.store_protocol import (
    CreateNote,
    IncompleteRevision,
    IngestArtifact,
    IngestRequest,
    MetadataUnavailable,
    NotesFilesystemUnavailable,
    PathCollision,
    PayloadTooLarge,
    ProjectionNotPlaced,
    SourceClaimMissing,
    SourceNotFound,
    SourcesFilesystemUnavailable,
)


def projection_text(store: LocalStore, relative: str) -> str:
    """The generated projection as the captain's vault holds it.

    The file is this increment's public output: it is what he opens on his
    phone with the service down, so it is read here from the notes filesystem
    rather than through any route.
    """
    return (store.notes_root / relative).read_text(encoding="utf-8")


def sample(external_id: str = "rec_8f3a2c19") -> IngestRequest:
    return IngestRequest.model_validate(
        {
            "source": {
                "provider": "plaud",
                "external_source_id": external_id,
                "source_type": "transcript",
                "origin": "Plaud NotePin",
                "captured_at": "2026-09-08T14:02:11-05:00",
                "metadata": {"duration_s": 2711, "language": "en"},
                "artifacts": [
                    {
                        "name": "transcript.txt",
                        "mime_type": "text/plain",
                        "content": "Scott: Let's start with the architecture review...",
                    },
                    {
                        "name": "plaud-summary.md",
                        "mime_type": "text/markdown",
                        "content": "## Summary\n- Reviewed the target architecture...",
                    },
                ],
            },
            "note": {
                "title": "Ameren Architecture Sync",
                "body": "## Key points\n- Target architecture agreed\n",
                "frontmatter": {
                    "date": "2026-09-08",
                    "type": "meeting",
                    "context": "customer",
                    "account": "Ameren",
                    "tags": ["architecture", "vcf"],
                },
            },
        }
    )


async def test_ingest_writes_an_immutable_bundle_linked_to_one_review_note(
    store: LocalStore, session_factory
):
    result = await store.ingest(sample())
    assert result.source.revision == 1
    assert result.source.created is True
    assert result.note.created is True
    assert result.projection_path == ("_Sources/Plaud/2026-09-08 Ameren Architecture Sync.md")

    source_root = store.sources_root / result.source.id
    manifest = json.loads((source_root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["source_id"] == result.source.id
    assert manifest["external_source_id"] == "rec_8f3a2c19"
    assert manifest["current_revision"] == 1
    assert [item["name"] for item in manifest["revisions"][0]["artifacts"]] == [
        "transcript.txt",
        "plaud-summary.md",
    ]
    assert (
        (source_root / "r0001" / "transcript.txt").read_text(encoding="utf-8").startswith("Scott:")
    )
    claims = list(store.sources_root.glob(".external-id-*.json"))
    assert len(claims) == 1
    claim = json.loads(claims[0].read_text(encoding="utf-8"))
    assert claim == {
        "schema_version": 1,
        "provider": "plaud",
        "external_source_id": "rec_8f3a2c19",
        "source_id": result.source.id,
    }

    note_path = store.notes_root / result.note.path
    frontmatter, body = fm.parse(note_path.read_text(encoding="utf-8"))
    assert result.note.path == "Review/2026-09-08 Ameren Architecture Sync.md"
    assert frontmatter["sources"] == [result.source.id]
    assert body.startswith("# Ameren Architecture Sync")
    projection = store.notes_root / result.projection_path
    projection_frontmatter, projection_body = fm.parse(projection.read_text(encoding="utf-8"))
    assert projection_frontmatter["managed"] is True
    assert projection_frontmatter["source_id"] == result.source.id
    assert projection_frontmatter["source_revision"] == 1
    assert "Scott: Let's start with the architecture review..." in projection_body
    assert "Reviewed the target architecture" in projection_body

    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Source)) == 1
        assert await session.scalar(sa.select(sa.func.count()).select_from(SourceRevision)) == 1
        assert await session.scalar(sa.select(sa.func.count()).select_from(SourceArtifact)) == 2
        link = (await session.execute(sa.select(NoteSource))).scalar_one()
        assert link.note_id == result.note.id
        assert link.source_id == result.source.id
        assert await session.scalar(sa.select(sa.func.count()).select_from(Note)) == 1


async def test_t_ing_1_identical_replay_returns_the_original_without_duplicating(
    store: LocalStore, session_factory
):
    first = await store.ingest(sample())
    before = {
        path.relative_to(store.notes_root.parent): path.read_bytes()
        for path in store.notes_root.parent.rglob("*")
        if path.is_file()
    }

    replay = await store.ingest(sample())

    after = {
        path.relative_to(store.notes_root.parent): path.read_bytes()
        for path in store.notes_root.parent.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert replay.source.id == first.source.id
    assert replay.source.revision == 1
    assert replay.source.created is False
    assert replay.note.id == first.note.id
    assert replay.note.path == first.note.path
    assert replay.note.created is False
    assert [path.name for path in store.sources_root.iterdir() if path.is_dir()] == [
        first.source.id
    ]
    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Source)) == 1
        assert await session.scalar(sa.select(sa.func.count()).select_from(Note)) == 1


async def test_identical_replay_recognizes_the_previous_identity_framing(store: LocalStore):
    """Bundles written by the merged ingest slice must not gain a false revision."""
    first = await store.ingest(sample())
    manifest_path = store.sources_root / first.source.id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifacts = manifest["revisions"][0]["artifacts"]
    legacy_preimage = "\n".join(sorted(f"{item['name']}:{item['sha256']}" for item in artifacts))
    manifest["revisions"][0]["content_identity"] = hashlib.sha256(
        legacy_preimage.encode("utf-8")
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    replay = await store.ingest(sample())

    assert replay.source.id == first.source.id
    assert replay.source.revision == 1
    assert replay.source.created is False
    assert not (store.sources_root / first.source.id / "r0002").exists()


async def test_oversize_ingest_is_refused_before_files_or_rows_are_written(
    store: LocalStore, session_factory
):
    body = dict(store.control.store.read("settings").body)
    body["limits"]["ingest_max_bytes"] = 512
    store.control.store.write("settings", body, if_revision=1)
    request = sample()
    request.source.artifacts[0].content = "x" * 1024

    with pytest.raises(PayloadTooLarge) as raised:
        await store.ingest(request)

    assert raised.value.limit_bytes == 512
    assert list(store.notes_root.rglob("*.md")) == []
    assert not store.sources_root.exists()
    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Source)) == 0
        assert await session.scalar(sa.select(sa.func.count()).select_from(Note)) == 0


async def test_concurrent_duplicate_is_serialised_by_the_filesystem_claim(
    store: LocalStore, session_factory
):
    """The claim and its lock produce one create and one successful replay."""
    outcomes = await asyncio.gather(store.ingest(sample()), store.ingest(sample()))
    assert sorted(item.source.created for item in outcomes) == [False, True]
    assert outcomes[0].source.id == outcomes[1].source.id
    winner = next(item for item in outcomes if item.source.created)
    assert [path.name for path in store.sources_root.iterdir() if path.is_dir()] == [
        winner.source.id
    ]
    assert len(list((store.notes_root / "Review").glob("*.md"))) == 1
    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Source)) == 1
        assert await session.scalar(sa.select(sa.func.count()).select_from(Note)) == 1


class InterruptedIngest(BaseException):
    pass


async def test_interruption_before_the_note_leaves_no_bundle_or_rows(
    store: LocalStore, session_factory, monkeypatch: pytest.MonkeyPatch
):
    real_create = sources_module.create_exclusive_bytes

    def interrupt_note_write(path, data, **kwargs):
        # The claim, both artifacts and the manifest have landed. Interrupt
        # the final note write and verify no claim or bundle survives it.
        if path.is_relative_to(store.notes_root):
            raise InterruptedIngest()
        return real_create(path, data, **kwargs)

    monkeypatch.setattr(sources_module, "create_exclusive_bytes", interrupt_note_write)

    with pytest.raises(InterruptedIngest):
        await store.ingest(sample())

    assert list(store.notes_root.rglob("*.md")) == []
    assert list(store.sources_root.iterdir()) == []
    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Source)) == 0
        assert await session.scalar(sa.select(sa.func.count()).select_from(Note)) == 0

    monkeypatch.setattr(sources_module, "create_exclusive_bytes", real_create)
    retried = await store.ingest(sample())
    assert (store.sources_root / retried.source.id / "manifest.json").is_file()


async def test_commit_failure_retry_resolves_the_durable_claim_and_repairs_the_mirror(
    store: LocalStore, session_factory, monkeypatch: pytest.MonkeyPatch
):
    real_transaction = sources_module.transaction

    @asynccontextmanager
    async def fail_at_commit(factory):
        async with real_transaction(factory) as session:
            yield session
            raise sa.exc.OperationalError("COMMIT", {}, OSError("connection lost"))

    monkeypatch.setattr(sources_module, "transaction", fail_at_commit)
    with pytest.raises(MetadataUnavailable):
        await store.ingest(sample())

    assert len(list(store.sources_root.glob(".external-id-*.json"))) == 1
    assert len([path for path in store.sources_root.iterdir() if path.is_dir()]) == 1
    assert len(list((store.notes_root / "Review").glob("*.md"))) == 1
    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Source)) == 0
        assert await session.scalar(sa.select(sa.func.count()).select_from(Note)) == 0

    monkeypatch.setattr(sources_module, "transaction", real_transaction)
    replay = await store.ingest(sample())

    assert replay.source.created is False
    assert replay.source.revision == 1
    assert replay.note.created is False
    assert len([path for path in store.sources_root.iterdir() if path.is_dir()]) == 1
    assert len(list((store.notes_root / "Review").glob("*.md"))) == 1
    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Source)) == 1
        assert await session.scalar(sa.select(sa.func.count()).select_from(SourceRevision)) == 1
        assert await session.scalar(sa.select(sa.func.count()).select_from(Note)) == 1


async def test_t_ing_2_changed_content_appends_a_revision_and_keeps_the_review_note(
    store: LocalStore, session_factory
):
    first_request = sample()
    first = await store.ingest(first_request)
    note_path = store.notes_root / first.note.path
    edited = fm.patch(note_path.read_text(encoding="utf-8"), {"reviewed": True})
    edited = edited.replace("Target architecture agreed", "Captain's correction")
    note_path.write_text(edited, encoding="utf-8")
    note_bytes = note_path.read_bytes()
    projection_path = store.notes_root / first.projection_path
    projection_path.write_text(
        projection_path.read_text(encoding="utf-8") + "Person's projection edit\n",
        encoding="utf-8",
    )

    changed = sample()
    changed.source.artifacts[0].content = "Scott: corrected source content"
    second = await store.ingest(changed)

    assert second.source.id == first.source.id
    assert second.source.revision == 2
    assert second.source.created is True
    assert second.note.id == first.note.id
    assert second.note.path == first.note.path
    assert second.note.created is False
    assert note_path.read_bytes() == note_bytes
    assert second.projection_path == first.projection_path
    projection_frontmatter, projection_body = fm.parse(projection_path.read_text(encoding="utf-8"))
    assert projection_frontmatter["source_revision"] == 2
    assert "Scott: corrected source content" in projection_body
    assert "Let's start with the architecture review" not in projection_body
    assert "Person's projection edit" not in projection_body
    source_root = store.sources_root / first.source.id
    assert (source_root / "r0001" / "transcript.txt").read_text() == (
        "Scott: Let's start with the architecture review..."
    )
    assert (source_root / "r0002" / "transcript.txt").read_text() == (
        "Scott: corrected source content"
    )
    manifest = json.loads((source_root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["current_revision"] == 2
    assert [item["revision"] for item in manifest["revisions"]] == [1, 2]
    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Source)) == 1
        assert await session.scalar(sa.select(sa.func.count()).select_from(SourceRevision)) == 2
        assert await session.scalar(sa.select(sa.func.count()).select_from(Note)) == 1


async def test_t_src_1_every_stored_source_revision_is_readable(store: LocalStore):
    request = sample()
    request.source.artifacts.append(
        IngestArtifact(
            name="recording.bin",
            mime_type="application/octet-stream",
            content_base64="AAEC",
        )
    )
    ingested = await store.ingest(request)

    source = await store.get_source(ingested.source.id)
    assert source.current_revision == 1
    assert source.revisions[0].artifacts[-1].size_bytes == 3
    text = await store.get_source_artifact(ingested.source.id, 1, "transcript.txt")
    assert text.content == "Scott: Let's start with the architecture review..."
    binary = await store.get_source_artifact(ingested.source.id, 1, "recording.bin")
    assert binary.content is None
    assert binary.size_bytes == 3
    assert binary.sha256 == hashlib.sha256(b"\x00\x01\x02").hexdigest()
    projection = projection_text(store, ingested.projection_path)
    assert "Binary artifact: application/octet-stream, 3 bytes" in projection


async def test_a_text_typed_artifact_that_is_not_utf8_is_described_not_refused(
    store: LocalStore,
):
    """A client's declared type is a claim about bytes the contract never checks.

    Undecodable bytes are a payload the projection describes instead of reading;
    they are not a notes filesystem outage, and they must not block the ingest.
    """
    request = sample()
    request.source.artifacts[0] = IngestArtifact(
        name="transcript.txt",
        mime_type="text/plain",
        content_base64=base64.b64encode("Sch\u00f6n".encode("latin-1")).decode("ascii"),
    )

    ingested = await store.ingest(request)

    artifact = await store.get_source_artifact(ingested.source.id, 1, "transcript.txt")
    assert artifact.content is None
    assert artifact.size_bytes == 5
    assert "Binary artifact: text/plain, 5 bytes" in projection_text(
        store, ingested.projection_path
    )


async def test_a_new_revision_never_writes_over_another_source_projection(store: LocalStore):
    """A recorded path is only reused while it still holds this source's output.

    Projections live in the captain's vault, so a path can be freed and taken
    by another source. Replacing whatever sits there would delete a file the
    manifest of a different source still points at, with nothing reporting it.
    """
    first = await store.ingest(sample("rec_first"))
    (store.notes_root / first.projection_path).unlink()
    other = await store.ingest(sample("rec_other"))
    assert other.projection_path == first.projection_path

    changed = sample("rec_first")
    changed.source.artifacts[0].content = "Scott: corrected source content"
    second = await store.ingest(changed)

    assert second.source.revision == 2
    assert second.projection_path != first.projection_path
    kept = projection_text(store, other.projection_path)
    assert f"source_id: {other.source.id}" in kept
    assert "Scott: Let's start with the architecture review..." in kept
    assert "Scott: corrected source content" in projection_text(store, second.projection_path)


async def test_a_volume_fault_on_a_source_read_is_an_outage_not_a_missing_source(
    store: LocalStore,
):
    """A bundle the store cannot read is not a bundle that is gone.

    An operator told 404 concludes the immutable source was lost. The manifest
    here is intact but unreadable, which is the mount fault the contract
    already has a typed answer for.
    """
    ingested = await store.ingest(sample())
    manifest_path = store.sources_root / ingested.source.id / "manifest.json"
    manifest_path.unlink()
    manifest_path.mkdir()

    with pytest.raises(SourcesFilesystemUnavailable):
        await store.get_source(ingested.source.id)
    with pytest.raises(SourcesFilesystemUnavailable):
        await store.get_source_artifact(ingested.source.id, 1, "transcript.txt")
    with pytest.raises(SourceNotFound):
        await store.get_source("01K4Q8Z2A0P1Q2R3S4T5U6V7W8")


async def test_a_notes_fault_during_a_new_revision_leaves_nothing_to_clean_up_by_hand(
    store: LocalStore, monkeypatch: pytest.MonkeyPatch
):
    """A revision no manifest names is removed however the attempt failed.

    The notes volume is consulted while the revision directory is on disk but
    not yet recorded. A blip there must not leave a half revision that the next
    attempt refuses as incomplete until an operator removes it.
    """
    first = await store.ingest(sample())
    changed = sample()
    changed.source.artifacts[0].content = "Scott: corrected source content"

    def notes_volume_blip(*_args, **_kwargs):
        raise NotesFilesystemUnavailable("the notes filesystem went away")

    monkeypatch.setattr(sources_module, "projection_revision", notes_volume_blip)
    with pytest.raises(NotesFilesystemUnavailable):
        await store.ingest(changed)

    assert not (store.sources_root / first.source.id / "r0002").exists()
    monkeypatch.undo()
    retried = await store.ingest(changed)
    assert retried.source.revision == 2
    assert retried.source.created is True


async def test_the_note_and_its_projection_take_their_date_from_one_rule(store: LocalStore):
    """The Review note and the projection are one recording under one day.

    The date key's kind is operator-configurable control state, and a schema
    that does not call it a date leaves the value the text the client sent. A
    second, narrower reading of that value would file the two files under
    different days and name the same recording twice.
    """
    schema = store.control.store.read("schema")
    body = dict(schema.body)
    body["keys"] = [
        {**key, "kind": "string"} if key["name"] == "date" else key for key in body["keys"]
    ]
    store.control.store.write("schema", body, if_revision=schema.revision)

    ingested = await store.ingest(sample())

    assert ingested.note.path == "Review/2026-09-08 Ameren Architecture Sync.md"
    assert ingested.projection_path == "_Sources/Plaud/2026-09-08 Ameren Architecture Sync.md"


async def test_a_taken_projection_path_on_a_first_ingest_is_answered_as_a_collision(
    store: LocalStore, session_factory, monkeypatch: pytest.MonkeyPatch
):
    """A source that never landed is not reported as a revision that did.

    Two recordings of one title on one day pick the same page name, and a
    device can deliver a note onto it just as readily. Generated output never
    writes over a file this store did not generate, and on a first ingest
    nothing durable survives the refusal, so the answer is the collision every
    other taken path gets.
    """
    mine = "---\nid: 01K4Q8Z3N7V2X9M1B5C6D8E0F2\n---\n# Mine now\n"
    real_choose = projections_module.new_projection_path
    chosen: list[str] = []

    def choose_then_the_path_is_taken(notes_root, settings, **kwargs):
        relative = real_choose(notes_root, settings, **kwargs)
        target = notes_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(mine, encoding="utf-8")
        chosen.append(relative)
        return relative

    monkeypatch.setattr(sources_module, "new_projection_path", choose_then_the_path_is_taken)

    with pytest.raises(PathCollision) as refused:
        await store.ingest(sample())

    assert refused.value.existing_path == chosen[0]
    assert (store.notes_root / chosen[0]).read_text(encoding="utf-8") == mine
    assert list(store.notes_root.glob("Review/*.md")) == []
    assert list(store.sources_root.iterdir()) == []
    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Source)) == 0
        assert await session.scalar(sa.select(sa.func.count()).select_from(Note)) == 0

    monkeypatch.undo()
    retried = await store.ingest(sample())

    assert retried.source.created is True
    assert retried.projection_path != chosen[0]
    assert (store.notes_root / chosen[0]).read_text(encoding="utf-8") == mine
    assert "Scott: Let's start with the architecture review..." in projection_text(
        store, retried.projection_path
    )


async def test_a_rebuilt_projection_without_a_note_date_is_named_in_the_operators_day(
    store: LocalStore, session_factory
):
    """The fallback day is the operator's, not UTC's.

    Blanking the note's date property on a device leaves the mirror no date to
    name the page by, and the page still has to land under the day the captain
    is living in: two operators twenty-six hours apart never name one the same.
    """
    ingested = await store.ingest(sample())
    manifest_path = store.sources_root / ingested.source.id / "manifest.json"
    async with session_factory() as session:
        await session.execute(sa.update(Note).values(date=None))
        await session.commit()

    named: dict[str, str] = {}
    for zone in ("Pacific/Kiritimati", "Etc/GMT+12"):
        current = store.control.store.read("settings")
        body = dict(current.body)
        body["general"] = {**body.get("general", {}), "timezone": zone}
        body.pop("revision", None)
        store.control.store.write("settings", body, if_revision=current.revision)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["projection_path"] = ""
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        replay = await store.ingest(sample())

        named[zone] = replay.projection_path.rsplit("/", 1)[-1][:10]
        assert named[zone] == datetime.now(tz=ZoneInfo(zone)).date().isoformat()

    assert named["Pacific/Kiritimati"] != named["Etc/GMT+12"]


async def test_a_file_delivered_onto_the_recorded_path_refuses_the_page_not_the_revision(
    store: LocalStore, session_factory, monkeypatch: pytest.MonkeyPatch
):
    """A revision that landed is not reported as a request that was rejected.

    Generated output never writes over one of the captain's own notes, so a
    file delivered onto the recorded path while the revision is landing refuses
    the page rather than the bytes. Ingesting the source again places the page
    at a free name and rebuilds the mirror rows the refusal rolled back.
    """
    first = await store.ingest(sample())
    target = store.notes_root / first.projection_path
    mine = "---\nid: 01K4Q8Z3N7V2X9M1B5C6D8E0F2\n---\n# Mine now\n"
    real_stage = projections_module.stage_bytes

    def stage_then_sync_delivers(path, data, **kwargs):
        staged = real_stage(path, data, **kwargs)
        path.write_text(mine, encoding="utf-8")
        return staged

    monkeypatch.setattr(projections_module, "stage_bytes", stage_then_sync_delivers)
    changed = sample()
    changed.source.artifacts[0].content = "Scott: corrected source content"

    with pytest.raises(ProjectionNotPlaced) as refused:
        await store.ingest(changed)

    assert refused.value.revision == 2
    assert refused.value.path == first.projection_path
    assert target.read_text(encoding="utf-8") == mine
    manifest_path = store.sources_root / first.source.id / "manifest.json"
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["current_revision"] == 2
    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(SourceRevision)) == 1

    monkeypatch.undo()
    retried = await store.ingest(changed)

    assert retried.source.revision == 2
    assert retried.projection_path != first.projection_path
    assert target.read_text(encoding="utf-8") == mine
    assert "Scott: corrected source content" in projection_text(store, retried.projection_path)
    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(SourceRevision)) == 2


async def test_a_file_delivered_onto_a_fresh_page_name_refuses_the_page_not_the_volume(
    store: LocalStore, session_factory, monkeypatch: pytest.MonkeyPatch
):
    """A landed revision is not reported as a volume that could not be written.

    The recorded path already holds one of the captain's own notes, so the
    revision picks a free name, and a device can deliver a file onto that one
    just as readily. Generated output never writes over it, and the answer says
    the page was refused rather than sending the operator to check the volume.
    """
    first = await store.ingest(sample())
    mine = "---\nid: 01K4Q8Z3N7V2X9M1B5C6D8E0F2\n---\n# Mine now\n"
    kept = store.notes_root / first.projection_path
    kept.write_text(mine, encoding="utf-8")
    real_create = projections_module.create_exclusive_bytes
    delivered: list[str] = []

    def create_after_sync_delivers(path, data, **kwargs):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(mine, encoding="utf-8")
        delivered.append(path.relative_to(store.notes_root).as_posix())
        return real_create(path, data, **kwargs)

    monkeypatch.setattr(projections_module, "create_exclusive_bytes", create_after_sync_delivers)
    changed = sample()
    changed.source.artifacts[0].content = "Scott: corrected source content"

    with pytest.raises(ProjectionNotPlaced) as refused:
        await store.ingest(changed)

    assert refused.value.revision == 2
    assert refused.value.path == delivered[0]
    assert (store.notes_root / delivered[0]).read_text(encoding="utf-8") == mine
    manifest_path = store.sources_root / first.source.id / "manifest.json"
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["current_revision"] == 2
    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(SourceRevision)) == 1

    monkeypatch.undo()
    retried = await store.ingest(changed)

    assert retried.source.revision == 2
    assert retried.projection_path not in (first.projection_path, delivered[0])
    assert "Scott: corrected source content" in projection_text(store, retried.projection_path)
    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(SourceRevision)) == 2


async def test_a_sources_fault_recording_a_repaired_projection_reports_it_and_leaves_no_copy(
    store: LocalStore, monkeypatch: pytest.MonkeyPatch
):
    """The operator is told which volume failed, not to go and check PostgreSQL.

    The captain's own file has taken the recorded path, so the replay rebuilds
    the projection somewhere else and has to record where. That manifest write
    is on the sources volume, and a fault there is that volume's to report. The
    projection it would have recorded goes with it: no manifest points at it and
    it names no note id, so reconciliation records it as an unidentified file
    and nothing reports it, while a retrying client would otherwise leave the
    captain one more copy of the whole transcript per attempt.
    """
    ingested = await store.ingest(sample())
    kept = store.notes_root / ingested.projection_path
    kept.write_text("---\nid: 01K4Q8Z3N7V2X9M1B5C6D8E0F2\n---\n# Mine now\n", encoding="utf-8")
    folder = kept.parent

    def read_only_volume(*_args, **_kwargs):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(sources_module, "stage_bytes", read_only_volume)

    with pytest.raises(SourcesFilesystemUnavailable):
        await store.ingest(sample())
    with pytest.raises(SourcesFilesystemUnavailable):
        await store.ingest(sample())

    assert [path.name for path in folder.iterdir()] == [kept.name]
    monkeypatch.undo()
    replay = await store.ingest(sample())
    assert replay.projection_path != ingested.projection_path
    assert sorted(path.name for path in folder.iterdir()) == sorted(
        [kept.name, replay.projection_path.rsplit("/", 1)[-1]]
    )


async def test_an_ambiguous_manifest_commit_keeps_the_newly_referenced_projection(
    store: LocalStore, monkeypatch: pytest.MonkeyPatch
):
    """A directory-sync failure may mean the new manifest is already authoritative."""
    ingested = await store.ingest(sample())
    kept = store.notes_root / ingested.projection_path
    kept.write_text("---\nid: 01K4Q8Z3N7V2X9M1B5C6D8E0F2\n---\n# Mine now\n", encoding="utf-8")
    real_commit = sources_module.commit_staged

    def replace_then_fail(staged, path):
        real_commit(staged, path)
        raise OSError("directory sync acknowledgement lost")

    monkeypatch.setattr(sources_module, "commit_staged", replace_then_fail)

    with pytest.raises(SourcesFilesystemUnavailable):
        await store.ingest(sample())

    manifest_path = store.sources_root / ingested.source.id / "manifest.json"
    projection_path = json.loads(manifest_path.read_text(encoding="utf-8"))["projection_path"]
    assert projection_path != ingested.projection_path
    assert "source_revision: 1" in projection_text(store, projection_path)
    assert kept.read_text(encoding="utf-8").endswith("# Mine now\n")


async def test_an_unrecorded_projection_path_is_rebuilt_rather_than_searched_for(
    store: LocalStore,
):
    """The manifest is the only record of where a projection lives.

    A manifest that names none is a source with no projection, whatever sits in
    the sources folder, so the next ingest builds one and records where.
    """
    ingested = await store.ingest(sample())
    manifest_path = store.sources_root / ingested.source.id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["projection_path"] == ingested.projection_path
    manifest["projection_path"] = ""
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    replay = await store.ingest(sample())

    assert replay.projection_path != ""
    assert (
        json.loads(manifest_path.read_text(encoding="utf-8"))["projection_path"]
        == replay.projection_path
    )
    assert "Scott: Let's start with the architecture review..." in projection_text(
        store, replay.projection_path
    )


async def test_retry_after_a_revision_commit_failure_returns_the_completed_revision(
    store: LocalStore, session_factory, monkeypatch: pytest.MonkeyPatch
):
    first = await store.ingest(sample())
    changed = sample()
    changed.source.artifacts[0].content = "Scott: corrected source content"
    real_transaction = sources_module.transaction

    @asynccontextmanager
    async def fail_at_commit(factory):
        async with real_transaction(factory) as session:
            yield session
            raise sa.exc.OperationalError("COMMIT", {}, OSError("connection lost"))

    monkeypatch.setattr(sources_module, "transaction", fail_at_commit)
    with pytest.raises(MetadataUnavailable):
        await store.ingest(changed)

    source_root = store.sources_root / first.source.id
    assert (source_root / "r0002" / "transcript.txt").is_file()
    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(SourceRevision)) == 1

    monkeypatch.setattr(sources_module, "transaction", real_transaction)
    replay = await store.ingest(changed)
    assert replay.source.id == first.source.id
    assert replay.source.revision == 2
    assert replay.source.created is False
    assert replay.note.id == first.note.id
    assert replay.note.created is False
    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(SourceRevision)) == 2


async def test_manifest_sync_failure_retains_a_possibly_committed_revision(
    store: LocalStore, monkeypatch: pytest.MonkeyPatch
):
    """A failed durability acknowledgement cannot undo a manifest replacement."""
    first = await store.ingest(sample())
    changed = sample()
    changed.source.artifacts[0].content = "Scott: corrected source content"
    real_atomic_write = sources_module.atomic_write_bytes

    def replace_then_fail(path, data, **kwargs):
        real_atomic_write(path, data, **kwargs)
        raise OSError("directory sync acknowledgement lost")

    monkeypatch.setattr(sources_module, "atomic_write_bytes", replace_then_fail)
    with pytest.raises(SourcesFilesystemUnavailable):
        await store.ingest(changed)

    source_root = store.sources_root / first.source.id
    manifest = json.loads((source_root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["current_revision"] == 2
    assert (source_root / "r0002" / "transcript.txt").read_text(encoding="utf-8") == (
        "Scott: corrected source content"
    )

    monkeypatch.setattr(sources_module, "atomic_write_bytes", real_atomic_write)
    replay = await store.ingest(changed)
    assert replay.source.revision == 2
    assert replay.source.created is False
    projection = projection_text(store, replay.projection_path)
    assert "source_revision: 2" in projection
    assert "Scott: corrected source content" in projection


@pytest.mark.parametrize("damage", ["missing", "altered"])
async def test_replay_refuses_a_missing_or_altered_current_artifact(store: LocalStore, damage: str):
    first = await store.ingest(sample())
    artifact = store.sources_root / first.source.id / "r0001" / "transcript.txt"
    if damage == "missing":
        artifact.unlink()
    else:
        artifact.write_text("damaged after ingest", encoding="utf-8")

    with pytest.raises(SourcesFilesystemUnavailable):
        await store.ingest(sample())


async def test_unverified_artifacts_cannot_rebuild_a_missing_mirror(
    store: LocalStore, session_factory, monkeypatch: pytest.MonkeyPatch
):
    real_transaction = sources_module.transaction

    @asynccontextmanager
    async def fail_at_commit(factory):
        async with real_transaction(factory) as session:
            yield session
            raise sa.exc.OperationalError("COMMIT", {}, OSError("connection lost"))

    monkeypatch.setattr(sources_module, "transaction", fail_at_commit)
    with pytest.raises(MetadataUnavailable):
        await store.ingest(sample())

    artifact = next(store.sources_root.glob("*/r0001/transcript.txt"))
    artifact.unlink()
    monkeypatch.setattr(sources_module, "transaction", real_transaction)
    with pytest.raises(SourcesFilesystemUnavailable):
        await store.ingest(sample())

    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Source)) == 0
        assert await session.scalar(sa.select(sa.func.count()).select_from(SourceArtifact)) == 0


async def test_replay_cannot_overwrite_a_concurrent_note_edit(
    store: LocalStore, monkeypatch: pytest.MonkeyPatch
):
    first = await store.ingest(sample())
    note_path = store.notes_root / first.note.path
    reached_mirror = asyncio.Event()
    allow_replay = asyncio.Event()
    real_ensure_mirror = sources_module._ensure_mirror

    async def held_ensure_mirror(*args, **kwargs):
        reached_mirror.set()
        await allow_replay.wait()
        return await real_ensure_mirror(*args, **kwargs)

    monkeypatch.setattr(sources_module, "_ensure_mirror", held_ensure_mirror)
    replay_task = asyncio.create_task(store.ingest(sample()))
    await reached_mirror.wait()
    edited = fm.patch(note_path.read_text(encoding="utf-8"), {"reviewed": True})
    edited = edited.replace("Target architecture agreed", "Edited during replay")
    note_path.write_text(edited, encoding="utf-8")
    allow_replay.set()
    replay = await replay_task

    assert replay.source.created is False
    assert replay.note.created is False
    assert note_path.read_text(encoding="utf-8") == edited


async def test_a_descriptive_correction_alone_replays_and_reports_what_was_not_stored(
    store: LocalStore, session_factory
):
    """A source is its artifact bytes, so a describing field is reported, not kept."""
    first = await store.ingest(sample())
    note_path = store.notes_root / first.note.path
    note_path.write_text(fm.patch(note_path.read_text(encoding="utf-8"), {"reviewed": True}))
    before = {
        path.relative_to(store.notes_root.parent): path.read_bytes()
        for path in store.notes_root.parent.rglob("*")
        if path.is_file()
    }

    corrected = sample()
    corrected.source.captured_at = datetime.fromisoformat("2026-09-08T15:47:00-05:00")
    corrected.source.metadata["language"] = "en-US"
    corrected.source.origin = "Plaud NotePin (office)"
    corrected.source.source_type = "document"
    replay = await store.ingest(corrected)

    assert replay.source.id == first.source.id
    assert replay.source.revision == 1
    assert replay.source.created is False
    assert replay.note.id == first.note.id
    assert replay.note.created is False
    assert sorted(replay.source.unstored_fields) == [
        "captured_at",
        "metadata",
        "origin",
        "source_type",
    ]
    after = {
        path.relative_to(store.notes_root.parent): path.read_bytes()
        for path in store.notes_root.parent.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert len(list((store.notes_root / "Review").glob("*.md"))) == 1
    async with session_factory() as session:
        source = await session.get(Source, first.source.id)
        assert source.current_revision == 1
        assert source.origin == "Plaud NotePin"
        assert source.source_type == "transcript"
        assert await session.scalar(sa.select(sa.func.count()).select_from(SourceRevision)) == 1
        assert await session.scalar(sa.select(sa.func.count()).select_from(Note)) == 1


async def test_a_corrected_mime_type_is_reported_rather_than_silently_discarded(
    store: LocalStore,
):
    """The artifact bytes are the identity, so a mime type correction is not kept either."""
    first = await store.ingest(sample())
    source_root = store.sources_root / first.source.id
    corrected = sample()
    corrected.source.artifacts[0].mime_type = "text/markdown"

    replay = await store.ingest(corrected)

    assert replay.source.created is False
    assert replay.source.unstored_fields == ["mime_type"]
    manifest = json.loads((source_root / "manifest.json").read_text(encoding="utf-8"))
    stored = {item["name"]: item["mime_type"] for item in manifest["revisions"][0]["artifacts"]}
    assert stored["transcript.txt"] == "text/plain"


async def test_a_fully_identical_replay_reports_nothing_unstored(store: LocalStore):
    await store.ingest(sample())
    replay = await store.ingest(sample())
    assert replay.source.created is False
    assert replay.source.unstored_fields == []


async def test_a_corrected_retry_after_a_commit_failure_still_repairs_the_mirror(
    store: LocalStore, session_factory, monkeypatch: pytest.MonkeyPatch
):
    """The retry an automation actually sends must not leave the mirror empty."""
    real_transaction = sources_module.transaction

    @asynccontextmanager
    async def fail_at_commit(factory):
        async with real_transaction(factory) as session:
            yield session
            raise sa.exc.OperationalError("COMMIT", {}, OSError("connection lost"))

    monkeypatch.setattr(sources_module, "transaction", fail_at_commit)
    with pytest.raises(MetadataUnavailable):
        await store.ingest(sample())
    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Source)) == 0

    monkeypatch.setattr(sources_module, "transaction", real_transaction)
    retried = sample()
    retried.source.captured_at = datetime.fromisoformat("2026-09-08T16:30:00-05:00")
    replay = await store.ingest(retried)

    assert replay.source.created is False
    assert replay.source.revision == 1
    assert replay.source.unstored_fields == ["captured_at"]
    assert replay.note.created is False
    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Source)) == 1
        assert await session.scalar(sa.select(sa.func.count()).select_from(SourceRevision)) == 1
        assert await session.scalar(sa.select(sa.func.count()).select_from(Note)) == 1
        link = (await session.execute(sa.select(NoteSource))).scalar_one()
        assert link.note_id == replay.note.id
        assert link.source_id == replay.source.id


async def test_a_leftover_revision_directory_is_named_and_left_alone(
    store: LocalStore, session_factory
):
    """An interrupted revision write is an operator's call, not a volume outage."""
    first = await store.ingest(sample())
    orphan = store.sources_root / first.source.id / "r0002"
    orphan.mkdir()
    (orphan / "transcript.txt").write_text("half a revision", encoding="utf-8")

    changed = sample()
    changed.source.artifacts[0].content = "Scott: corrected source content"
    with pytest.raises(IncompleteRevision) as raised:
        await store.ingest(changed)

    assert raised.value.path == f"{first.source.id}/r0002"
    assert (orphan / "transcript.txt").read_text(encoding="utf-8") == "half a revision"
    manifest = json.loads(
        (store.sources_root / first.source.id / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["current_revision"] == 1
    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(SourceRevision)) == 1


async def test_a_missing_claim_over_a_surviving_row_is_refused_not_an_outage(
    store: LocalStore, session_factory
):
    """/data/sources restored without the database must not answer 503."""
    first = await store.ingest(sample())
    claim = next(store.sources_root.glob(".external-id-*.json"))
    claim.unlink()

    with pytest.raises(SourceClaimMissing) as raised:
        await store.ingest(sample())

    assert raised.value.provider == "plaud"
    assert raised.value.external_source_id == "rec_8f3a2c19"
    assert not isinstance(raised.value, MetadataUnavailable)
    assert list(store.sources_root.glob(".external-id-*.json")) == []
    assert [path.name for path in store.sources_root.iterdir() if path.is_dir()] == [
        first.source.id
    ]
    assert len(list((store.notes_root / "Review").glob("*.md"))) == 1
    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Source)) == 1
        assert await session.scalar(sa.select(sa.func.count()).select_from(Note)) == 1


async def test_a_note_delivered_before_the_write_survives_the_refused_ingest(
    store: LocalStore, session_factory, monkeypatch: pytest.MonkeyPatch
):
    """The collision cleanup only removes files this ingest created."""
    real_create = sources_module.create_exclusive_bytes
    delivered = b"---\nid: 01K4Q8Z3N7V2X9M1B5C6D8E0F2\n---\n\n# Delivered by a device\n"

    def deliver_then_create(path, data, **kwargs):
        if path.is_relative_to(store.notes_root):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(delivered)
        return real_create(path, data, **kwargs)

    monkeypatch.setattr(sources_module, "create_exclusive_bytes", deliver_then_create)
    with pytest.raises(PathCollision):
        await store.ingest(sample())

    assert [path.read_bytes() for path in store.notes_root.rglob("*.md")] == [delivered]
    assert list(store.sources_root.iterdir()) == []
    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Source)) == 0
        assert await session.scalar(sa.select(sa.func.count()).select_from(Note)) == 0


async def test_the_recovery_scan_does_not_block_the_request_loop(
    store: LocalStore, monkeypatch: pytest.MonkeyPatch
):
    """Rebuilding a lost note link reads every note, so it must not stall the store."""
    real_transaction = sources_module.transaction

    @asynccontextmanager
    async def fail_at_commit(factory):
        async with real_transaction(factory) as session:
            yield session
            raise sa.exc.OperationalError("COMMIT", {}, OSError("connection lost"))

    monkeypatch.setattr(sources_module, "transaction", fail_at_commit)
    with pytest.raises(MetadataUnavailable):
        await store.ingest(sample())
    monkeypatch.setattr(sources_module, "transaction", real_transaction)

    real_scan = sources_module._scan_for_linked_note

    def slow_scan(*args, **kwargs):
        time.sleep(0.6)
        return real_scan(*args, **kwargs)

    monkeypatch.setattr(sources_module, "_scan_for_linked_note", slow_scan)
    gaps: list[float] = []

    async def heartbeat():
        previous = time.monotonic()
        while True:
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - previous)
            previous = now

    beating = asyncio.create_task(heartbeat())
    try:
        replay = await store.ingest(sample())
    finally:
        beating.cancel()

    assert replay.source.created is False
    assert replay.note.created is False
    assert len(gaps) > 20, "the event loop did not keep running during the scan"
    assert max(gaps) < 0.3


async def test_ingest_refuses_a_path_a_live_row_still_holds(store: LocalStore, session_factory):
    """Ingest answers the same 409 as create when a row, not a file, holds the path."""
    note = await store.create_note(
        CreateNote(
            title="Ameren Architecture Sync",
            frontmatter={"date": "2026-09-08", "type": "meeting", "context": "internal"},
        )
    )
    (store.notes_root / note.path).unlink()

    with pytest.raises(PathCollision) as raised:
        await store.ingest(sample())

    assert raised.value.existing_path == note.path
    assert list(store.sources_root.rglob("manifest.json")) == []
    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Source)) == 0
        assert await session.scalar(sa.select(sa.func.count()).select_from(Note)) == 1
