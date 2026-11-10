"""Artifacts against PostgreSQL and a real notes filesystem: the acceptance list of issue 77.

Each test drives the store the way the API, Dropbox or a device would, then
reads the notes filesystem, the archive, the state records and the search
index back, because every claim here is about where bytes landed and what
can find them.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from coppermind_store import artifacts, filing, indexer
from coppermind_store.artifacts import browse_folder, record_path
from coppermind_store.extraction import EXTRACTOR_VERSION
from coppermind_store.notes import LocalStore
from coppermind_store.reconciler import reconcile_once

from coppermind.db.models import Artifact, Note, SearchDocument
from coppermind.store_protocol import (
    ArtifactMissing,
    ArtifactQuery,
    CreateArtifact,
    CreateNote,
    IngestArtifact,
    MetadataUnavailable,
    PatchFrontmatter,
    ReplaceNote,
    SearchQuery,
    ValidationFailed,
)
from services.store.tests.test_extraction import deck, minimal_pdf
from tests.integration.test_review_filing import approve, drain
from tests.integration.test_search_index import ingest_request


def encoded(data: bytes) -> str:
    return base64.b64encode(data).decode()


def pptx_bytes(tmp_path, title: str, body: str) -> bytes:
    path = tmp_path / "deck-source.pptx"
    deck(path, title, body)
    return path.read_bytes()


def upload(filename: str, data: bytes, **fields) -> CreateArtifact:
    return CreateArtifact(
        kind="artifact", filename=filename, content_base64=encoded(data), **fields
    )


def month_folder(store: LocalStore) -> str:
    return browse_folder(datetime.now(tz=UTC), store.control.settings())


def notes_tree(store: LocalStore) -> set[str]:
    return {
        p.relative_to(store.notes_root).as_posix()
        for p in store.notes_root.rglob("*")
        if p.is_file() and not p.name.startswith(".")
    }


async def hits(store: LocalStore, q: str, **filters):
    return (await store.search(SearchQuery.model_validate({"q": q, **filters}))).items


async def test_a_pptx_upload_is_a_browsable_file_and_searchable_at_once_with_nothing_else(
    store, tmp_path
):
    data = pptx_bytes(tmp_path, "VCDX design package", "Storage: vSAN stretched kerfuffle")
    created = await store.create_artifact(
        upload("VCDX design package.pptx", data, tags=["reference"])
    )

    assert created.created is True and created.kind == "artifact"
    assert created.path == f"{month_folder(store)}/VCDX design package.pptx"
    assert (store.notes_root / created.path).read_bytes() == data
    # The file, and only the file, appears in the notes filesystem.
    assert notes_tree(store) == {created.path}
    assert not (store.notes_root / "Review").exists()
    # The archive holds the bytes under the identity; the record sits in the state volume.
    archive = store.artifacts_root / created.id
    assert (archive / "r0001" / "VCDX design package.pptx").read_bytes() == data
    manifest = json.loads((archive / "manifest.json").read_text())
    assert manifest["kind"] == "artifact" and manifest["path"] == created.path
    record = json.loads(record_path(store, created.id).read_text())
    assert record["text_status"] == "extracted" and "kerfuffle" in record["text"]
    assert record["extractor_version"] == EXTRACTOR_VERSION

    # Searchable by slide text without a review gate, labelled with its kind.
    found = await hits(store, "kerfuffle")
    assert [(hit.kind, hit.id, hit.path) for hit in found] == [
        ("artifact", created.id, created.path)
    ]
    assert "**kerfuffle**" in found[0].excerpt
    assert await hits(store, "kerfuffle", kind="note") == []
    assert await hits(store, "kerfuffle", kind="artifact", tag="reference")
    assert await hits(store, "kerfuffle", tag="other") == []
    # It is not a note anywhere: no listing, no review queue.
    assert (await store.list_notes(SearchQuery.model_validate({"q": "x"}).model_copy())).items == []
    status = await store.get_status()
    assert status.counters.artifacts == 1
    assert status.counters.notes_awaiting_review == 0

    read = await store.get_artifact(created.id)
    assert read.kind == "artifact" and read.origin == "api" and read.sha256 == record["sha256"]
    assert read.mime_type.endswith("presentationml.presentation")
    assert read.filename == "VCDX design package.pptx" and read.tags == ["reference"]
    assert read.citing_note_ids == [] and read.revision == 1
    assert "kerfuffle" in read.text
    served = await store.get_artifact_bytes(created.id)
    assert await drain(served) == data and served.filename == "VCDX design package.pptx"


async def test_search_can_leave_artifact_hits_out(store):
    created = await store.create_artifact(upload("notes.txt", b"hidden quokka text\n"))
    assert [hit.id for hit in await hits(store, "quokka")] == [created.id]
    assert await hits(store, "quokka", include_artifacts=False) == []
    assert await hits(store, "quokka", kind="artifact", include_artifacts=False) == []


async def test_deleting_the_file_in_obsidian_retires_the_artifact_once_git_records_it(
    store, tmp_path
):
    """The owner's delete in Obsidian is the deletion the Git helper commits, nothing less."""
    from tests.integration.test_note_recovery import commit, git

    data = pptx_bytes(tmp_path, "Enablement deck", "Platypus enablement")
    created = await store.create_artifact(upload("enablement.pptx", data))
    await store.create_artifact(
        CreateArtifact(kind="artifact", url="https://example.test/wombat", title="Wombat paper")
    )
    git(store.notes_root, "init", "-q")
    commit(store.notes_root)
    assert await hits(store, "platypus")
    (store.notes_root / created.path).unlink()

    # Absent but not committed: a device may simply not have synced yet.
    swept = await artifacts.sweep(store)
    assert swept["artifacts_missing"] == 1 and swept["artifacts_removed"] == 0
    assert record_path(store, created.id).exists()
    assert (await store.get_artifact(created.id)).state == "missing"
    problems = [p for p in await store.get_problems() if p.artifact_id == created.id]
    assert problems and "Git has not recorded its deletion" in problems[0].reason
    assert "removed_at" not in json.loads(
        (store.artifacts_root / created.id / "manifest.json").read_text()
    )
    assert await hits(store, "platypus")

    # Committed: the artifact is gone on the next pass, the archive marked for purge.
    removal = commit(store.notes_root)
    swept = await artifacts.sweep(store)
    assert swept["artifacts_removed"] == 1
    assert not record_path(store, created.id).exists()
    assert await hits(store, "platypus") == []
    with pytest.raises(ArtifactMissing):
        await store.get_artifact(created.id)
    manifest = json.loads((store.artifacts_root / created.id / "manifest.json").read_text())
    assert manifest["removed_reason"] == artifacts.DELETED_IN_OBSIDIAN
    assert manifest["removal_commit"] == removal
    # Nothing else changed: the other artifact, and the archived bytes, are still there.
    assert await hits(store, "wombat")
    assert (store.artifacts_root / created.id / "r0001" / "enablement.pptx").read_bytes() == data
    assert (await store.get_status()).counters.artifacts == 1


