"""Review gates publication; monthly filing and restore preserve evidence."""

import base64
import json
from datetime import UTC, datetime

import sqlalchemy as sa
from coppermind_store import filing, indexer
from coppermind_store.eligibility import reviewed_note
from coppermind_store.reconciler import reconcile_once

from coppermind.db.models import Note, NoteSource, SearchDocument, Source
from coppermind.store_protocol import (
    IngestArtifact,
    MoveNote,
    PatchFrontmatter,
    ReplaceNote,
    SearchQuery,
)
from tests.integration.test_search_index import ingest_request


async def approve(store, note_id):
    note = await store.get_note(note_id)
    await store.patch_frontmatter(
        note_id, PatchFrontmatter(set={"reviewed": True}), note.content_hash
    )
    return await filing.file_reviewed(store)


async def test_review_files_monthly_then_publishes_and_human_edit_stays_reviewed(store):
    request = ingest_request()
    request.note.frontmatter["reviewed"] = True
    result = await store.ingest(request)
    assert result.projection_path.startswith("_Sources/_Pending/2026/2026-09/")
    assert (await store.get_note(result.note.id)).frontmatter["reviewed"] is False
    await indexer.rebuild(store)
    assert not (await store.search(SearchQuery(q="kerfuffle"))).items
    async with store.session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(SearchDocument)) == 0
    assert await approve(store, result.note.id) == {"filed": 1, "filing_failed": 0}
    await indexer.catch_up(store)
    note = await store.get_note(result.note.id)
    source = await store.get_source(result.source.id)
    assert note.path.startswith("Notes/2026/2026-09/")
    assert source.projection_path.startswith("_Sources/2026/2026-09/")
    assert source.projection_path.removesuffix(".md") in note.body
    assert not (store.notes_root / result.projection_path).exists()
    assert (await store.search(SearchQuery(q="kerfuffle"))).items[0].id == result.source.id
    path = store.notes_root / note.path
    path.write_text(path.read_text() + "\nA human adds platypus.\n")
    await reconcile_once(store)
    assert (await store.get_note(note.id)).frontmatter["reviewed"] is True
    assert (await store.search(SearchQuery(q="platypus"))).items[0].id == note.id
    await store.patch_frontmatter(
        note.id,
        PatchFrontmatter(set={"reviewed": False}),
        (await store.get_note(note.id)).content_hash,
    )
    await indexer.catch_up(store)
    assert not (await store.search(SearchQuery(q="platypus"))).items
    assert not (await store.search(SearchQuery(q="kerfuffle"))).items


async def test_failed_filing_retries_without_indexing_or_losing_originals(store, monkeypatch):
    result = await store.ingest(ingest_request())
    original = (store.sources_root / result.source.id / "r0001/transcript.txt").read_bytes()
    copy = filing._copy

    def fail(*args):
        raise OSError("simulated volume failure")

    monkeypatch.setattr(filing, "_copy", fail)
    assert await approve(store, result.note.id) == {"filed": 0, "filing_failed": 1}
    assert (await store.get_note(result.note.id)).path.startswith("Review/")
    assert any(p.kind == "filing" for p in await store.get_problems())
    await indexer.rebuild(store)
    assert not (await store.search(SearchQuery(q="lease"))).items
    moved = await store.move_note(
        result.note.id, MoveNote(target_folder="Notes/2026/2026-09"), None
    )
    assert not (await store.search(SearchQuery(q="lease"))).items
    await store.patch_frontmatter(
        moved.id,
        PatchFrontmatter(set={"time": "2026-10-02T12:00:00"}, unset=["date"]),
        moved.content_hash,
    )
    monkeypatch.setattr(filing, "_copy", copy)
    assert await filing.file_reviewed(store) == {"filed": 1, "filing_failed": 0}
    assert not any(p.kind == "filing" for p in await store.get_problems())
    assert (await store.get_note(result.note.id)).path.startswith("Notes/2026/2026-10/")
    assert (await store.get_source(result.source.id)).projection_path.startswith(
        "_Sources/2026/2026-10/"
    )
    assert (store.sources_root / result.source.id / "r0001/transcript.txt").read_bytes() == original
    assert await filing.file_reviewed(store) == {"filed": 0, "filing_failed": 0}


