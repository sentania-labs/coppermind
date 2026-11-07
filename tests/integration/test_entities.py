"""Dossier behavior against real files and PostgreSQL, including recovery and purge."""

import pytest
import sqlalchemy as sa
from coppermind_store.entities import DELIMITER, EntityService
from coppermind_store.reconciler import reconcile_once

from coppermind.db.models import EntityRecord, SearchDocument
from coppermind.entities import EntityChange, EntityInput, EntityQuery
from coppermind.store_protocol import CreateNote, PatchFrontmatter, SearchQuery, VersionConflict
from tests.integration.test_purge import confirmation, retention


async def person(store, name="Casey", address="casey@example.test"):
    return await store.entities.create(EntityInput(kind="person", name=name, emails=[address]))


async def reviewed_note(store, body="Casey is an engineer."):
    return await store.create_note(
        CreateNote(
            title="Meeting",
            body=body,
            frontmatter={
                "reviewed": True,
                "date": "2026-11-01",
                "attendees": ["casey@example.test"],
            },
        )
    )


async def claim(store, entity, note, **kwargs):
    current = await store.entities.get(entity["id"])
    return await store.entities.change(
        entity["id"],
        EntityChange(
            version=current["version"],
            action="claim",
            predicate="role",
            value="Engineer",
            note_id=note.id,
            quote="Casey is an engineer.",
            **kwargs,
        ),
    )


async def test_identity_claims_correction_projection_and_recovery(store):
    a = await person(store)
    b = await person(store, address="other@example.test")
    company = await store.entities.create(EntityInput(kind="company", name="Example"))
    assert a["id"] != b["id"] and a["path"] != b["path"]
    note = await reviewed_note(store)
    a = await claim(store, a, note, target=company["id"])
    path = store.notes_root / a["path"]
    original = path.read_text()
    prefix, block = original.split(DELIMITER)
    prefix += "Personal writing stays byte for byte.\n\n"
    path.write_text(prefix + DELIMITER + block.replace("role: Engineer", "role: Architect"))
    updated = await store.entities.get(a["id"])
    assert not updated["problem"]
    corrections = [f for f in updated["facts"] if f["origin"] == "correction"]
    assert corrections[-1]["value"] == "Architect"
    assert corrections[-1]["evidence"]["path"] == a["path"]
    assert path.read_text().split(DELIMITER)[0] == prefix
    assert not any(f["state"] == "confirmed" and f["value"] == "Engineer" for f in updated["facts"])
    # SQL is disposable. Rebuilding uses durable corrections without a model.
    async with store.session_factory() as session, session.begin():
        await session.execute(sa.delete(EntityRecord))
        await session.execute(sa.delete(SearchDocument))
    store.entities = EntityService(store)
    assert (await store.entities.get(a["id"]))["facts"] == updated["facts"]
    assert path.read_text().split(DELIMITER)[0] == prefix
    with pytest.raises(VersionConflict):
        await store.entities.change(a["id"], EntityChange(version=1, action="rename", name="New"))
    current = await store.entities.get(a["id"])
    renamed = await store.entities.change(
        a["id"], EntityChange(version=current["version"], action="rename", name="New")
    )
    assert renamed["path"] == a["path"]


async def test_evidence_withdraws_on_unreview_edit_or_delete(store):
    p = await person(store)
    note = await reviewed_note(store)
    p = await claim(store, p, note)
    await store.patch_frontmatter(
        note.id, PatchFrontmatter(set={"reviewed": False}), note.content_hash
    )
    withdrawn = await store.entities.get(p["id"])
    assert all(f["state"] == "withdrawn" for f in withdrawn["facts"])
    assert "Engineer" not in (store.notes_root / p["path"]).read_text()
    assert not (await store.search(SearchQuery(q="Engineer"), entities=True)).items