@pytest.mark.parametrize("destination", ["renamed deck.pptx", "Elsewhere/Deeper/deck.pptx"])
async def test_moving_or_renaming_the_file_in_obsidian_is_not_a_delete(
    store, tmp_path, destination
):
    from tests.integration.test_note_recovery import commit, git

    data = pptx_bytes(tmp_path, "Enablement deck", "Platypus enablement")
    created = await store.create_artifact(upload("deck.pptx", data))
    git(store.notes_root, "init", "-q")
    commit(store.notes_root)
    original = store.notes_root / created.path
    target = (
        original.parent / destination
        if "/" not in destination
        else (store.notes_root / destination)
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    original.rename(target)
    commit(store.notes_root)

    swept = await artifacts.sweep(store)

    assert swept["artifacts_removed"] == 0
    assert original.read_bytes() == data
    assert not target.exists()
    assert (await store.get_artifact(created.id)).state == "ok"
    assert await hits(store, "platypus")
    manifest = json.loads((store.artifacts_root / created.id / "manifest.json").read_text())
    assert "removed_at" not in manifest

    # The owner sees why it moved back: the same current-notice list a hand-moved note uses.
    [notice] = await store.list_reconciler_notices()
    assert notice.kind == "artifact" and notice.note_id == created.id
    assert notice.title == "deck"
    assert notice.from_path == target.relative_to(store.notes_root).as_posix()
    assert notice.path == created.path
    folder = created.path.rpartition("/")[0]
    assert notice.reason.startswith(f"Moved back to {folder}/ because artifacts are filed")
    assert "nothing needs doing" in notice.reason
    assert (await store.get_status()).counters.artifacts_returned == 1
    await store.dismiss_reconciler_notice(created.id)
    assert await store.list_reconciler_notices() == []
    assert (await store.get_status()).counters.artifacts_returned == 0


async def test_a_notice_about_an_artifact_that_is_gone_is_dropped(store, tmp_path):
    from tests.integration.test_note_recovery import commit, git

    data = pptx_bytes(tmp_path, "Enablement deck", "Platypus enablement")
    created = await store.create_artifact(upload("deck.pptx", data))
    git(store.notes_root, "init", "-q")
    commit(store.notes_root)
    original = store.notes_root / created.path
    original.rename(original.parent / "renamed.pptx")
    commit(store.notes_root)
    await artifacts.sweep(store)
    assert len(await store.list_reconciler_notices()) == 1

    await store.remove_artifact(created.id)

    assert await store.list_reconciler_notices() == []
    assert store.control.read_reconciler_notices() == {}


async def test_a_moved_copy_is_never_taken_from_another_artifact(store, tmp_path):
    from tests.integration.test_note_recovery import commit, git

    data = pptx_bytes(tmp_path, "Twin", "Identical bytes")
    first = await store.create_artifact(upload("deck.pptx", data))
    second = await store.create_artifact(upload("copy of deck.pptx", data))
    git(store.notes_root, "init", "-q")
    commit(store.notes_root)
    assert first.id != second.id
    (store.notes_root / first.path).unlink()
    commit(store.notes_root)

    swept = await artifacts.sweep(store)

    assert swept["artifacts_removed"] == 1
    assert (store.notes_root / second.path).read_bytes() == data
    assert (await store.get_artifact(second.id)).state == "ok"


async def test_a_name_with_glob_characters_is_matched_literally_against_history(store):
    from tests.integration.test_note_recovery import commit, git

    created = await store.create_artifact(upload("Q1 [final]*?.txt", b"glob quokka\n"))
    pattern = (store.notes_root / created.path).name
    assert pattern.startswith("Q1 [final]")
    git(store.notes_root, "init", "-q")
    folder = (store.notes_root / created.path).parent
    (store.notes_root / created.path).unlink()
    decoy = folder / "Q1 fxy.txt"
    decoy.write_bytes(b"another file")
    commit(store.notes_root)
    decoy.unlink()
    commit(store.notes_root)

    swept = await artifacts.sweep(store)

    assert swept["artifacts_removed"] == 0 and swept["artifacts_missing"] == 1
    manifest = json.loads((store.artifacts_root / created.id / "manifest.json").read_text())
    assert "removed_at" not in manifest
    assert await hits(store, "quokka")


async def test_an_unfinished_search_for_a_moved_copy_never_retires_the_artifact(store, monkeypatch):
    from pathlib import Path

    from tests.integration.test_note_recovery import commit, git

    data = b"unreadable quokka\n"
    created = await store.create_artifact(upload("deck.txt", data))
    git(store.notes_root, "init", "-q")
    commit(store.notes_root)
    (store.notes_root / created.path).unlink()
    stray = store.notes_root / "Elsewhere" / "sync-temp.bin"
    stray.parent.mkdir()
    stray.write_bytes(b"x" * len(data))
    commit(store.notes_root)
    real = Path.open

    def flaky(self, *args, **kwargs):
        if self.name == "sync-temp.bin":
            raise OSError("vanished during sync")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", flaky)
    swept = await artifacts.sweep(store)

    assert swept["artifacts_removed"] == 0 and swept["artifacts_missing"] == 1
    manifest = json.loads((store.artifacts_root / created.id / "manifest.json").read_text())
    assert "removed_at" not in manifest
    monkeypatch.undo()
    assert (await artifacts.sweep(store))["artifacts_removed"] == 1


async def test_an_older_deletion_at_the_same_name_does_not_retire_a_newer_artifact(store):
    import os
    import subprocess

    from tests.integration.test_note_recovery import git

    git(store.notes_root, "init", "-q")
    folder = store.notes_root / month_folder(store)
    folder.mkdir(parents=True)
    old = folder / "deck.txt"
    old.write_bytes(b"an older artifact")
    past = {**os.environ, "GIT_COMMITTER_DATE": "2020-01-01T00:00:00Z"}
    for message in ("add", "delete"):
        if message == "delete":
            old.unlink()
        git(store.notes_root, "add", "-A")
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=T",
                "-c",
                "user.email=t@example.test",
                "commit",
                "-qm",
                message,
            ],
            cwd=store.notes_root,
            env={**past, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
            check=True,
        )
    created = await store.create_artifact(upload("deck.txt", b"newer wombat\n"))
    assert created.path == f"{month_folder(store)}/deck.txt"
    (store.notes_root / created.path).unlink()

    swept = await artifacts.sweep(store)

    assert swept["artifacts_removed"] == 0 and swept["artifacts_missing"] == 1
    assert await hits(store, "wombat")