async def test_pdf_is_browsable_audio_stays_archived_and_database_loss_recovers(store):
    request = ingest_request()
    for name, mime in [("slides.pdf", "application/pdf"), ("recording.wav", "audio/wav")]:
        request.source.artifacts.append(
            IngestArtifact(
                name=name,
                mime_type=mime,
                content_base64=base64.b64encode(b"synthetic bytes").decode(),
            )
        )
    result = await store.ingest(request)
    await approve(store, result.note.id)
    paths = list((store.notes_root / "_Attachments").rglob("*"))
    assert any(p.suffix == ".pdf" for p in paths)
    assert not any(p.suffix == ".wav" for p in paths)
    snapshot = {
        p.relative_to(store.sources_root): p.read_bytes()
        for p in store.sources_root.rglob("*")
        if p.is_file()
    }
    async with store.session_factory() as session:
        await session.execute(sa.text("TRUNCATE notes, sources, search_documents CASCADE"))
        await session.commit()
    recovered = await store.rebuild_metadata()
    assert recovered.counts["recovered_sources"] == 1
    async with store.session_factory() as session:
        for model in (Note, Source, NoteSource):
            assert await session.scalar(sa.select(sa.func.count()).select_from(model)) == 1
    assert (await store.search(SearchQuery(q="kerfuffle"))).items
    assert snapshot == {
        p.relative_to(store.sources_root): p.read_bytes()
        for p in store.sources_root.rglob("*")
        if p.is_file()
    }
    replay = await store.ingest(request)
    assert replay.note.id == result.note.id
    assert not replay.note.created


async def test_source_revision_is_not_silently_published(store):
    request = ingest_request()
    result = await store.ingest(request)
    await approve(store, result.note.id)
    await indexer.catch_up(store)
    request.source.artifacts[0].content = "New unreviewed aardvark material."
    await store.ingest(request)
    await indexer.rebuild(store)
    assert not (await store.search(SearchQuery(q="aardvark"))).items
    assert (await store.get_note(result.note.id)).frontmatter["reviewed"] is True


async def test_restore_skips_malformed_source_list(store):
    from coppermind_store.recovery import recover_sources

    from coppermind.ids import new_id

    result = await store.ingest(ingest_request())
    (store.notes_root / "bad.md").write_text(
        f"---\nid: {new_id()}\nsources: [{{}}]\n---\nMalformed citations\n"
    )
    assert (await recover_sources(store))["recovered_sources"] == 1
    assert (await store.get_note(result.note.id)).id == result.note.id


async def test_added_source_requires_filing_before_publication(store):
    from coppermind import frontmatter as fm

    first = await store.ingest(ingest_request("first"))
    second = await store.ingest(ingest_request("second"))
    await approve(store, first.note.id)
    note = await store.get_note(first.note.id)
    path = store.notes_root / note.path
    frontmatter, body = fm.parse(path.read_text())
    frontmatter["sources"].append(second.source.id)
    path.write_text(fm.compose(frontmatter, body))
    await reconcile_once(store)
    assert not (await store.search(SearchQuery(q="lease discussion"))).items
    updated = await store.get_note(note.id)
    await store.patch_frontmatter(
        updated.id, PatchFrontmatter(set={"reviewed": True}), updated.content_hash
    )
    assert await filing.file_reviewed(store) == {"filed": 1, "filing_failed": 0}
    await indexer.catch_up(store)
    assert len((await store.search(SearchQuery(q="kerfuffle"))).items) == 2
    assert (await store.get_source(second.source.id)).projection_path.startswith(
        "_Sources/2026/2026-09/"
    )


async def test_date_correction_preserves_canonical_source_path(store):
    result = await store.ingest(ingest_request())
    await approve(store, result.note.id)
    await indexer.catch_up(store)
    note = await store.get_note(result.note.id)
    await store.patch_frontmatter(
        note.id,
        PatchFrontmatter(set={"time": "2026-10-01T12:00:00"}, unset=["date"]),
        note.content_hash,
    )
    assert await filing.file_reviewed(store) == {"filed": 1, "filing_failed": 0}
    await indexer.catch_up(store)
    hits = (await store.search(SearchQuery(q="kerfuffle"))).items
    assert len(hits) == 1
    assert hits[0].path.startswith("_Sources/2026/2026-09/")
    assert (await store.get_note(note.id)).path.startswith("Notes/2026/2026-10/")
    assert hits[0].path.removesuffix(".md") in (await store.get_note(note.id)).body


