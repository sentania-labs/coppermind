"""A clean note: what stops being written, what is named for its title, and what an upgrade
leaves alone, removes, or never touches on a vault that already holds the older files."""

import base64
import json

import pytest
import sqlalchemy as sa
from coppermind_store import filing
from coppermind_store.admin_features import Features
from coppermind_store.artifacts import TRANSCRIPT_DATA, read_manifest, sweep
from coppermind_store.entities import dossier_marker
from coppermind_store.reconciler import reconcile_once

from coppermind import frontmatter as fm
from coppermind.context_config import FeatureAssignment
from coppermind.db.models import Artifact, Note, SearchDocument
from coppermind.entities import EntityChange, EntityInput, EntityQuery
from coppermind.store_protocol import (
    CreateArtifact,
    CreateNote,
    IngestArtifact,
    PatchFrontmatter,
    ReplaceNote,
)
from tests.integration.test_review_filing import approve
from tests.integration.test_search_index import ingest_request

# --- the note ----------------------------------------------------------------------------


async def test_a_new_note_carries_none_of_the_properties_nothing_writes(store):
    note = await store.create_note(
        CreateNote(title="Planning", body="Human prose.", frontmatter={"type": "work"})
    )
    assert not {"kind", "context", "time_unknown", "invitees"} & set(note.frontmatter)
    on_disk = fm.parse((store.notes_root / note.path).read_text())[0]
    assert not {"kind", "context", "time_unknown", "invitees"} & set(on_disk)
    assert {"id", "date", "type", "reviewed", "tags", "sources", "schema_version"} <= set(on_disk)


async def test_calendar_matching_sets_up_a_meeting_property_and_no_invitees(store):
    await store.context.setup_lifecycle({"revision": 0})
    names = {key.name for key in store.control.schema().keys}
    assert {"attendees", "meeting"} <= names
    assert not {"invitees", "calendar_event", "time_unknown"} & names
    config = store.context.config().assignments["enrichment"]
    assert (config.attendees_field, config.event_field, config.invitees_field) == (
        "attendees",
        "meeting",
        "",
    )


async def test_an_install_that_mapped_the_old_names_moves_to_meeting_and_stops_invitees(store):
    """Its mapping moves to `meeting`; notes still saying `calendar_event` are read as they are."""
    config = store.context.config()
    config.assignments["enrichment"].event_field = "calendar_event"
    store.context.save_config(config)
    assignment = store.context.config().assignments["enrichment"]
    assert assignment.event_field == "meeting"
    Features(store.context).ensure_owned_properties()
    names = {key.name for key in store.control.schema().keys}
    assert "meeting" in names
    assert assignment.event_key({"calendar_event": "evt-1"}) == "calendar_event"
    assert assignment.event_key({"calendar_event": "evt-1", "meeting": "evt-2"}) == "meeting"
    assert assignment.event_key({}) == "meeting"
    assert FeatureAssignment.model_validate({"invitees_field": "invitees"}).invitees_field == ""
    assert FeatureAssignment.model_validate({"invitees_field": "tags"}).invitees_field == "tags"
    assert FeatureAssignment.model_validate({"event_field": "account"}).event_field == "account"


async def test_a_note_with_the_old_properties_files_and_keeps_every_one_of_them(store):
    """Old notes are read as they are; filing does not rewrite their properties."""
    old = {
        "type": "work",
        "date": "2026-10-16",
        "context": "internal",
        "kind": "note",
        "time_unknown": True,
        "invitees": ["pat@example.com"],
        "calendar_event": "evt-1",
        "reviewed": True,
    }
    note = await store.create_note(CreateNote(title="Old", body="Prose.", frontmatter=old))
    await filing.file_reviewed(store)
    filed = await store.get_note(note.id)
    assert filed.path.startswith("work/2026/2026-10/")
    for key, value in old.items():
        assert filed.frontmatter[key] == value


# --- the transcript page -------------------------------------------------------------------