async def test_a_copy_that_comes_back_clears_missing_and_a_rebuild_never_retires(store, tmp_path):
    data = pptx_bytes(tmp_path, "Deck", "Returning quoll")
    created = await store.create_artifact(upload("deck.pptx", data))
    (store.notes_root / created.path).unlink()
    assert (await artifacts.sweep(store))["artifacts_missing"] == 1

    counts = (await store.rebuild_metadata()).counts
    assert counts["recovered_artifacts"] == 1 and counts["missing_artifact_copies"] == 1
    assert (await store.get_artifact(created.id)).state == "missing"
    assert (store.artifacts_root / created.id / "r0001" / "deck.pptx").read_bytes() == data

    (store.notes_root / created.path).write_bytes(data)
    await artifacts.sweep(store)
    assert (await store.get_artifact(created.id)).state == "ok"
    assert not [p for p in await store.get_problems() if p.artifact_id == created.id]


async def test_a_refused_revision_never_deletes_an_unrecorded_revision_directory(store, tmp_path):
    """Bytes an interrupted write left behind are the operator's to inspect, not ours to erase."""
    from coppermind.store_protocol import ArtifactsFilesystemUnavailable

    created = await store.create_artifact(upload("deck.pptx", pptx_bytes(tmp_path, "D", "one")))
    stray = store.artifacts_root / created.id / "r0002"
    stray.mkdir()
    (stray / "leftover.pptx").write_bytes(b"left behind")

    with pytest.raises(ArtifactsFilesystemUnavailable, match="r0002"):
        await store.create_artifact(
            upload("deck.pptx", pptx_bytes(tmp_path, "D", "two"), artifact_id=created.id)
        )

    assert (stray / "leftover.pptx").read_bytes() == b"left behind"
    assert (await store.get_artifact(created.id)).revision == 1