async def test_completed_publication_retry_clears_error(store, monkeypatch):
    result = await store.ingest(ingest_request())
    publish = filing._publish

    async def fail(*args):
        raise ValueError("simulated content-hash race")

    monkeypatch.setattr(filing, "_publish", fail)
    assert (await approve(store, result.note.id))["filing_failed"] == 1
    monkeypatch.setattr(filing, "_publish", publish)
    assert (await filing.file_reviewed(store))["filing_failed"] == 0
    assert not any(p.kind == "filing" for p in await store.get_problems())


async def test_duplicate_citations_file_and_recover_once(store):
    from coppermind import frontmatter as fm

    result = await store.ingest(ingest_request())
    note = await store.get_note(result.note.id)
    path = store.notes_root / note.path
    metadata, body = fm.parse(path.read_text())
    metadata["sources"].append(result.source.id)
    path.write_text(fm.compose(metadata, body))
    await reconcile_once(store)
    assert (await approve(store, note.id))["filing_failed"] == 0
    async with store.session_factory() as session:
        await session.execute(sa.text("TRUNCATE notes, sources, search_documents CASCADE"))
        await session.commit()
    await store.rebuild_metadata()
    async with store.session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(NoteSource)) == 1


async def test_shared_source_retains_first_filed_path(store):
    from coppermind import frontmatter as fm

    first = await store.ingest(ingest_request("first-shared"))
    second = await store.ingest(ingest_request("second-shared"))
    await approve(store, first.note.id)
    original = (await store.get_source(first.source.id)).projection_path
    note = await store.get_note(second.note.id)
    path = store.notes_root / note.path
    metadata, body = fm.parse(path.read_text())
    metadata["date"] = "2026-10-01"
    metadata["sources"].append(first.source.id)
    path.write_text(fm.compose(metadata, body + f"\n[[{original.removesuffix('.md')}]]\n"))
    await reconcile_once(store)
    assert (await approve(store, second.note.id))["filing_failed"] == 0
    assert (await store.get_source(first.source.id)).projection_path == original
    assert (store.notes_root / original).is_file()
    for note_id in (first.note.id, second.note.id):
        assert original.removesuffix(".md") in (await store.get_note(note_id)).body


async def test_source_revision_requires_explicit_rereview(store):
    request = ingest_request("revision-review")
    result = await store.ingest(request)
    await approve(store, result.note.id)
    request.source.artifacts[0].content = "New unreviewed aardvark material."
    await store.ingest(request)
    note = await store.get_note(result.note.id)
    await store.patch_frontmatter(
        note.id,
        PatchFrontmatter(set={"time": "2026-10-01T12:00:00"}, unset=["date"]),
        note.content_hash,
    )
    await filing.file_reviewed(store)
    await indexer.catch_up(store)
    assert not (await store.search(SearchQuery(q="aardvark"))).items
    note = await store.get_note(note.id)
    await store.patch_frontmatter(
        note.id, PatchFrontmatter(set={"reviewed": False}), note.content_hash
    )
    assert (await approve(store, note.id))["filing_failed"] == 0
    await indexer.catch_up(store)
    assert (await store.search(SearchQuery(q="aardvark"))).items


async def test_device_rereview_approves_revised_pdf_without_overwriting_old_copy(store):
    request = ingest_request("device-rereview")
    request.source.artifacts.append(
        IngestArtifact(name="slides.pdf", mime_type="application/pdf", content="version one")
    )
    result = await store.ingest(request)
    await approve(store, result.note.id)
    original_pdf = next((store.notes_root / "_Attachments").rglob("*.pdf"))
    request.source.artifacts[0].content = "Revised aardvark transcript"
    request.source.artifacts[1].content = "version two"
    await store.ingest(request)
    note = await store.get_note(result.note.id)
    path = store.notes_root / note.path
    path.write_text(path.read_text().replace("reviewed: true", "reviewed: false"))
    await reconcile_once(store)
    path.write_text(path.read_text().replace("reviewed: false", "reviewed: true"))
    await reconcile_once(store)
    assert (await filing.file_reviewed(store))["filing_failed"] == 0
    await indexer.catch_up(store)
    assert (await store.search(SearchQuery(q="aardvark"))).items
    assert original_pdf.read_bytes() == b"version one"
    assert {p.read_bytes() for p in (store.notes_root / "_Attachments").rglob("*.pdf")} == {
        b"version one",
        b"version two",
    }