async def test_a_filed_transcript_page_is_named_for_the_note_title_and_the_link_follows(store):
    result = await store.ingest(ingest_request())
    pending = result.projection_path
    assert pending.startswith("_Sources/_Pending/2026/2026-09/")
    await approve(store, result.note.id)
    note = await store.get_note(result.note.id)
    source = await store.get_source(result.source.id)
    assert source.projection_path == "_Sources/2026/2026-09/Warehouse call transcript.md"
    assert (store.notes_root / source.projection_path).is_file()
    assert not (store.notes_root / pending).exists()
    assert "[[_Sources/2026/2026-09/Warehouse call transcript]]" in note.body
    assert result.source.id not in source.projection_path
    # Filing again changes nothing: the page keeps the name it was filed under.
    assert await filing.file_reviewed(store) == {"filed": 0, "filing_failed": 0}
    assert (await store.get_source(result.source.id)).projection_path == source.projection_path


async def test_a_date_the_title_carries_is_not_part_of_the_page_name(store):
    request = ingest_request()
    request.note.title = "2026-09-08 Warehouse call"
    result = await store.ingest(request)
    await approve(store, result.note.id)
    path = (await store.get_source(result.source.id)).projection_path
    assert path == "_Sources/2026/2026-09/Warehouse call transcript.md"


async def test_two_transcripts_with_one_title_do_not_share_a_name(store):
    first = await store.ingest(ingest_request("rec-1"))
    second = await store.ingest(ingest_request("rec-2"))
    await approve(store, first.note.id)
    await approve(store, second.note.id)
    paths = {
        (await store.get_source(first.source.id)).projection_path,
        (await store.get_source(second.source.id)).projection_path,
    }
    assert paths == {
        "_Sources/2026/2026-09/Warehouse call transcript.md",
        "_Sources/2026/2026-09/Warehouse call transcript (2).md",
    }


async def test_a_source_that_is_not_a_transcript_is_named_source(store):
    request = ingest_request()
    request.source.artifacts[0].name = "notes.txt"
    result = await store.ingest(request)
    await approve(store, result.note.id)
    path = (await store.get_source(result.source.id)).projection_path
    assert path == "_Sources/2026/2026-09/Warehouse call source.md"


async def test_a_page_filed_under_an_id_before_is_not_renamed_and_its_links_still_work(store):
    """An already-filed vault: the page keeps its opaque name, and so does the link to it."""
    result = await store.ingest(ingest_request())
    await approve(store, result.note.id)
    source_id = result.source.id
    manifest_path = store.sources_root / source_id / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    filed = manifest["filed_projection_path"]
    old = f"_Sources/2026/2026-09/{source_id}.md"
    (store.notes_root / filed).rename(store.notes_root / old)
    manifest["projection_path"] = manifest["filed_projection_path"] = old
    manifest_path.write_text(json.dumps(manifest))
    note = await store.get_note(result.note.id)
    body = note.body.replace(filed.removesuffix(".md"), old.removesuffix(".md"))
    note = await store.replace_note(
        note.id, ReplaceNote(frontmatter=note.frontmatter, body=body), note.content_hash
    )
    # A later pass over the already filed note leaves page and link exactly where they are.
    await filing.file_reviewed(store)
    assert (await store.get_source(source_id)).projection_path == old
    assert (store.notes_root / old).is_file()
    assert f"[[{old.removesuffix('.md')}]]" in (await store.get_note(note.id)).body


# --- transcript.json -----------------------------------------------------------------------


def with_transcript_json(request):
    request.source.artifacts.append(
        IngestArtifact(
            name=TRANSCRIPT_DATA,
            mime_type="application/json",
            content_base64=base64.b64encode(b'{"segments": []}').decode(),
        )
    )
    return request


async def filed_artifacts(store):
    async with store.session_factory() as session:
        return (await session.execute(sa.select(Artifact.source_name, Artifact.path))).all()