async def test_a_record_describing_an_older_revision_is_regenerated(store, tmp_path):
    """A revision write interrupted between the manifest and the record leaves a stale record."""
    one = pptx_bytes(tmp_path, "Deck", "Version one numbat")
    two = pptx_bytes(tmp_path, "Deck", "Version two numbat")
    created = await store.create_artifact(upload("deck.pptx", one))
    await store.create_artifact(upload("deck.pptx", two, artifact_id=created.id))
    path = record_path(store, created.id)
    stale = json.loads(path.read_text())
    stale["revision"] = 1
    stale["text"] = "Version one numbat"
    path.write_text(json.dumps(stale))

    replay = await store.create_artifact(upload("deck.pptx", two, artifact_id=created.id))
    assert replay.created is False and replay.revision == 2
    refreshed = json.loads(path.read_text())
    assert refreshed["revision"] == 2 and "two" in refreshed["text"]

    path.write_text(json.dumps(stale))
    assert (await artifacts.sweep(store))["artifacts_reextracted"] == 1
    assert json.loads(path.read_text())["revision"] == 2
    path.write_text(json.dumps(stale))
    await store.rebuild_metadata()
    assert json.loads(path.read_text())["revision"] == 2
    assert (await store.get_artifact(created.id)).text.count("two") >= 1


async def test_a_note_that_cites_an_artifact_links_it_files_by_month_and_lists_as_citing(
    store, tmp_path
):
    data = pptx_bytes(tmp_path, "Upgrade deck", "Upgrade path for vSphere")
    created = await store.create_artifact(upload("upgrade.pptx", data, date="2026-09-15"))
    assert created.path == "Artifacts/2026/2026-09/upgrade.pptx"

    note = await store.create_note(
        CreateNote(
            title="Upgrade planning",
            body="We walked the deck.",
            frontmatter={
                "date": "2026-10-02",
                "type": "work",
                "context": "internal",
                "reviewed": True,
                "artifacts": [created.id],
            },
        )
    )
    assert note.frontmatter["artifacts"] == [created.id]
    assert note.frontmatter["kind"] == "note"
    assert note.body.endswith("## Artifacts\n\n[[Artifacts/2026/2026-09/upgrade.pptx]]\n")

    assert await filing.file_reviewed(store) == {"filed": 1, "filing_failed": 0}
    filed = await store.get_note(note.id)
    assert filed.path.startswith("Notes/2026/2026-10/")
    # The artifact did not move with the note, and keeps its own month.
    assert (store.notes_root / created.path).read_bytes() == data
    assert (await store.get_artifact(created.id)).path == created.path
    assert (await store.get_artifact(created.id)).citing_note_ids == [note.id]
    listed = await store.list_artifacts(ArtifactQuery(note_id=note.id))
    assert [item.id for item in listed.items] == [created.id]
    assert listed.items[0].citing_note_ids == [note.id]
    assert [hit.id for hit in await hits(store, "vSphere", citing_note=note.id)] == [created.id]
    assert await hits(store, "vSphere", citing_note=created.id) == []

    # Citing through the upload route patches an existing note the same way.
    second = await store.create_artifact(
        upload("second.pptx", pptx_bytes(tmp_path, "Second", "Another"), note_id=note.id)
    )
    assert second.note_id == note.id
    cited = await store.get_note(note.id)
    assert cited.frontmatter["artifacts"] == [created.id, second.id]
    assert f"[[{second.path}]]" in cited.body
    assert cited.body.count("## Artifacts") == 1


async def test_a_url_is_an_artifact_with_title_and_link_searchable_by_title(store):
    created = await store.create_artifact(
        CreateArtifact(kind="artifact", url="https://example.test/vsan-paper", title="vSAN paper")
    )
    assert created.path is None
    assert notes_tree(store) == set()
    found = await hits(store, "vSAN")
    assert [(hit.kind, hit.id, hit.path, hit.title) for hit in found] == [
        ("artifact", created.id, "", "vSAN paper")
    ]
    record = await store.get_artifact(created.id)
    assert record.url == "https://example.test/vsan-paper" and record.text_status == "link"
    assert record.filename is None and record.sha256 is None
    with pytest.raises(ValidationFailed, match="link"):
        await store.get_artifact_bytes(created.id)
    replay = await store.create_artifact(
        CreateArtifact(kind="artifact", url="https://example.test/vsan-paper")
    )
    assert replay.id == created.id and replay.created is False