async def test_approval_does_not_cover_a_revision_arriving_before_filing(store):
    request = ingest_request("approval-race")
    result = await store.ingest(request)
    note = await store.get_note(result.note.id)
    await store.patch_frontmatter(
        note.id, PatchFrontmatter(set={"reviewed": True}), note.content_hash
    )
    request.source.artifacts[0].content = "Late aardvark transcript"
    await store.ingest(request)
    assert (await filing.file_reviewed(store))["filing_failed"] == 1
    await indexer.catch_up(store)
    assert not (await store.search(SearchQuery(q="aardvark"))).items
    # An explicit mark-reviewed API action can approve material even if the flag is already true.
    assert (await approve(store, note.id))["filing_failed"] == 0
    await indexer.catch_up(store)
    assert (await store.search(SearchQuery(q="aardvark"))).items


async def test_recovery_does_not_approve_pending_source_revision(store):
    request = ingest_request("restore-review")
    result = await store.ingest(request)
    await approve(store, result.note.id)
    request.source.artifacts[0].content = "Unreviewed aardvark revision"
    await store.ingest(request)
    async with store.session_factory() as session:
        await session.execute(sa.text("TRUNCATE notes, sources, search_documents CASCADE"))
        await session.commit()
    await store.rebuild_metadata()
    assert not (await store.search(SearchQuery(q="aardvark"))).items
    assert (await approve(store, result.note.id))["filing_failed"] == 0
    await indexer.catch_up(store)
    assert (await store.search(SearchQuery(q="aardvark"))).items


async def test_revoking_review_cannot_fall_back_to_another_notes_older_approval(store):
    from coppermind import frontmatter as fm

    request = ingest_request("shared-revisions")
    first = await store.ingest(request)
    second = await store.ingest(ingest_request("second-reviewer"))
    await approve(store, first.note.id)
    note = await store.get_note(second.note.id)
    path = store.notes_root / note.path
    metadata, body = fm.parse(path.read_text())
    metadata["sources"].append(first.source.id)
    path.write_text(fm.compose(metadata, body))
    await reconcile_once(store)
    await approve(store, second.note.id)
    request.source.artifacts[0].content = "Aardvark revision two"
    await store.ingest(request)
    await approve(store, second.note.id)
    await indexer.catch_up(store)
    assert (await store.search(SearchQuery(q="aardvark"))).items
    note = await store.get_note(second.note.id)
    await store.patch_frontmatter(
        note.id, PatchFrontmatter(set={"reviewed": False}), note.content_hash
    )
    assert not (await store.search(SearchQuery(q="aardvark"))).items


def image_request(
    external_id="image-embeds", data=b"synthetic jpeg one", embed="![[x-image-01.jpg]]"
):
    request = ingest_request(external_id)
    request.note.body = f"Opening note.\n\n{embed}\n\n![](unrelated.jpg)\n"
    request.source.artifacts.append(
        IngestArtifact(
            name="x-image-01.jpg",
            mime_type="image/jpeg",
            content_base64=base64.b64encode(data).decode(),
        )
    )
    return request


async def test_filing_rewrites_embeds_to_the_filed_attachment(store):
    result = await store.ingest(image_request())
    before = await store.get_note(result.note.id)
    assert "![[x-image-01.jpg]]" in before.body
    await approve(store, result.note.id)
    filed = f"_Attachments/2026-09/{result.source.id}-r0001-x-image-01.jpg"
    note = await store.get_note(result.note.id)
    assert f"![[{filed}]]" in note.body
    assert "![](unrelated.jpg)" in note.body
    assert (store.notes_root / filed).read_bytes() == b"synthetic jpeg one"
    assert await filing.file_reviewed(store) == {"filed": 0, "filing_failed": 0}
    assert (await store.get_note(result.note.id)).body == note.body


async def test_markdown_embed_is_rewritten_and_unmatched_note_is_unchanged(store):
    request = image_request("md-embed", embed="![](x-image-01.jpg)")
    result = await store.ingest(request)
    await approve(store, result.note.id)
    filed = f"_Attachments/2026-09/{result.source.id}-r0001-x-image-01.jpg"
    assert f"![]({filed})" in (await store.get_note(result.note.id)).body
    plain = await store.ingest(ingest_request("no-images"))
    body = (await store.get_note(plain.note.id)).body
    await approve(store, plain.note.id)
    source = await store.get_source(plain.source.id)
    assert (await store.get_note(plain.note.id)).body == body.replace(
        plain.projection_path.removesuffix(".md"), source.projection_path.removesuffix(".md")
    )