async def test_transcript_json_is_not_left_in_artifacts_but_other_files_still_are(store):
    request = with_transcript_json(ingest_request())
    request.source.artifacts.append(
        IngestArtifact(
            name="slides.pdf",
            mime_type="application/pdf",
            content_base64=base64.b64encode(b"synthetic bytes").decode(),
        )
    )
    result = await store.ingest(request)
    await approve(store, result.note.id)
    filed = await filed_artifacts(store)
    assert [name for name, _ in filed] == ["slides.pdf"]
    # The timings are still in the source's own archive.
    assert (store.sources_root / result.source.id / "r0001" / TRANSCRIPT_DATA).is_file()
    assert not list((store.notes_root / "Artifacts").rglob(TRANSCRIPT_DATA))


async def file_the_old_way(store, monkeypatch):
    """Reproduce an earlier release: transcript.json went into Artifacts with the PDF."""
    from coppermind_store import artifacts

    monkeypatch.setattr(artifacts, "TRANSCRIPT_DATA", "never-matches.json")
    result = await store.ingest(with_transcript_json(ingest_request()))
    await approve(store, result.note.id)
    monkeypatch.setattr(artifacts, "TRANSCRIPT_DATA", TRANSCRIPT_DATA)
    return result


async def test_a_transcript_json_an_earlier_release_filed_is_left_where_it_is(store, monkeypatch):
    result = await file_the_old_way(store, monkeypatch)
    ((name, path),) = await filed_artifacts(store)
    assert name == TRANSCRIPT_DATA
    artifact_id = await _only_artifact_id(store)
    body_before = (await store.get_note(result.note.id)).body

    counts = await sweep(store)

    assert counts["artifacts_removed"] == 0
    assert await filed_artifacts(store) == [(name, path)]
    assert (store.notes_root / path).is_file()
    assert read_manifest(store, artifact_id)["artifact_id"] == artifact_id
    note = await store.get_note(result.note.id)
    assert artifact_id in note.frontmatter["artifacts"]
    assert note.body == body_before
    assert (store.sources_root / result.source.id / "r0001" / TRANSCRIPT_DATA).is_file()


async def _only_artifact_id(store) -> str:
    async with store.session_factory() as session:
        return str(await session.scalar(sa.select(Artifact.id)))


async def test_an_artifact_the_owner_stored_under_that_name_is_left_alone(store):
    created = await store.create_artifact(
        CreateArtifact(
            kind="artifact",
            title="transcript",
            origin="manual",
            filename=TRANSCRIPT_DATA,
            mime_type="application/json",
            content_base64=base64.b64encode(b"{}").decode(),
        )
    )
    await sweep(store)
    assert [name for name, _ in await filed_artifacts(store)] == [None]
    assert created.id == await _only_artifact_id(store)


# --- People and Companies ------------------------------------------------------------------

OLD_FACTS = (
    "\n## Coppermind's Notes\n\n### Facts\n\n- role: engineer <!-- fact:F1 -->\n"
    "  - Evidence: [[work/2026/2026-11/Meeting]] 2026-11-01\n\n"
    "### Supporting notes\n\n- [[work/2026/2026-11/Meeting]]\n\n"
    "Edit a fact above to correct it; delete its line to withdraw it.\n"
)


def old_page(entity, extra_front="", owner_text="", block=OLD_FACTS):
    front = f"---\naliases: []\nname: {entity.name}\n{extra_front}---\n"
    return f"{front}# {entity.name}\n\n## Scott's Notes\n\n{owner_text}" + (
        dossier_marker(entity.id) + block
    )


async def pretend_it_was_written_by_an_earlier_release(store, entity_id, text, block=OLD_FACTS):
    ledger = store.entities.load()
    entity = ledger.entities[entity_id]
    path = store.notes_root / entity.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    entity.generated = block
    entity.pending = None
    entity.projection_fields = {"name": entity.name, "aliases": []}
    store.entities.save(ledger)
    return path