async def test_a_note_cannot_be_an_artifact_and_an_artifact_needs_its_kind(store):
    with pytest.raises(ValidationFailed) as refused:
        await store.create_note(
            CreateNote(
                title="Not a note",
                frontmatter={"type": "reference", "context": "internal", "kind": "artifact"},
            )
        )
    assert "an artifact is never a note" in str(refused.value)
    note = await store.create_note(CreateNote(title="A note", frontmatter={"type": "reference"}))
    with pytest.raises(ValidationFailed, match="artifact is never a note"):
        await store.patch_frontmatter(
            note.id, PatchFrontmatter(set={"kind": "artifact"}), note.content_hash
        )
    with pytest.raises(ValidationFailed, match="no artifact with id"):
        await store.patch_frontmatter(
            note.id,
            PatchFrontmatter(set={"artifacts": ["01K4Q8Z2A0P1Q2R3S4T5U6V7W8"]}),
            note.content_hash,
        )
    assert (await store.get_note(note.id)).frontmatter.get("kind") == "note"
    with pytest.raises(ValueError, match="a declared kind is required"):
        CreateArtifact(filename="deck.pptx", content_base64=encoded(b"PK"))
    assert notes_tree(store) == {note.path}


async def test_a_markdown_file_under_the_artifacts_folder_is_never_adopted_as_a_note(store):
    created = await store.create_artifact(upload("readme.md", b"# Not a note\n\nplain text\n"))
    assert created.path.endswith("/readme.md")
    await reconcile_once(store, full=True)
    await reconcile_once(store, full=True)
    async with store.session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Note)) == 0
    assert (store.notes_root / created.path).read_bytes() == b"# Not a note\n\nplain text\n"
    assert [hit.kind for hit in await hits(store, "plain")] == ["artifact"]


async def test_rebuilding_the_mirror_from_disk_brings_every_artifact_back_identical(
    store, tmp_path
):
    data = pptx_bytes(tmp_path, "Design package", "Stretched cluster quokka")
    created = await store.create_artifact(upload("design.pptx", data, tags=["reference"]))
    link = await store.create_artifact(
        CreateArtifact(kind="artifact", url="https://example.test/q", title="Quokka link")
    )
    note = await store.create_note(
        CreateNote(
            title="Design review",
            frontmatter={
                "date": "2026-10-02",
                "type": "work",
                "context": "internal",
                "reviewed": True,
                "artifacts": [created.id, link.id],
            },
        )
    )
    await filing.file_reviewed(store)
    await indexer.catch_up(store)
    before_records = {
        a: (await store.get_artifact(a)).model_dump(exclude={"updated_at"})
        for a in (created.id, link.id)
    }
    # Two artifacts, plus the note whose body links the artifact by its title.
    before_hits = [(hit.kind, hit.id, hit.path) for hit in await hits(store, "quokka")]
    assert sorted(kind for kind, _, _ in before_hits) == ["artifact", "artifact", "note"]
    before_listing = (await store.list_artifacts(ArtifactQuery())).model_dump()

    for path in (store.control.store.state_dir / "artifacts").glob("*.json"):
        path.unlink()
    async with store.session_factory() as session:
        await session.execute(sa.text("TRUNCATE notes, artifacts, search_documents CASCADE"))
        await session.commit()

    counts = (await store.rebuild_metadata()).counts

    assert counts["recovered_artifacts"] == 2
    after_records = {
        a: (await store.get_artifact(a)).model_dump(exclude={"updated_at"})
        for a in (created.id, link.id)
    }
    assert after_records == before_records
    assert after_records[created.id]["citing_note_ids"] == [note.id]
    after_hits = [(hit.kind, hit.id, hit.path) for hit in await hits(store, "quokka")]
    assert sorted(after_hits) == sorted(before_hits)
    assert (await store.list_artifacts(ArtifactQuery())).model_dump() == before_listing
    assert record_path(store, created.id).exists() and record_path(store, link.id).exists()