async def test_replayed_ingest_with_identical_images_adds_no_revision(store):
    request = image_request("replay-images")
    result = await store.ingest(request)
    replay = await store.ingest(request)
    assert replay.source.id == result.source.id
    assert replay.source.revision == 1
    assert not replay.note.created


async def test_new_revision_repoints_embeds_to_the_current_attachments(store):
    result = await store.ingest(image_request("revised-images"))
    await approve(store, result.note.id)
    revised = await store.ingest(image_request("revised-images", data=b"synthetic jpeg two"))
    assert revised.source.revision == 2
    note = await store.get_note(result.note.id)
    path = store.notes_root / note.path
    path.write_text(path.read_text().replace("reviewed: true", "reviewed: false"))
    await reconcile_once(store)
    path.write_text(path.read_text().replace("reviewed: false", "reviewed: true"))
    await reconcile_once(store)
    assert (await filing.file_reviewed(store))["filing_failed"] == 0
    current = f"_Attachments/2026-09/{result.source.id}-r0002-x-image-01.jpg"
    old = f"_Attachments/2026-09/{result.source.id}-r0001-x-image-01.jpg"
    body = (await store.get_note(result.note.id)).body
    assert f"![[{current}]]" in body
    assert f"![[{old}]]" not in body
    assert (store.notes_root / old).read_bytes() == b"synthetic jpeg one"
    assert (store.notes_root / current).read_bytes() == b"synthetic jpeg two"


async def _eligible_ids(store):
    async with store.session_factory() as session:
        root = store.control.settings().notes.notes_folder
        return set((await session.scalars(sa.select(Note.id).where(reviewed_note(root)))).all())


async def test_a_reviewed_note_files_under_the_notes_root_by_year_and_month(store):
    request = ingest_request()
    request.note.frontmatter["date"] = "2026-04-13"
    result = await store.ingest(request)
    assert result.projection_path.startswith("_Sources/_Pending/2026/2026-04/")
    await approve(store, result.note.id)
    await indexer.catch_up(store)
    note = await store.get_note(result.note.id)
    assert note.path == "Notes/2026/2026-04/2026-04-13 Warehouse call.md"
    receipt = json.loads(
        (store.control.store.state_dir / "filing" / f"{note.id}.json").read_bytes()
    )
    assert receipt["folder"] == "Notes/2026/2026-04"
    assert (await store.search(SearchQuery(q="kerfuffle"))).items
    status = await store.get_status()
    assert status.counters.notes_folder == "Notes"
    assert status.counters.notes_to_refile == 0


async def test_upgrade_carries_flat_filed_source_pages_into_their_year_folder(store):
    result = await store.ingest(ingest_request())
    await approve(store, result.note.id)
    await indexer.catch_up(store)
    nested = (await store.get_source(result.source.id)).projection_path
    assert nested.startswith("_Sources/2026/2026-09/")
    flat = nested.replace("_Sources/2026/", "_Sources/")
    (store.notes_root / flat).parent.mkdir(parents=True)
    (store.notes_root / nested).rename(store.notes_root / flat)
    manifest_path = store.sources_root / result.source.id / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["projection_path"] = manifest["filed_projection_path"] = flat
    manifest_path.write_text(json.dumps(manifest))
    note = await store.get_note(result.note.id)
    link = nested.removesuffix(".md")
    assert link in note.body
    await store.replace_note(
        note.id,
        ReplaceNote(
            frontmatter=note.frontmatter,
            body=note.body.replace(link, flat.removesuffix(".md")),
        ),
        note.content_hash,
    )
    assert await filing.file_reviewed(store) == {"filed": 1, "filing_failed": 0}
    await indexer.catch_up(store)
    moved = await store.get_source(result.source.id)
    assert moved.projection_path == nested
    assert (store.notes_root / nested).exists()
    assert not (store.notes_root / flat).exists()
    body = (await store.get_note(note.id)).body
    assert link in body
    assert flat.removesuffix(".md") not in body
    assert not (store.notes_root / flat).parent.exists()
    hits = (await store.search(SearchQuery(q="kerfuffle"))).items
    assert [hit.id for hit in hits] == [result.source.id]
    assert hits[0].path == nested
    assert await filing.file_reviewed(store) == {"filed": 0, "filing_failed": 0}