async def test_a_new_person_page_is_plain(store):
    created = await store.entities.create(
        EntityInput(kind="person", name="Casey", emails=["casey@example.test"])
    )
    await store.create_note(
        CreateNote(
            title="Meeting",
            body="Casey is an engineer.",
            frontmatter={
                "reviewed": True,
                "date": "2026-11-01",
                "attendees": ["casey@example.test"],
                "account": ["Acme"],
            },
        )
    )
    await store.entities.refresh()
    page = (store.notes_root / created["path"]).read_text()
    fields, body = fm.parse(page)
    assert set(fields) == {"aliases", "name", "email", "company"}
    assert fields["company"] == []  # a company is a name here, never a link to a page
    assert body == f"# Casey\n\n## Scott's Notes\n\n{dossier_marker(created['id'])}"
    for text in ("Coppermind's Notes", "Facts", "Supporting notes", "Evidence", "[["):
        assert text not in page
    # What was learned is still in the ledger, and still found by search.
    entity = store.entities.load().entities[created["id"]]
    assert entity.facts
    async with store.session_factory() as session:
        assert await session.scalar(
            sa.select(SearchDocument.ref_id).where(SearchDocument.ref_id == entity.id)
        )


async def test_a_company_gets_no_page_and_is_not_searched_as_one(store):
    await store.create_note(
        CreateNote(
            title="Customer meeting",
            body="A meeting.",
            frontmatter={"reviewed": True, "date": "2026-11-01", "account": ["Xcel Energy"]},
        )
    )
    await store.entities.refresh()
    company = next(e for e in store.entities.load().entities.values() if e.kind == "company")
    assert company.name == "Xcel Energy"
    assert not (store.notes_root / "Companies").exists()
    assert not (store.notes_root / company.path).exists()
    assert not company.retired_at and company.problem is None
    async with store.session_factory() as session:
        assert not await session.scalar(
            sa.select(SearchDocument.ref_id).where(SearchDocument.ref_id == company.id)
        )


async def test_an_old_person_page_becomes_plain_and_keeps_what_the_owner_wrote(store):
    created = await store.entities.create(
        EntityInput(kind="person", name="Casey", emails=["casey@example.test"])
    )
    entity = store.entities.load().entities[created["id"]]
    path = await pretend_it_was_written_by_an_earlier_release(
        store,
        entity.id,
        old_page(
            entity,
            extra_front="email:\n  - casey@example.test\ncompany:\n  - '[[Companies/Acme]]'\n"
            "nickname: KC\n",
            owner_text="Met at the offsite.\n\n",
        ),
    )
    ledger = store.entities.load()
    ledger.entities[entity.id].projection_fields = {
        "name": "Casey",
        "aliases": [],
        "email": ["casey@example.test"],
        "company": ["[[Companies/Acme]]"],
    }
    store.entities.save(ledger)

    await store.entities.refresh()

    fields, body = fm.parse(path.read_text())
    # The link Coppermind wrote pointed at a page that is gone; it is now the company's name.
    assert fields["company"] == []
    assert fields["nickname"] == "KC"  # their own property stays
    assert "Met at the offsite." in body
    for text in ("Coppermind's Notes", "Facts", "Supporting notes", "role: engineer"):
        assert text not in path.read_text()
    assert dossier_marker(entity.id) in path.read_text()


async def test_a_company_property_the_owner_changed_is_not_removed(store):
    created = await store.entities.create(
        EntityInput(kind="person", name="Casey", emails=["casey@example.test"])
    )
    entity = store.entities.load().entities[created["id"]]
    path = await pretend_it_was_written_by_an_earlier_release(
        store, entity.id, old_page(entity, extra_front="company:\n  - '[[My own company]]'\n")
    )
    ledger = store.entities.load()
    ledger.entities[entity.id].projection_fields = {
        "name": "Casey",
        "aliases": [],
        "company": ["[[Companies/Acme]]"],
    }
    store.entities.save(ledger)
    await store.entities.refresh()
    assert fm.parse(path.read_text())[0]["company"] == ["[[My own company]]"]