async def test_filing_a_source_pdf_lands_it_under_artifacts_and_cites_it(store):
    request = ingest_request("pdf-source")
    request.source.artifacts.append(
        IngestArtifact(
            name="slides.pdf",
            mime_type="application/pdf",
            content_base64=encoded(minimal_pdf("Numbat architecture")),
        )
    )
    result = await store.ingest(request)
    await approve(store, result.note.id)
    await indexer.catch_up(store)

    folder = month_folder(store)
    filed = store.notes_root / folder / "slides.pdf"
    assert filed.read_bytes() == minimal_pdf("Numbat architecture")
    assert not (store.notes_root / "_Attachments").exists()
    note = await store.get_note(result.note.id)
    listed = await store.list_artifacts(ArtifactQuery(note_id=note.id))
    assert len(listed.items) == 1
    artifact = listed.items[0]
    assert artifact.origin == "source:plaud" and artifact.path == f"{folder}/slides.pdf"
    assert note.frontmatter["artifacts"] == [artifact.id]
    projection = store.notes_root / (await store.get_source(result.source.id)).projection_path
    assert f"[[{folder}/slides.pdf]]" in projection.read_text()
    # Searchable by its own extracted text, as an artifact, and the source page as a source.
    assert [hit.kind for hit in await hits(store, "numbat")] == ["artifact"]
    assert await filing.file_reviewed(store) == {"filed": 0, "filing_failed": 0}
    assert (await store.list_artifacts(ArtifactQuery())).items[0].id == artifact.id


async def test_legacy_attachments_move_into_the_artifacts_folder_and_notes_follow(
    store, monkeypatch
):
    request = ingest_request("legacy")
    request.source.artifacts.append(
        IngestArtifact(
            name="x-image-01.jpg", mime_type="image/jpeg", content_base64=encoded(b"jpeg")
        )
    )
    result = await store.ingest(request)
    legacy = f"_Attachments/2026-09/{result.source.id}-r0001-x-image-01.jpg"
    (store.notes_root / legacy).parent.mkdir(parents=True)
    (store.notes_root / legacy).write_bytes(b"jpeg")
    note = await store.create_note(
        CreateNote(
            title="Legacy embed",
            body=f"![[{legacy}]] and ![]({legacy})\n",
            folder="2026-09",
            frontmatter={"date": "2026-09-08", "type": "reference", "context": "internal"},
        )
    )

    def repoint_fails(*args, **kwargs):
        raise MetadataUnavailable("simulated outage during repointing")

    monkeypatch.setattr(artifacts, "_repoint_notes", repoint_fails)
    with pytest.raises(MetadataUnavailable):
        await artifacts.migrate_attachments(store)
    # Nothing was deleted before every reference moved, so the next pass can finish.
    assert (store.notes_root / legacy).read_bytes() == b"jpeg"
    assert f"![[{legacy}]]" in (await store.get_note(note.id)).body
    monkeypatch.undo()

    counts = await artifacts.migrate_attachments(store)

    assert counts == {"attachments_migrated": 1}
    assert not (store.notes_root / "_Attachments").exists()
    moved = await store.list_artifacts(ArtifactQuery())
    assert len(moved.items) == 1 and moved.items[0].origin == "source:plaud"
    new_path = moved.items[0].path
    assert new_path.endswith("/x-image-01.jpg") and new_path.startswith("Artifacts/")
    assert (store.notes_root / new_path).read_bytes() == b"jpeg"
    after = await store.get_note(note.id)
    assert after.body == f"# Legacy embed\n\n![[{new_path}]] and ![]({new_path})\n"
    assert after.frontmatter["artifacts"] == [moved.items[0].id]
    assert (await store.get_artifact(moved.items[0].id)).citing_note_ids == [note.id]
    # A second pass has nothing left to do.
    assert await artifacts.sweep(store) == {
        "artifacts_removed": 0,
        "artifacts_missing": 0,
        "artifacts_modified": 0,
        "artifacts_reextracted": 0,
    }


async def test_a_changed_upload_is_a_new_revision_and_identical_bytes_replay(store, tmp_path):
    one = pptx_bytes(tmp_path, "Deck", "Version one wallaby")
    created = await store.create_artifact(upload("deck.pptx", one))
    replay = await store.create_artifact(upload("deck.pptx", one))
    assert (replay.id, replay.revision, replay.created) == (created.id, 1, False)

    two = pptx_bytes(tmp_path, "Deck", "Version two wallaby")
    revised = await store.create_artifact(upload("deck.pptx", two, artifact_id=created.id))
    assert (revised.id, revised.revision, revised.created) == (created.id, 2, True)
    assert revised.path == created.path
    assert (store.notes_root / created.path).read_bytes() == two
    assert await drain(await store.get_artifact_bytes(created.id, revision=1)) == one
    assert await drain(await store.get_artifact_bytes(created.id)) == two
    record = await store.get_artifact(created.id)
    assert record.revision == 2 and [r.revision for r in record.revisions] == [1, 2]
    assert "two" in record.text and "one" not in record.text.split("Version")[0]
    assert [hit.id for hit in await hits(store, "wallaby")] == [created.id]
    with pytest.raises(ArtifactMissing):
        await store.create_artifact(
            upload("deck.pptx", two, artifact_id="01K4Q8Z2A0P1Q2R3S4T5U6V7W8")
        )
    assert notes_tree(store) == {created.path}