async def test_a_retried_migration_rewrites_the_link_an_interrupted_attempt_left(store):
    result = await store.ingest(ingest_request())
    await approve(store, result.note.id)
    nested = (await store.get_source(result.source.id)).projection_path
    flat = nested.replace("_Sources/2026/", "_Sources/")
    note = await store.get_note(result.note.id)
    # The interrupted attempt moved the page and its manifest to the nested path
    # but died before rewriting the note, which still links the flat page.
    await store.replace_note(
        note.id,
        ReplaceNote(
            frontmatter=note.frontmatter,
            body=note.body.replace(nested.removesuffix(".md"), flat.removesuffix(".md")),
        ),
        note.content_hash,
    )
    journal = store.control.store.state_dir / "filing" / f"{note.id}.json"
    receipt = json.loads(journal.read_bytes())
    receipt["complete"] = False
    receipt["sources"] = [
        {
            "id": result.source.id,
            "from": flat,
            "link_from": "_Sources/_Pending/2026/2026-09/original",
            "to": nested,
            "revision": 1,
        }
    ]
    journal.write_text(json.dumps(receipt))
    assert flat.removesuffix(".md") in (await store.get_note(note.id)).body
    assert await filing.file_reviewed(store) == {"filed": 1, "filing_failed": 0}
    body = (await store.get_note(note.id)).body
    assert nested.removesuffix(".md") in body
    assert flat.removesuffix(".md") not in body


async def test_upgrade_keeps_the_first_filing_notes_link_when_a_sharing_note_moves_the_page(
    store,
):
    from coppermind import frontmatter as fm

    first = await store.ingest(ingest_request("first-shared"))
    second = await store.ingest(ingest_request("second-shared"))
    late, early = sorted((first.note.id, second.note.id), reverse=True)
    await approve(store, late)
    source_id = (first if first.note.id == late else second).source.id
    nested = (await store.get_source(source_id)).projection_path
    sharer = await store.get_note(early)
    path = store.notes_root / sharer.path
    metadata, body = fm.parse(path.read_text())
    metadata["sources"].append(source_id)
    path.write_text(fm.compose(metadata, body + f"\n[[{nested.removesuffix('.md')}]]\n"))
    await reconcile_once(store)
    assert (await approve(store, early))["filing_failed"] == 0

    flat = nested.replace("_Sources/2026/", "_Sources/")
    (store.notes_root / flat).parent.mkdir(parents=True)
    (store.notes_root / nested).rename(store.notes_root / flat)
    manifest_path = store.sources_root / source_id / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["projection_path"] = manifest["filed_projection_path"] = flat
    manifest_path.write_text(json.dumps(manifest))
    for note_id in (late, early):
        note = await store.get_note(note_id)
        await store.replace_note(
            note_id,
            ReplaceNote(
                frontmatter=note.frontmatter,
                body=note.body.replace(nested.removesuffix(".md"), flat.removesuffix(".md")),
            ),
            note.content_hash,
        )
        journal = store.control.store.state_dir / "filing" / f"{note_id}.json"
        receipt = json.loads(journal.read_bytes())
        for item in receipt["sources"]:
            if item["id"] == source_id:
                item["to"] = flat
                item["from"] = (
                    "_Sources/_Pending/2026/2026-09/original" if note_id == late else flat
                )
                item["link_from"] = item["from"]
                item.pop("link_froms", None)
        journal.write_text(json.dumps(receipt))

    assert await filing.file_reviewed(store) == {"filed": 2, "filing_failed": 0}
    for note_id in (late, early):
        body = (await store.get_note(note_id)).body
        assert nested.removesuffix(".md") in body
        assert flat.removesuffix(".md") not in body
    assert (store.notes_root / nested).is_file()