async def test_retired_subject_purges_without_rewriting_git_or_regeneration(store):
    retention(store, 0)
    p = await person(store)
    note = await reviewed_note(store)
    p = await claim(store, p, note)
    path = store.notes_root / p["path"]
    path.unlink()
    retired = await store.entities.get(p["id"])
    assert retired["retired_at"]
    assert not (await store.entities.list(EntityQuery())).get("items")
    preview = await store.preview_purge()
    assert [c.kind for c in preview.candidates] == ["entity"]
    assert preview.counts.entities == 1
    assert (await store.confirm_purge(confirmation(preview))).state == "complete"
    assert p["id"] not in store.entities.load().entities
    await store.entities.refresh()
    assert not path.exists()
    assert not (await store.entities.list(EntityQuery()))["items"]
    assert (await store.get_note(note.id)).body == note.body


async def test_projection_race_occupied_path_and_reserved_folders(store, monkeypatch):
    p = await person(store)
    path = store.notes_root / p["path"]
    path.write_text("An unrelated file, never overwrite me.")
    item = await store.entities.get(p["id"])
    assert item["problem"] and path.read_text() == "An unrelated file, never overwrite me."
    unknown = store.notes_root / "People" / "Handwritten.md"
    unknown.write_text("# A personal document\n")
    await reconcile_once(store)
    assert unknown.read_text() == "# A personal document\n"


async def test_query_paging_relationship_search_and_identity_preview(store):
    p = await person(store)
    await person(store, name="Casey", address="second@example.test")
    note = await reviewed_note(store)
    p = await claim(store, p, note)
    page = await store.entities.list(EntityQuery(limit=1, q="Casey"))
    second = await store.entities.list(EntityQuery(limit=1, q="Casey", cursor=page["next_cursor"]))
    assert page["items"][0]["id"] != second["items"][0]["id"]
    assert not (await store.search(SearchQuery(q="Engineer"))).items
    hits = await store.search(SearchQuery(q="Engineer"), entities=True)
    assert hits.items[0].kind == "entity" and hits.items[0].id == p["id"]
    fact = next(f for f in p["facts"] if f["predicate"] == "role")
    request = dict(version=p["version"], name="Split Casey", fact_ids=[fact["id"]])
    preview = await store.entities.change(p["id"], EntityChange(action="split-preview", **request))
    split = await store.entities.change(
        p["id"], EntityChange(action="split", preview=preview["preview"], **request)
    )
    assert split["id"] != p["id"] and split["facts"][0]["id"] == fact["id"]
    current = await store.entities.get(p["id"])
    request = dict(version=split["version"], target=current["id"])
    preview = await store.entities.change(
        split["id"], EntityChange(action="merge-preview", **request)
    )
    merged = await store.entities.change(
        split["id"], EntityChange(action="merge", preview=preview["preview"], **request)
    )
    assert merged["id"] == current["id"]
    assert (await store.entities.get(split["id"]))["redirect"] == current["id"]


async def test_note_purge_scrubs_quotes_and_orphan_dossier(store):
    from tests.integration.test_note_recovery import commit, git

    retention(store, 0)
    note = await reviewed_note(store)
    await store.entities.refresh()
    p = (await store.entities.list(EntityQuery()))["items"][0]
    assert p["automatic"]
    await claim(store, p, note)
    git(store.notes_root, "init", "-q")
    commit(store.notes_root)
    (store.notes_root / note.path).unlink()
    commit(store.notes_root)
    await reconcile_once(store)
    preview = await store.preview_purge()
    assert {c.kind for c in preview.candidates} == {"note", "entity"}
    before = git(store.notes_root, "rev-parse", "HEAD")
    assert (await store.confirm_purge(confirmation(preview))).state == "complete"
    assert "Casey is an engineer" not in (store.entities.root / "catalog.json").read_text()
    assert not (store.notes_root / p["path"]).exists()
    assert git(store.notes_root, "rev-parse", "HEAD") == before


async def test_projection_receipt_survives_interrupted_write(store, monkeypatch):
    import coppermind_store.entities as entities

    p = await person(store)
    note = await reviewed_note(store)
    p = await claim(store, p, note)
    path = store.notes_root / p["path"]
    path.write_text(path.read_text().replace("role: Engineer", "role: Architect"))
    original = entities.commit_staged

    def interrupt(temp, destination):
        original(temp, destination)
        raise OSError("simulated crash after atomic rename")

    monkeypatch.setattr(entities, "commit_staged", interrupt)
    await store.entities.get(p["id"])
    monkeypatch.setattr(entities, "commit_staged", original)
    store.entities = EntityService(store)
    restored = await store.entities.get(p["id"])
    corrections = [f for f in restored["facts"] if f["origin"] == "correction"]
    assert len(corrections) == 1 and corrections[0]["value"] == "Architect"
    assert not restored["problem"]


