"""Review gates publication; monthly filing and restore preserve evidence."""

import base64

import sqlalchemy as sa
from coppermind_store import filing, indexer
from coppermind_store.reconciler import reconcile_once

from coppermind.db.models import Note, NoteSource, SearchDocument, Source
from coppermind.store_protocol import IngestArtifact, MoveNote, PatchFrontmatter, SearchQuery
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
    assert result.projection_path.startswith("_Sources/_Pending/2026-09/")
    assert (await store.get_note(result.note.id)).frontmatter["reviewed"] is False
    await indexer.rebuild(store)
    assert not (await store.search(SearchQuery(q="kerfuffle"))).items
    async with store.session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(SearchDocument)) == 0
    assert await approve(store, result.note.id) == {"filed": 1, "filing_failed": 0}
    await indexer.catch_up(store)
    note = await store.get_note(result.note.id)
    source = await store.get_source(result.source.id)
    assert note.path.startswith("2026-09/")
    assert source.projection_path.startswith("_Sources/2026-09/")
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
    moved = await store.move_note(result.note.id, MoveNote(target_folder="2026-09"), None)
    assert not (await store.search(SearchQuery(q="lease"))).items
    await store.patch_frontmatter(
        moved.id,
        PatchFrontmatter(set={"time": "2026-10-02T12:00:00"}, unset=["date"]),
        moved.content_hash,
    )
    monkeypatch.setattr(filing, "_copy", copy)
    assert await filing.file_reviewed(store) == {"filed": 1, "filing_failed": 0}
    assert not any(p.kind == "filing" for p in await store.get_problems())
    assert (await store.get_note(result.note.id)).path.startswith("2026-10/")
    assert (await store.get_source(result.source.id)).projection_path.startswith(
        "_Sources/2026-10/"
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
        "_Sources/2026-09/"
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
    assert hits[0].path.startswith("_Sources/2026-09/")
    assert (await store.get_note(note.id)).path.startswith("2026-10/")
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