async def test_a_configured_notes_root_governs_filing_search_and_indexing(store):
    state = store.control.store
    settings = store.control.settings()
    settings.notes.notes_folder = "Filed"
    state.write("settings", settings.model_dump(), if_revision=state.read("settings").revision)
    request = ingest_request()
    request.note.frontmatter["date"] = "2026-04-13"
    result = await store.ingest(request)
    await approve(store, result.note.id)
    await indexer.catch_up(store)
    note = await store.get_note(result.note.id)
    assert note.path == "Filed/2026/2026-04/2026-04-13 Warehouse call.md"
    assert note.id in await _eligible_ids(store)
    assert (await store.search(SearchQuery(q="kerfuffle"))).items
    assert (await store.get_status()).counters.notes_folder == "Filed"


async def test_upgrade_refiles_legacy_notes_and_search_never_empties(store):
    result = await store.ingest(ingest_request())
    await approve(store, result.note.id)
    await indexer.catch_up(store)
    note = await store.get_note(result.note.id)
    # Recreate the pre-upgrade shape: a flat month folder and a receipt without a folder.
    legacy = await store.move_note(note.id, MoveNote(target_folder="2026-09"), None)
    assert legacy.path.startswith("2026-09/")
    journal = store.control.store.state_dir / "filing" / f"{note.id}.json"
    receipt = json.loads(journal.read_bytes())
    receipt.pop("folder")
    journal.write_text(json.dumps(receipt))
    await reconcile_once(store)
    async with store.session_factory() as session:
        await session.execute(sa.update(Note).where(Note.id == note.id).values(filed=True))
    # Before the pass the legacy shape is still eligible, and the Admin counts the move.
    assert note.id in await _eligible_ids(store)
    assert (await store.get_status()).counters.notes_to_refile == 1
    assert await filing.file_reviewed(store) == {"filed": 1, "filing_failed": 0}
    moved = await store.get_note(note.id)
    assert moved.path.startswith("Notes/2026/2026-09/")
    assert note.id in await _eligible_ids(store)
    assert moved.sources == note.sources
    assert (await store.get_status()).counters.notes_to_refile == 0
    assert not (store.notes_root / "2026-09").exists()
    await indexer.catch_up(store)
    assert (await store.search(SearchQuery(q="kerfuffle"))).items
    # A second pass has nothing left to move and records no notice.
    assert await filing.file_reviewed(store) == {"filed": 0, "filing_failed": 0}
    assert store.control.read_reconciler_notices() == {}


async def test_a_hand_moved_note_returns_with_a_reconciler_notice(store):
    result = await store.ingest(ingest_request())
    await approve(store, result.note.id)
    note = await store.get_note(result.note.id)
    source = store.notes_root / note.path
    elsewhere = store.notes_root / "Elsewhere"
    elsewhere.mkdir()
    source.rename(elsewhere / source.name)
    await reconcile_once(store)
    assert (await store.get_note(note.id)).path == f"Elsewhere/{source.name}"
    assert await filing.file_reviewed(store) == {"filed": 1, "filing_failed": 0}
    returned = await store.get_note(note.id)
    assert returned.path == note.path
    [notice] = await store.list_reconciler_notices()
    assert notice.note_id == note.id
    assert notice.title == note.title
    assert notice.from_path == f"Elsewhere/{source.name}"
    assert notice.path == note.path
    assert "Notes/2026/2026-09" in notice.reason
    assert (await store.get_status()).counters.notes_returned == 1


async def test_a_hand_move_notice_survives_a_failed_first_return(store, monkeypatch):
    result = await store.ingest(ingest_request())
    await approve(store, result.note.id)
    note = await store.get_note(result.note.id)
    source = store.notes_root / note.path
    (store.notes_root / "Elsewhere").mkdir()
    source.rename(store.notes_root / "Elsewhere" / source.name)
    await reconcile_once(store)
    move_note = store.move_note
    failures: list[bool] = []

    async def failing_once(*args, **kwargs):
        if not failures:
            failures.append(True)
            raise ValueError("the destination is busy")
        return await move_note(*args, **kwargs)

    monkeypatch.setattr(store, "move_note", failing_once)
    assert await filing.file_reviewed(store) == {"filed": 0, "filing_failed": 1}
    assert await store.list_reconciler_notices() == []
    assert await filing.file_reviewed(store) == {"filed": 1, "filing_failed": 0}
    [notice] = await store.list_reconciler_notices()
    assert notice.from_path == f"Elsewhere/{source.name}"
    assert (await store.get_note(note.id)).path == note.path
    assert await filing.file_reviewed(store) == {"filed": 0, "filing_failed": 0}