async def make_company(store):
    created = await store.entities.create(EntityInput(kind="company", name="Acme"))
    return created["id"]


async def test_a_company_page_coppermind_wrote_is_removed_and_the_company_stays(store):
    company_id = await make_company(store)
    path = await pretend_it_was_written_by_an_earlier_release(
        store,
        company_id,
        "---\naliases: []\nname: Acme\ndomains:\n  - acme.com\n---\n# Acme\n\n## Scott's Notes\n\n"
        + dossier_marker(company_id)
        + "\n## Coppermind's Notes\n\n### Facts\n\n- domain: acme.com <!-- fact:F2 -->\n",
        block="\n## Coppermind's Notes\n\n### Facts\n\n- domain: acme.com <!-- fact:F2 -->\n",
    )
    ledger = store.entities.load()
    ledger.entities[company_id].projection_fields = {
        "name": "Acme",
        "aliases": [],
        "domains": ["acme.com"],
    }
    store.entities.save(ledger)

    await store.entities.refresh()

    assert not path.exists()
    entity = store.entities.load().entities[company_id]
    assert entity.retired_at is None and entity.generated is None
    # Gone from the vault, not from Coppermind: the company is still an account to match.
    assert (await store.entities.list(EntityQuery(kind="company")))["items"]
    await store.entities.refresh()
    assert not path.exists()


async def test_a_company_page_the_owner_wrote_in_is_left_exactly_as_it_is(store):
    company_id = await make_company(store)
    block = "\n## Coppermind's Notes\n\n### Facts\n\n- domain: acme.com <!-- fact:F2 -->\n"
    text = (
        "---\naliases: []\nname: Acme\ndomains:\n  - acme.com\n---\n# Acme\n\n"
        "## Scott's Notes\n\nOur biggest account. Renewal is in March.\n\n"
        + dossier_marker(company_id)
        + block
    )
    path = await pretend_it_was_written_by_an_earlier_release(store, company_id, text, block)
    ledger = store.entities.load()
    ledger.entities[company_id].projection_fields = {
        "name": "Acme",
        "aliases": [],
        "domains": ["acme.com"],
    }
    store.entities.save(ledger)

    await store.entities.refresh()
    assert path.read_text() == text

    # Coppermind no longer tends it, so deleting it later does not retire the company.
    path.unlink()
    await store.entities.refresh()
    assert store.entities.load().entities[company_id].retired_at is None


async def test_a_company_page_the_owner_edited_a_fact_in_is_left_alone(store):
    company_id = await make_company(store)
    block = "\n## Coppermind's Notes\n\n### Facts\n\n- domain: acme.com <!-- fact:F2 -->\n"
    edited = block.replace("acme.com", "acme.io")
    text = (
        "---\naliases: []\nname: Acme\n---\n# Acme\n\n## Scott's Notes\n\n"
        + dossier_marker(company_id)
        + edited
    )
    path = await pretend_it_was_written_by_an_earlier_release(store, company_id, text, block)
    await store.entities.refresh()
    assert path.read_text() == text


# --- the sync test note --------------------------------------------------------------------


async def test_an_old_sync_test_note_is_never_adopted_as_a_note(store):
    (store.notes_root / "Coppermind sync test.md").write_text(
        "# Coppermind sync test\n\nCoppermind sent this note so you can see sync working.\n"
    )
    await reconcile_once(store)
    assert (store.notes_root / "Coppermind sync test.md").is_file()
    async with store.session_factory() as session:
        assert not await session.scalar(
            sa.select(Note.id).where(Note.path == "Coppermind sync test.md")
        )
    assert "Coppermind sync test.md" in store.control.read_reconciler_exclusions()