async def test_a_device_edit_to_the_browsable_copy_is_reported_never_rewritten(store, tmp_path):
    data = pptx_bytes(tmp_path, "Deck", "Original echidna")
    created = await store.create_artifact(upload("deck.pptx", data))
    (store.notes_root / created.path).write_bytes(b"edited on a device")

    swept = await artifacts.sweep(store, full=True)

    assert swept["artifacts_modified"] == 1
    assert (store.notes_root / created.path).read_bytes() == b"edited on a device"
    problems = [p for p in await store.get_problems() if p.kind == "artifact"]
    assert len(problems) == 1 and problems[0].artifact_id == created.id
    assert "no longer matches the archived bytes" in problems[0].reason
    assert (await store.get_artifact(created.id)).state == "modified"
    # Still searchable by the archived text, and the archive still serves the original.
    assert [hit.id for hit in await hits(store, "echidna")] == [created.id]
    assert await drain(await store.get_artifact_bytes(created.id)) == data
    # Removing in Admin leaves the edited file alone, because it is no longer the store's bytes.
    await store.remove_artifact(created.id)
    assert (store.notes_root / created.path).exists()
    assert await hits(store, "echidna") == []
    async with store.session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Artifact)) == 0
        assert (
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(SearchDocument)
                .where(SearchDocument.kind == "artifact")
            )
            == 0
        )


async def test_remove_takes_the_projection_and_keeps_the_archive(store, tmp_path):
    data = pptx_bytes(tmp_path, "Deck", "Removable bilby")
    created = await store.create_artifact(upload("deck.pptx", data))
    await store.remove_artifact(created.id)
    assert not (store.notes_root / created.path).exists()
    assert not record_path(store, created.id).exists()
    assert await hits(store, "bilby") == []
    assert (store.artifacts_root / created.id / "r0001" / "deck.pptx").read_bytes() == data
    with pytest.raises(ArtifactMissing):
        await store.remove_artifact(created.id)
    with pytest.raises(ArtifactMissing):
        await store.get_artifact(created.id)
    assert (await store.rebuild_metadata()).counts["recovered_artifacts"] == 0
    assert await hits(store, "bilby") == []


async def test_a_note_keeps_citing_an_artifact_that_was_removed_and_can_still_be_written(
    store, tmp_path
):
    gone = await store.create_artifact(
        upload("gone.pptx", pptx_bytes(tmp_path, "Gone", "Vanishing dingo"))
    )
    other = await store.create_artifact(upload("other.txt", b"second artifact\n"))
    note = await store.create_note(
        CreateNote(
            title="Cites both",
            frontmatter={"type": "reference", "context": "internal", "artifacts": [gone.id]},
        )
    )
    await store.remove_artifact(gone.id)

    current = await store.get_note(note.id)
    replaced = await store.replace_note(
        note.id,
        ReplaceNote(frontmatter=current.frontmatter, body=current.body + "\nedited\n"),
        current.content_hash,
    )
    assert replaced.frontmatter["artifacts"] == [gone.id]

    await artifacts.cite(store, note.id, [other.id])
    assert (await store.get_note(note.id)).frontmatter["artifacts"] == [gone.id, other.id]

    current = await store.get_note(note.id)
    with pytest.raises(ValidationFailed, match="no artifact with id"):
        await store.replace_note(
            note.id,
            ReplaceNote(
                frontmatter={**current.frontmatter, "artifacts": [gone.id, other.id, "0" * 26]},
                body=current.body,
            ),
            current.content_hash,
        )


async def test_the_received_day_filters_use_the_operators_zone(store):
    created = await store.create_artifact(upload("late.txt", b"received in the evening\n"))
    async with store.session_factory() as session:
        await session.execute(
            sa.update(Artifact)
            .where(Artifact.id == created.id)
            .values(received_at=datetime(2026, 11, 5, 2, 0, tzinfo=UTC))
        )
        await session.commit()
    on_the_fourth = await store.list_artifacts(
        ArtifactQuery.model_validate({"from": "2026-11-04", "to": "2026-11-04"})
    )
    assert [item.id for item in on_the_fourth.items] == [created.id]
    on_the_fifth = await store.list_artifacts(ArtifactQuery.model_validate({"from": "2026-11-05"}))
    assert on_the_fifth.items == []


async def test_unmatched_files_in_the_legacy_folder_do_not_starve_the_migration(store):
    request = ingest_request("starved")
    request.source.artifacts.append(
        IngestArtifact(
            name="x-image-01.jpg", mime_type="image/jpeg", content_base64=encoded(b"jpeg")
        )
    )
    result = await store.ingest(request)
    folder = store.notes_root / "_Attachments/2026-09"
    folder.mkdir(parents=True)
    for index in range(artifacts.MIGRATE_PER_PASS + 5):
        (folder / f"a-pasted-{index:03d}.png").write_bytes(b"pasted")
    (folder / f"{result.source.id}-r0001-x-image-01.jpg").write_bytes(b"jpeg")

    counts = await artifacts.migrate_attachments(store)

    assert counts == {"attachments_migrated": 1}
    assert len((await store.list_artifacts(ArtifactQuery())).items) == 1
    assert len(list(folder.iterdir())) == artifacts.MIGRATE_PER_PASS + 5