async def test_projection_racing_device_edit_is_imported_next_pass(store, monkeypatch):
    import coppermind_store.entities as entities

    p = await person(store)
    note = await reviewed_note(store)
    p = await claim(store, p, note)
    path = store.notes_root / p["path"]
    original = entities.stage_bytes
    fired = False

    def device_write(destination, data):
        nonlocal fired
        staged = original(destination, data)
        if not fired:
            fired = True
            destination.write_text(
                destination.read_text().replace("role: Engineer", "role: Captain")
            )
        return staged

    monkeypatch.setattr(entities, "stage_bytes", device_write)
    await store.entities.change(
        p["id"], EntityChange(version=p["version"], action="rename", name="Casey New")
    )
    assert "role: Captain" in path.read_text()
    restored = await store.entities.get(p["id"])
    assert any(f["value"] == "Captain" and f["state"] == "confirmed" for f in restored["facts"])


async def test_claim_evidence_is_current_and_reserved_folder_writes_refused(store):
    from coppermind.store_protocol import MoveNote, ValidationFailed

    p = await person(store)
    note = await reviewed_note(store)
    with pytest.raises(ValidationFailed):
        await store.entities.change(
            p["id"],
            EntityChange(
                version=(await store.entities.get(p["id"]))["version"],
                action="claim",
                note_id=note.id,
                quote="Invented quotation",
                value="Invented",
            ),
        )
    for folder in ("People", "Companies/Subfolder", "people"):
        with pytest.raises(ValidationFailed):
            await store.create_note(CreateNote(title="Wrong place", folder=folder))
        with pytest.raises(ValidationFailed):
            await store.move_note(note.id, MoveNote(target_folder=folder), note.content_hash)


async def test_purge_interruption_never_recreates_a_removed_page(store, monkeypatch):
    from coppermind.store_protocol import NotesFilesystemUnavailable, ValidationFailed

    retention(store, 0)
    p = await person(store)
    p = await store.entities.change(p["id"], EntityChange(version=p["version"], action="retire"))
    preview = await store.preview_purge()
    original = store.entities.purge

    def interrupted(*args):
        raise OSError("simulated crash after page removal")

    monkeypatch.setattr(store.entities, "purge", interrupted)
    with pytest.raises(NotesFilesystemUnavailable):
        await store.confirm_purge(confirmation(preview))
    assert not (store.notes_root / p["path"]).exists()
    await store.entities.refresh()
    assert not (store.notes_root / p["path"]).exists()
    with pytest.raises(ValidationFailed, match="interrupted retention purge"):
        await store.entities.get(p["id"])
    monkeypatch.setattr(store.entities, "purge", original)
    assert (await store.confirm_purge(confirmation(preview))).state == "complete"
    assert p["id"] not in store.entities.load().entities


async def test_orphan_purge_preserves_personal_writing_arriving_before_preview(store):
    from tests.integration.test_note_recovery import commit, git

    retention(store, 0)
    note = await reviewed_note(store)
    await store.entities.refresh()
    p = (await store.entities.list(EntityQuery()))["items"][0]
    git(store.notes_root, "init", "-q")
    commit(store.notes_root)
    (store.notes_root / note.path).unlink()
    commit(store.notes_root)
    await reconcile_once(store)
    path = store.notes_root / p["path"]
    path.write_text(path.read_text().replace(DELIMITER, "Personal reminder.\n\n" + DELIMITER))
    preview = await store.preview_purge()
    assert [c.kind for c in preview.candidates] == ["note"]
    await store.confirm_purge(confirmation(preview))
    assert "Personal reminder" in path.read_text()
    assert "Personal reminder" in (await store.entities.get(p["id"]))["personal_text"]