async def test_old_month_folders_are_removed_when_empty_and_reported_when_not(store):
    kept = [store.notes_root / "2026-08", store.notes_root / "_Sources" / "2026-08"]
    empty = [store.notes_root / "2026-07", store.notes_root / "_Sources" / "2026-07"]
    for folder in (*kept, *empty):
        folder.mkdir(parents=True)
    for folder in kept:
        (folder / "keep.txt").write_text("mine")
    await filing.file_reviewed(store)
    assert not any(folder.exists() for folder in empty)
    assert all((folder / "keep.txt").read_text() == "mine" for folder in kept)
    notices = {n.note_id: n for n in await store.list_reconciler_notices()}
    assert set(notices) == {"folder:2026-08", "folder:_Sources/2026-08"}
    assert {n.kind for n in notices.values()} == {"folder"}
    assert "still holds files" in notices["folder:2026-08"].reason
    counters = (await store.get_status()).counters
    assert (counters.folders_kept, counters.notes_returned) == (2, 0)
    await filing.file_reviewed(store)
    assert len(await store.list_reconciler_notices()) == 2
    for folder in kept:
        (folder / "keep.txt").unlink()
    await filing.file_reviewed(store)
    assert not any(folder.exists() for folder in kept)
    assert await store.list_reconciler_notices() == []
    assert (await store.get_status()).counters.folders_kept == 0


async def test_a_folder_notice_is_dropped_once_the_folder_is_gone(store):
    folder = store.notes_root / "2026-08"
    folder.mkdir()
    (folder / "keep.txt").write_text("mine")
    await filing.file_reviewed(store)
    assert len(await store.list_reconciler_notices()) == 1
    (folder / "keep.txt").unlink()
    folder.rmdir()
    assert await store.list_reconciler_notices() == []
    assert store.control.read_reconciler_notices() == {}


async def _hand_move_and_return(store):
    result = await store.ingest(ingest_request())
    await approve(store, result.note.id)
    note = await store.get_note(result.note.id)
    source = store.notes_root / note.path
    (store.notes_root / "Elsewhere").mkdir()
    source.rename(store.notes_root / "Elsewhere" / source.name)
    await reconcile_once(store)
    assert await filing.file_reviewed(store) == {"filed": 1, "filing_failed": 0}
    return note


async def test_a_notice_is_dropped_when_its_note_no_longer_exists(store):
    note = await _hand_move_and_return(store)
    assert (await store.get_status()).counters.notes_returned == 1
    (store.notes_root / (await store.get_note(note.id)).path).unlink()
    await reconcile_once(store)
    assert await store.list_reconciler_notices() == []
    assert (await store.get_status()).counters.notes_returned == 0
    assert store.control.read_reconciler_notices() == {}


async def test_dismissing_a_notice_clears_it_and_only_it(store):
    note = await _hand_move_and_return(store)
    other = store.control
    other.record_reconciler_notice(
        note_id="01K4Q8Z3N7V2X9M1B5C6D8E0F9",
        title="Other",
        from_path="A/other.md",
        path="Notes/other.md",
        folder="Notes",
        reason="because",
        at=datetime.now(tz=UTC),
    )
    await store.dismiss_reconciler_notice(note.id)
    assert set(store.control.read_reconciler_notices()) == {"01K4Q8Z3N7V2X9M1B5C6D8E0F9"}
    await store.dismiss_reconciler_notice(note.id)
    assert (await store.get_status()).counters.notes_returned == 0


async def test_a_rebuild_from_disk_yields_the_same_eligibility_set(store):
    first = await store.ingest(ingest_request("rec-1"))
    second_request = ingest_request("rec-2")
    second_request.note.frontmatter["date"] = "2026-04-13"
    second = await store.ingest(second_request)
    for result in (first, second):
        await approve(store, result.note.id)
    before = await _eligible_ids(store)
    assert before == {first.note.id, second.note.id}
    paths_before = {(await store.get_note(r.note.id)).path for r in (first, second)}
    async with store.session_factory() as session:
        await session.execute(sa.text("TRUNCATE notes, sources, search_documents CASCADE"))
        await session.commit()
    await store.rebuild_metadata()
    assert await _eligible_ids(store) == before
    assert {(await store.get_note(r.note.id)).path for r in (first, second)} == paths_before
    assert all(path.startswith("Notes/") for path in paths_before)