async def test_an_older_extractor_version_is_regenerated_by_the_sweep(store, tmp_path):
    data = pptx_bytes(tmp_path, "Deck", "Stale koala")
    created = await store.create_artifact(upload("deck.pptx", data))
    path = record_path(store, created.id)
    record = json.loads(path.read_text())
    record["extractor_version"] = "0"
    record["text"] = "old text"
    path.write_text(json.dumps(record))
    async with store.session_factory() as session:
        await session.execute(sa.update(Artifact).values(extractor_version="0"))
        await session.commit()

    swept = await artifacts.sweep(store)

    assert swept["artifacts_reextracted"] == 1
    refreshed = json.loads(path.read_text())
    assert refreshed["extractor_version"] == EXTRACTOR_VERSION and "koala" in refreshed["text"]
    await indexer.catch_up(store)
    assert [hit.id for hit in await hits(store, "koala")] == [created.id]


async def test_a_note_cannot_be_created_or_moved_into_the_artifacts_folder(store):
    with pytest.raises(ValidationFailed, match="Artifacts"):
        await store.create_note(
            CreateNote(title="Stray", folder="Artifacts/2026", frontmatter={"type": "reference"})
        )
    note = await store.create_note(CreateNote(title="Movable", frontmatter={"type": "reference"}))
    from coppermind.store_protocol import MoveNote

    with pytest.raises(ValidationFailed, match="Artifacts"):
        await store.move_note(note.id, MoveNote(target_folder="artifacts/2026/2026-11"), None)


async def test_a_dropbox_original_never_undoes_the_delete_of_its_browsable_copy(store, tmp_path):
    from tests.integration.test_note_recovery import commit, git

    data = pptx_bytes(tmp_path, "Dropped deck", "Platypus dropbox")
    created = await store.create_artifact(upload("deck.pptx", data, origin="dropbox"))
    dropped = store.notes_root / store.context.config().dropbox_folder / "deck.pptx"
    dropped.parent.mkdir(parents=True)
    dropped.write_bytes(data)
    git(store.notes_root, "init", "-q")
    commit(store.notes_root)
    (store.notes_root / created.path).unlink()
    removal = commit(store.notes_root)

    swept = await artifacts.sweep(store)

    assert swept["artifacts_removed"] == 1
    assert dropped.read_bytes() == data
    assert not (store.notes_root / created.path).exists()
    assert await store.list_reconciler_notices() == []
    manifest = json.loads((store.artifacts_root / created.id / "manifest.json").read_text())
    assert manifest["removed_reason"] == artifacts.DELETED_IN_OBSIDIAN
    assert manifest["removal_commit"] == removal


async def test_an_identical_file_that_predates_the_artifact_is_never_taken_as_its_move(
    store, tmp_path
):
    from tests.integration.test_note_recovery import commit, git

    data = pptx_bytes(tmp_path, "Twin", "Platypus predates")
    keeper = store.notes_root / "Reference" / "my own copy.pptx"
    keeper.parent.mkdir(parents=True)
    keeper.write_bytes(data)
    created = await store.create_artifact(upload("deck.pptx", data))
    git(store.notes_root, "init", "-q")
    commit(store.notes_root)
    (store.notes_root / created.path).unlink()
    commit(store.notes_root)

    swept = await artifacts.sweep(store)

    assert swept["artifacts_removed"] == 1
    assert keeper.read_bytes() == data
    assert not (store.notes_root / created.path).exists()
    assert await store.list_reconciler_notices() == []


async def test_a_large_artifact_is_served_and_checked_in_bounded_chunks(store, monkeypatch):
    from pathlib import Path

    data = bytes(range(256)) * (3 * 1024 * 1024 // 256) + b"tail"
    created = await store.create_artifact(upload("recording.m4a", data))

    def whole(self):
        raise AssertionError(f"read whole: {self.name}")

    monkeypatch.setattr(Path, "read_bytes", whole)
    served = await store.get_artifact_bytes(created.id)
    sizes = [len(chunk) async for chunk in served.content]
    assert sum(sizes) == len(data) == served.size_bytes
    assert len(sizes) >= 4 and max(sizes) <= artifacts.READ_CHUNK_BYTES

    await artifacts.sweep(store, full=True)
    assert (await store.get_artifact(created.id)).state == "ok"
    assert (await artifacts.recover_artifacts(store)) is not None


async def test_an_archive_that_fails_verification_is_refused_before_any_byte_is_served(store):
    created = await store.create_artifact(upload("recording.m4a", b"a" * 4096))
    archived = store.artifacts_root / created.id / "r0001" / "recording.m4a"
    archived.write_bytes(b"b" * 4096)
    with pytest.raises(artifacts.ArtifactsFilesystemUnavailable, match="verification"):
        await store.get_artifact_bytes(created.id)