async def test_an_old_company_link_on_a_persons_page_becomes_the_companys_name(store):
    await store.create_note(
        CreateNote(
            title="Customer meeting",
            body="A meeting.",
            frontmatter={
                "reviewed": True,
                "date": "2026-11-01",
                "account": ["Acme"],
                "attendees": ["pat@acme.com"],
            },
        )
    )
    await store.entities.refresh()
    ledger = store.entities.load()
    pat = next(e for e in ledger.entities.values() if e.kind == "person")
    page = store.notes_root / pat.path
    assert fm.parse(page.read_text())[0]["company"] == ["Acme"]
    # What an earlier release wrote: a link to the company's page, which is not there now.
    page.write_text(page.read_text().replace("- Acme", "- '[[Companies/Acme]]'"))
    pat.projection_fields = {**(pat.projection_fields or {}), "company": ["[[Companies/Acme]]"]}
    store.entities.save(ledger)

    await store.entities.refresh()

    assert fm.parse(page.read_text())[0]["company"] == ["Acme"]
    assert not (store.notes_root / "Companies").exists()


# --- what the owner wrote is never replaced -------------------------------------------------

OWNER_TAIL = "Typed after the marker.\n"


async def plain_page_with_owner_text(store, name="Casey", address="casey@example.test"):
    created = await store.entities.create(EntityInput(kind="person", name=name, emails=[address]))
    path = store.notes_root / created["path"]
    text = path.read_text()
    marker = dossier_marker(created["id"])
    above = text.replace(marker, "Above the marker.\n\n" + marker)
    path.write_text(above + OWNER_TAIL)
    await store.entities.refresh()
    assert path.read_text() == above + OWNER_TAIL
    return created, path, marker


async def test_retiring_a_person_keeps_everything_the_owner_wrote_on_the_page(store):
    created, path, marker = await plain_page_with_owner_text(store)
    await store.entities.change(
        created["id"], EntityChange(version=created["version"], action="retire")
    )
    await store.entities.refresh()
    retired = path.read_text()
    assert "Above the marker." in retired and OWNER_TAIL in retired
    assert "Retired dossier" in retired  # the message is added in front of their writing
    assert retired.index("Retired dossier") < retired.index(OWNER_TAIL)
    await store.entities.refresh()
    assert path.read_text() == retired

    current = await store.entities.get(created["id"])
    await store.entities.change(
        created["id"], EntityChange(version=current["version"], action="restore")
    )
    await store.entities.refresh()
    restored = path.read_text()
    assert "Retired dossier" not in restored
    assert "Above the marker." in restored and restored.endswith(marker + OWNER_TAIL)


async def test_merging_a_person_away_keeps_everything_the_owner_wrote_on_the_page(store):
    keep = await store.entities.create(EntityInput(kind="person", name="Keep"))
    created, path, marker = await plain_page_with_owner_text(store)
    request = {"version": created["version"], "target": keep["id"]}
    preview = await store.entities.change(
        created["id"], EntityChange(action="merge-preview", **request)
    )
    await store.entities.change(
        created["id"], EntityChange(action="merge", preview=preview["preview"], **request)
    )
    await store.entities.refresh()
    merged = path.read_text()
    assert "Above the marker." in merged and OWNER_TAIL in merged
    assert "See [[" in merged and merged.index("See [[") < merged.index(OWNER_TAIL)
    await store.entities.refresh()
    assert path.read_text() == merged


async def test_text_after_the_marker_of_an_older_facts_page_survives_the_upgrade(store):
    created = await store.entities.create(
        EntityInput(kind="person", name="Casey", emails=["casey@example.test"])
    )
    entity = store.entities.load().entities[created["id"]]
    tail = "Added below the old facts.\n"
    path = await pretend_it_was_written_by_an_earlier_release(
        store, entity.id, old_page(entity, extra_front="email:\n  - casey@example.test\n") + tail
    )
    await store.entities.refresh()
    assert path.read_text().endswith(dossier_marker(entity.id) + tail)
    assert "Facts" not in path.read_text()


OWNER_ADDED = [
    pytest.param("email:\n  - sales@acme.com\n", id="email"),
    pytest.param("company:\n  - Parent Co\n", id="company"),
    pytest.param("nickname: Big A\n", id="own-property"),
    pytest.param("domains:\n  - acme.com\n", id="domains-changed"),
]


@pytest.mark.parametrize("added", OWNER_ADDED)
async def test_a_company_page_with_a_property_the_owner_added_is_never_removed(store, added):
    """`email` and `company` are generated names, and are still the owner's when they added them."""
    company_id = await make_company(store)
    block = "\n## Coppermind's Notes\n\n### Facts\n\n- domain: acme.com <!-- fact:F2 -->\n"
    projected = {"name": "Acme", "aliases": []}
    if added.startswith("domains"):
        projected = {**projected, "domains": ["acme.io"]}  # the page now says something else
    front = "name: Acme\naliases: []\n" + added
    text = f"---\n{front}---\n# Acme\n\n## Scott's Notes\n\n" + dossier_marker(company_id) + block
    path = await pretend_it_was_written_by_an_earlier_release(store, company_id, text, block)
    ledger = store.entities.load()
    ledger.entities[company_id].projection_fields = projected
    store.entities.save(ledger)

    await store.entities.refresh()

    assert path.read_text() == text
    assert store.entities.load().entities[company_id].retired_at is None


async def test_a_company_page_with_a_line_the_owner_added_is_never_removed(store):
    company_id = await make_company(store)
    block = "\n## Coppermind's Notes\n\n### Facts\n\n- domain: acme.com <!-- fact:F2 -->\n"
    text = (
        "---\nname: Acme\naliases: []\n---\n# Acme\n\n## Scott's Notes\n\nA line of mine.\n"
        + dossier_marker(company_id)
        + block
    )
    path = await pretend_it_was_written_by_an_earlier_release(store, company_id, text, block)
    await store.entities.refresh()
    assert path.read_text() == text


# --- one meeting property per note --------------------------------------------------------


async def test_a_note_with_the_old_meeting_property_keeps_one_property_and_a_removed_meeting_stays_removed(  # noqa: E501
    store, monkeypatch
):
    from coppermind_store.association_corrections import correct
    from coppermind_store.context_service import fingerprint

    from tests.integration.test_recording_merge import prepare

    context, recording, generated, target, payload = await prepare(store)
    assert context.config().assignments["enrichment"].event_field == "meeting"
    note = await store.get_note(generated.id)
    old = note.frontmatter.get("calendar_event")
    if not old:  # a note an earlier release wrote: only the old property
        note = await store.patch_frontmatter(
            note.id,
            PatchFrontmatter(set={"calendar_event": "E1"}, unset=["meeting"]),
            note.content_hash,
        )
        old = "E1"
    assert "meeting" not in note.frontmatter
    selected = {"event_id": "E2", "title": "Another meeting", "start": "2026-10-14T09:00:00"}

    async def candidates_for_note(_):
        return [selected]

    monkeypatch.setattr(context, "recording_calendar_candidates", candidates_for_note)
    context.write("recording-meetings", {recording.source_id: {"event_id": old, "title": "x"}})
    await context.lifecycle.choose_recording_meeting(
        {
            "note_id": note.id,
            "if_match": note.content_hash,
            "calendar_hash": await context.calendar_evidence_hash(note),
            "event_id": "E2",
        }
    )
    chosen = (await store.get_note(note.id)).frontmatter
    assert chosen["calendar_event"] == "E2" and "meeting" not in chosen

    note = await store.get_note(note.id)
    records = context.read("associations", {})
    await correct(
        context,
        {
            "confirmation": "Remove mistaken association",
            "note_id": note.id,
            "note_hash": note.content_hash,
            "associations_hash": fingerprint(records[note.id]),
            "kind": "events",
            "target": "E2",
        },
    )
    after = (await store.get_note(note.id)).frontmatter
    assert "meeting" not in after and "calendar_event" not in after  # it does not come back
