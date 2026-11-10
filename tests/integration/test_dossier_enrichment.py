"""Reviewed-note knowledge feeds blank metadata through the real store write protocol."""

import json

import httpx
import pytest
from coppermind_store.context_service import ContextService
from coppermind_store.filing import file_note
from coppermind_store.model_provider import ModelProvider
from coppermind_store.reconciler import reconcile_once

from coppermind.entities import EntityChange, EntityInput
from coppermind.store_protocol import CreateNote, IngestRequest, PatchFrontmatter
from tests.integration.test_context_enrichment import _fake_transcript, configure

BODY = "Casey Doe (casey@example.test) works at Acme Company as an engineer and enjoys sailing."


def extracted():
    return {
        "entities": [
            {
                "ref": "person",
                "kind": "person",
                "name": "Casey Doe",
                "emails": ["casey@example.test"],
                "quote": BODY,
            },
            {"ref": "company", "kind": "company", "name": "Acme Company", "quote": BODY},
        ],
        "claims": [
            {
                "subject": "person",
                "predicate": "organization",
                "value": "Acme Company",
                "target": "company",
                "quote": BODY,
            },
            {"subject": "person", "predicate": "role", "value": "engineer", "quote": BODY},
            {"subject": "person", "predicate": "interest", "value": "sailing", "quote": BODY},
            {
                "subject": "company",
                "predicate": "mentioned",
                "value": "Acme Company",
                "quote": BODY,
            },
        ],
    }


def provider(context, answer):
    seen = []

    def serve(request):
        payload = json.loads(request.content)
        evidence = json.loads(payload["messages"][-1]["content"])
        seen.append(evidence)
        output = answer(evidence) if callable(answer) else answer
        return httpx.Response(
            200,
            json={
                "model": "test-model-version",
                "usage": {"prompt_tokens": 10},
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(output)}}],
            },
        )

    context.provider = ModelProvider(httpx.MockTransport(serve))
    return seen


async def contribute(store):
    context = await configure(store)
    note = await store.create_note(
        CreateNote(title="Reviewed meeting", body=BODY, frontmatter={"reviewed": True})
    )
    provider(context, extracted())
    job = context.load_job(await context.queue_reviewed(note.id))
    await context.run_job(job)
    assert job.state == "applied", job.error
    entities = list(store.entities.load().entities.values())
    person = next(e for e in entities if e.kind == "person")
    company = next(e for e in entities if e.kind == "company")
    return context, note, job, person, company


async def test_reviewed_extraction_searches_transcript_and_validates_source_quote(store):
    context = await configure(store)
    quote = "Casey Doe (casey@example.test) is an engineer."
    transcript = "Unrelated context.\n" * 500 + quote + "\n" + "Other material.\n" * 500
    ingested = await store.ingest(
        IngestRequest.model_validate(
            {
                "source": {
                    "provider": "plaud",
                    "external_source_id": "searched-transcript",
                    "source_type": "transcript",
                    "captured_at": "2026-10-20T17:00:00Z",
                    "artifacts": [
                        {"name": "transcript.txt", "mime_type": "text/plain", "content": transcript}
                    ],
                },
                "note": {
                    "title": "Meeting",
                    "body": "Meeting summary.",
                    "frontmatter": {"reviewed": True, "attendees": ["casey@example.test"]},
                },
            }
        )
    )
    note = await store.get_note(ingested.note.id)
    await store.patch_frontmatter(
        note.id, PatchFrontmatter(set={"reviewed": True}), note.content_hash
    )
    seen = provider(
        context,
        {
            "entities": [
                {
                    "ref": "p",
                    "kind": "person",
                    "name": "Casey Doe",
                    "emails": ["casey@example.test"],
                    "quote": quote,
                }
            ],
            "claims": [
                {
                    "subject": "p",
                    "predicate": "role",
                    "value": "engineer",
                    "quote": quote,
                    "field": "transcript",
                }
            ],
        },
    )
    job = context.load_job(await context.queue_reviewed(ingested.note.id))
    await context.run_job(job)
    assert job.state == "applied", job.error
    assert quote in "\n".join(seen[0]["transcripts"])
    assert transcript not in seen[0]["transcripts"]
    assert job.evidence["transcript_omitted_characters"] > 0
    fact = next(
        f for e in store.entities.load().entities.values() for f in e.facts if f.predicate == "role"
    )
    assert fact.evidence.quote == quote
    assert fact.evidence.source_versions


@pytest.mark.parametrize("addresses", [("one@example.test", "two@example.test"), ("", "")])
async def test_distinct_same_name_subjects_keep_separate_dossiers(store, addresses):
    context = await configure(store)
    quotes = [
        f"Casey Doe ({address}) enjoys {interest}."
        for address, interest in zip(addresses, ("sailing", "hiking"), strict=True)
    ]
    note = await store.create_note(
        CreateNote(title="Two people", body="\n".join(quotes), frontmatter={"reviewed": True})
    )
    provider(
        context,
        {
            "entities": [
                {
                    "ref": str(i),
                    "kind": "person",
                    "name": "Casey Doe",
                    "emails": [address] if address else [],
                    "quote": quotes[i],
                }
                for i, address in enumerate(addresses)
            ],
            "claims": [
                {"subject": str(i), "predicate": "interest", "value": interest, "quote": quotes[i]}
                for i, interest in enumerate(("sailing", "hiking"))
            ],
        },
    )
    job = context.load_job(await context.queue_reviewed(note.id))
    await context.run_job(job)
    assert job.state == "applied", job.error
    entities = list(store.entities.load().entities.values())
    assert len(entities) == 2
    for address, interest in zip(addresses, ("sailing", "hiking"), strict=True):
        entity = next(e for e in entities if any(f.value == interest for f in e.facts))
        assert entity.emails == ([address] if address else [])
        assert [f.value for f in entity.facts if f.predicate == "interest"] == [interest]
    before = store.entities.load().model_dump()
    await context.run_job(job)
    assert store.entities.load().model_dump() == before


async def test_filing_preserves_extracted_frontmatter_evidence(store):
    context = await configure(store)
    await context.setup_lifecycle({"revision": context.config().revision})
    ingested = await store.ingest(
        IngestRequest.model_validate(
            {
                "source": {
                    "provider": "plaud",
                    "external_source_id": "structured-filing",
                    "source_type": "transcript",
                    "captured_at": "2026-10-20T17:00:00Z",
                    "artifacts": [
                        {
                            "name": "transcript.txt",
                            "mime_type": "text/plain",
                            "content": "Meeting transcript.",
                        }
                    ],
                },
                "note": {
                    "title": "Structured filing",
                    "body": "",
                    "frontmatter": {
                        "type": "work",
                        "people": ["Casey Doe"],
                        "interests": ["sailing"],
                    },
                },
            }
        )
    )
    note = await store.get_note(ingested.note.id)
    await context.complete_review(
        {"note_id": note.id, "if_match": note.content_hash, "confirmation": "Complete review"}
    )
    note = await store.get_note(note.id)
    provider(
        context,
        {
            "entities": [{"ref": "p", "kind": "person", "name": "Casey Doe", "quote": "Casey Doe"}],
            "claims": [
                {
                    "subject": "p",
                    "predicate": "interest",
                    "value": "sailing",
                    "quote": "sailing",
                    "field": "interests",
                }
            ],
        },
    )
    job = context.load_job(await context.queue_reviewed(note.id))
    await context.run_job(job)
    assert job.state == "applied", job.error
    before = next(iter(store.entities.load().entities.values()))
    assert await file_note(store, note.id)
    filed = await store.get_note(note.id)
    assert filed.content_hash != note.content_hash
    assert filed.path.startswith("Notes/2026/2026-10/")
    await store.entities.refresh()
    entity = store.entities.load().entities[before.id]
    assert {f.id for f in entity.facts} == {f.id for f in before.facts}
    assert all(f.state == "confirmed" for f in entity.facts)
    assert all(f.evidence.note_hash == filed.content_hash for f in entity.facts)
    assert context.load_job(job.id).note_hash == filed.content_hash
    assert await context.queue_reviewed(note.id) == job.id


async def test_review_extracts_projects_and_enriches_without_an_acceptance_step(store):
    context, source, extraction, person, company = await contribute(store)
    assert {f.predicate for f in person.facts} == {"organization", "role", "interest", "mentioned"}
    assert all(f.evidence.note_hash == source.content_hash for f in person.facts)
    assert "sailing" in (store.notes_root / person.path).read_text()
    await store.entities.create(EntityInput(kind="person", name="Unrelated Person"))
    note = await store.create_note(
        CreateNote(
            title="Next conversation",
            folder="Review",
            body="Discuss sailing with casey@example.test.",
        )
    )
    link = "[[" + person.path.removesuffix(".md") + "]]"
    seen = provider(
        context,
        {
            "suggestions": [
                {
                    "kind": "field",
                    "field": "account",
                    "value": [link],
                    "entity_ids": [person.id],
                    "quote": "sailing",
                    "confidence": 0.95,
                    "reason": "The known person's interest supports this conversation.",
                }
            ]
        },
    )
    job = context.load_job(await context.queue_note(note.id))
    await context.run_job(job)
    assert job.state == "applied", job.error
    current = await store.get_note(note.id)
    assert current.frontmatter["account"] == [link]
    assert current.path == note.path and current.body == note.body
    assert current.frontmatter["reviewed"] is False
    assert {d["id"] for d in seen[0]["dossiers"]} >= {person.id}
    assert not any(d["name"] == "Unrelated Person" for d in seen[0]["dossiers"])
    assert job.usage["provider_model_version"] == "test-model-version"
    assert job.decisions[0]["automatic"] is True
    assert ContextService(store).load_job(job.id).state == "applied"
    # A restart after committing extraction cannot duplicate claims or undo corrections.
    before = store.entities.load().model_dump()
    await context.run_job(extraction)
    assert store.entities.load().model_dump() == before
    # Editing the supporting note removes only its facts; a page correction survives.
    path = store.notes_root / person.path
    path.write_text(path.read_text().replace("interest: sailing", "interest: hiking"))
    await store.entities.refresh()
    source = await store.patch_frontmatter(
        source.id, PatchFrontmatter(set={"reviewed": False}), source.content_hash
    )
    await store.entities.refresh()
    facts = (await store.entities.get(person.id))["facts"]
    assert [f["value"] for f in facts if f["state"] == "confirmed"] == ["hiking"]


@pytest.mark.parametrize(
    "confidence,owned,folder",
    [(0.3, False, "Review"), (0.95, True, "Review"), (0.95, False, "Other")],
)
async def test_held_or_owned_values_never_write(store, confidence, owned, folder):
    context = await configure(store)
    note = await store.create_note(
        CreateNote(
            title="Discussion",
            folder=folder,
            body="Talk about sailing.",
            frontmatter={"account": ["Human"]} if owned else {},
        )
    )
    seen = provider(
        context,
        {
            "suggestions": [
                {
                    "kind": "field",
                    "field": "account",
                    "value": ["Sailing"],
                    "quote": "sailing",
                    "confidence": confidence,
                    "reason": "Topic",
                }
            ],
            "unresolved": ["Unknown employer"],
        },
    )
    job = context.load_job(await context.queue_note(note.id))
    await context.run_job(job)
    assert job.state == "suggested", job.error
    assert (await store.get_note(note.id)).content_hash == note.content_hash
    assert "Unknown employer" not in (store.notes_root / note.path).read_text()
    if owned:
        assert "account" not in {f["name"] for f in seen[0]["allowed_fields"]}


async def test_dossier_correction_invalidates_a_held_suggestion(store):
    context, _, _, person, _ = await contribute(store)
    note = await store.create_note(
        CreateNote(title="Discussion", folder="Review", body="casey@example.test")
    )
    provider(
        context,
        {
            "suggestions": [
                {
                    "kind": "field",
                    "field": "account",
                    "value": ["Sailing"],
                    "entity_ids": [person.id],
                    "quote": "sailing",
                    "confidence": 0.3,
                    "reason": "Interest",
                }
            ]
        },
    )
    job = context.load_job(await context.queue_note(note.id))
    await context.run_job(job)
    current = await store.entities.get(person.id)
    await store.entities.change(
        person.id,
        EntityChange(
            version=current["version"],
            action="correct",
            fact_id=next(f["id"] for f in current["facts"] if f["predicate"] == "interest"),
            predicate="interest",
            value="hiking",
        ),
    )
    with pytest.raises(ValueError, match="stale"):
        await context.approve(
            {"id": job.id, "index": 0, "confirmation": "Apply selected suggestion"}
        )
    assert (await store.get_note(note.id)).content_hash == note.content_hash


async def test_extraction_uses_frontmatter_evidence_and_withdraws_on_deletion(store):
    context = await configure(store)
    note = await store.create_note(
        CreateNote(
            title="Structured",
            body="",
            frontmatter={"reviewed": True, "people": ["Casey Doe"], "interests": ["sailing"]},
        )
    )
    provider(
        context,
        {
            "entities": [{"ref": "p", "kind": "person", "name": "Casey Doe", "quote": "Casey Doe"}],
            "claims": [
                {
                    "subject": "p",
                    "predicate": "interest",
                    "value": "sailing",
                    "quote": "sailing",
                    "field": "interests",
                }
            ],
        },
    )
    job = context.load_job(await context.queue_reviewed(note.id))
    await context.run_job(job)
    assert job.state == "applied", job.error
    entity = next(iter(store.entities.load().entities.values()))
    assert entity.facts[0].origin == "metadata"
    (store.notes_root / note.path).unlink()
    await reconcile_once(store)
    assert all(f["state"] == "withdrawn" for f in (await store.entities.get(entity.id))["facts"])


async def test_reviewed_during_model_call_is_frozen(store):
    context = await configure(store)
    note = await store.create_note(CreateNote(title="Race", folder="Review", body="sailing"))

    class RacingProvider:
        async def suggest(self, *_args):
            await store.patch_frontmatter(
                note.id, PatchFrontmatter(set={"reviewed": True}), note.content_hash
            )
            return {
                "suggestions": [
                    {
                        "kind": "field",
                        "field": "account",
                        "value": ["Sailing"],
                        "quote": "sailing",
                        "confidence": 1,
                        "reason": "Topic",
                    }
                ]
            }, {}

    context.provider = RacingProvider()
    job = context.load_job(await context.queue_note(note.id))
    await context.run_job(job)
    assert job.state == "stale"
    current = await store.get_note(note.id)
    assert not current.frontmatter.get("account")
    assert current.body == note.body


async def test_worker_queues_reviewed_facts_and_rejects_unsupported_quotes_atomically(store):
    context = await configure(store)
    note = await store.create_note(
        CreateNote(title="Reviewed", body=BODY, frontmatter={"reviewed": True})
    )
    output = extracted()
    output["claims"][-1]["quote"] = "Not in the reviewed note"
    seen = provider(context, output)
    await context.tick()
    jobs = [j for j in context.jobs() if j.note_id == note.id]
    assert len(jobs) == 1 and jobs[0].purpose == "dossier"
    assert jobs[0].state == "failed"
    assert len(seen) == 1
    assert not store.entities.load().entities
    retried = (await context.retry_job({"id": jobs[0].id}))["job"]
    assert retried != jobs[0].id
    provider(context, extracted())
    await context.tick()
    assert context.load_job(retried).state == "applied"
    assert context.load_job(jobs[0].id).state == "failed"


async def test_automatic_write_receipt_resumes_after_file_write_before_acknowledgement(
    store, monkeypatch
):
    context = await configure(store)
    note = await store.create_note(CreateNote(title="Interrupted", folder="Review", body="sailing"))
    provider(
        context,
        {
            "suggestions": [
                {
                    "kind": "field",
                    "field": "account",
                    "value": ["Sailing"],
                    "quote": "sailing",
                    "confidence": 1,
                    "reason": "Topic",
                }
            ]
        },
    )
    original = store.patch_frontmatter

    async def interrupted(*args, **kwargs):
        await original(*args, **kwargs)
        raise OSError("simulated lost acknowledgement")

    monkeypatch.setattr(store, "patch_frontmatter", interrupted)
    job = context.load_job(await context.queue_note(note.id))
    await context.run_job(job)
    assert job.state == "applying"
    assert (await store.get_note(note.id)).frontmatter["account"] == ["Sailing"]
    monkeypatch.setattr(store, "patch_frontmatter", original)
    restarted = ContextService(store)
    await restarted.resume_approval({"id": job.id})
    saved = restarted.load_job(job.id)
    assert saved.state == "applied"
    assert len(saved.decisions) == 1
    assert (await store.get_note(note.id)).body == note.body


async def test_mapped_attendees_accept_dossier_links_without_calendar_contacts(store):
    context, _, _, person, _ = await contribute(store)
    await context.configure_property(
        {"property": "attendees", "revision": context.config().revision}
    )
    note = await store.create_note(
        CreateNote(
            title="Attendance", folder="Review", body="casey@example.test attended the call."
        )
    )
    link = "[[" + person.path.removesuffix(".md") + "]]"
    provider(
        context,
        {
            "suggestions": [
                {
                    "kind": "field",
                    "field": "attendees",
                    "value": [link],
                    "entity_ids": [person.id],
                    "quote": "casey@example.test attended the call.",
                    "confidence": 0.95,
                    "reason": "Explicit attendance and exact email.",
                }
            ]
        },
    )
    job = context.load_job(await context.queue_note(note.id))
    await context.run_job(job)
    assert job.state == "applied", job.error
    assert (await store.get_note(note.id)).frontmatter["attendees"] == [link]


async def test_multiple_blank_fields_fill_from_one_model_answer(store):
    context = await configure(store)
    config = context.config()
    config.assignments["enrichment"].fields = ["account", "tags"]
    context.save_config(config)
    note = await store.create_note(CreateNote(title="Two fields", folder="Review", body="sailing"))
    seen = provider(
        context,
        {
            "suggestions": [
                {
                    "kind": "field",
                    "field": "account",
                    "value": ["Sailing"],
                    "quote": "sailing",
                    "confidence": 0.95,
                    "reason": "Topic",
                },
                {
                    "kind": "field",
                    "field": "tags",
                    "value": ["sailing"],
                    "quote": "sailing",
                    "confidence": 0.95,
                    "reason": "Topic",
                },
            ]
        },
    )
    job = context.load_job(await context.queue_note(note.id))
    await context.run_job(job)
    assert job.state == "applied", job.error
    current = await store.get_note(note.id)
    assert current.frontmatter["account"] == ["Sailing"]
    assert current.frontmatter["tags"] == ["sailing"]
    assert len(seen) == 1 and len(job.decisions) == 2
    assert current.body == note.body and current.path == note.path


async def test_purge_removes_supporting_quotes_from_other_notes_enrichment_snapshots(store):
    from tests.integration.test_note_recovery import commit, git
    from tests.integration.test_purge import confirmation, retention

    context, source, _, person, _ = await contribute(store)
    note = await store.create_note(
        CreateNote(title="Held context", folder="Review", body="casey@example.test")
    )
    provider(
        context,
        {
            "suggestions": [
                {
                    "kind": "field",
                    "field": "account",
                    "value": ["Sailing"],
                    "entity_ids": [person.id],
                    "quote": "sailing",
                    "confidence": 0.3,
                    "reason": "Interest",
                }
            ]
        },
    )
    job = context.load_job(await context.queue_note(note.id))
    await context.run_job(job)
    assert job.evidence["dossiers"]
    git(store.notes_root, "init", "-q")
    commit(store.notes_root)
    (store.notes_root / source.path).unlink()
    commit(store.notes_root)
    await reconcile_once(store)
    retention(store, 0)
    preview = await store.preview_purge()
    assert source.id in {c.id for c in preview.candidates}
    await store.confirm_purge(confirmation(preview))
    saved = context.load_job(job.id)
    assert saved.state == "stale" and saved.result is None
    assert not saved.evidence.get("dossiers")
    assert not saved.decisions
    assert BODY not in saved.model_dump_json()


async def test_notes_reviewed_before_enablement_are_extracted_without_rereview(store):
    old = await store.create_note(
        CreateNote(title="Old reviewed", body=BODY, frontmatter={"reviewed": True})
    )
    context = await configure(store)
    seen = provider(context, extracted())
    await context.tick()
    assert [j.purpose for j in context.jobs() if j.note_id == old.id] == ["dossier"]
    assert len(seen) == 1
    fresh = await store.create_note(
        CreateNote(title="New reviewed", body=BODY, frontmatter={"reviewed": True})
    )
    await context.tick()
    assert [j.purpose for j in context.jobs() if j.note_id == fresh.id] == ["dossier"]
    assert len(seen) == 2


async def test_old_exhausted_extraction_is_retried_once_under_new_version(store):
    context = await configure(store)
    note = await store.create_note(
        CreateNote(title="Reviewed", body=BODY, frontmatter={"reviewed": True})
    )
    old = context.load_job(await context.queue_reviewed(note.id))
    old.extraction_version = ""
    old.state, old.attempt_limit, old.attempts = "failed", 2, 2
    # Simulate a legacy ID, which did not include source versions or extraction version.
    (context.root / "jobs" / (old.id + ".json")).unlink()
    old.id = "a" * 64
    context.save_job(old)
    seen = provider(context, extracted())
    await context.tick()
    assert len(seen) == 1
    await context.tick()
    assert len(seen) == 1


async def test_applied_legacy_extraction_is_requeued_under_current_version(store):
    context = await configure(store)
    note = await store.create_note(
        CreateNote(title="Reviewed", body=BODY, frontmatter={"reviewed": True})
    )
    legacy = context.load_job(await context.queue_reviewed(note.id))
    (context.root / "jobs" / (legacy.id + ".json")).unlink()
    legacy.id = "a" * 64
    legacy.extraction_version = ""
    legacy.state = "applied"
    context.save_job(legacy)
    current_id = await context.queue_reviewed(note.id)
    assert current_id != legacy.id
    current = context.load_job(current_id)
    assert current.state == "queued" and current.extraction_version


async def test_source_only_rerun_does_not_duplicate_note_backed_facts(store, monkeypatch):
    from types import SimpleNamespace as NS

    context = await configure(store)
    note = await store.create_note(
        CreateNote(title="Reviewed", body=BODY, frontmatter={"reviewed": True})
    )
    _fake_transcript(store, monkeypatch, "unrelated transcript")
    revision = 1
    artifact = NS(name="t.txt", mime_type="text/plain", size_bytes=20)

    async def source(_identity):
        return NS(
            current_revision=revision,
            revisions=[NS(revision=revision, artifacts=[artifact])],
        )

    monkeypatch.setattr(store, "get_source", source)
    provider(context, extracted())
    first = context.load_job(await context.queue_reviewed(note.id))
    await context.run_job(first)
    assert first.state == "applied", first.error
    before = {
        entity.id: [fact.id for fact in entity.facts if fact.evidence.note_id == note.id]
        for entity in store.entities.load().entities.values()
    }
    revision = 2
    second = context.load_job(await context.queue_reviewed(note.id))
    assert second.id != first.id
    await context.run_job(second)
    assert second.state == "applied", second.error
    after = {
        entity.id: [fact.id for fact in entity.facts if fact.evidence.note_id == note.id]
        for entity in store.entities.load().entities.values()
    }
    assert after == before


async def test_dossier_job_made_stale_by_a_settings_change_is_requeued(store):
    context = await configure(store)
    note = await store.create_note(
        CreateNote(title="Reviewed", body=BODY, frontmatter={"reviewed": True})
    )
    seen = provider(context, extracted())
    job = context.load_job(await context.queue_reviewed(note.id))
    await context.save_assignment(
        {
            "revision": 2,
            "assignment": context.config()
            .assignments["enrichment"]
            .model_copy(update={"fields": []})
            .model_dump(),
        }
    )
    await context.tick()
    assert context.load_job(job.id).state == "stale"
    assert not seen
    await context.tick()
    assert context.load_job(job.id).state == "applied"
    assert len(seen) == 1


async def test_ordinary_note_wikilink_in_an_unmapped_field_is_accepted(store):
    context = await configure(store)
    note = await store.create_note(
        CreateNote(title="Discussion", folder="Review", body="See [[Projects/Apollo]] for sailing.")
    )
    provider(
        context,
        {
            "suggestions": [
                {
                    "kind": "field",
                    "field": "account",
                    "value": ["[[Projects/Apollo]]"],
                    "quote": "[[Projects/Apollo]]",
                    "confidence": 0.5,
                    "reason": "Linked project",
                }
            ],
            "unresolved": [],
        },
    )
    job = context.load_job(await context.queue_note(note.id))
    await context.run_job(job)
    assert job.state == "suggested", job.error


async def test_unfillable_suggestion_is_skipped_without_failing_the_job_or_later_fills(store):
    context = await configure(store)
    await context.setup_lifecycle({"revision": context.config().revision})
    config = context.config()
    config.assignments["enrichment"].fields = ["tags"]
    config.assignments["enrichment"].event_field = "account"
    context.save_config(config)
    note = await store.create_note(
        CreateNote(
            title="Planning",
            folder="Review",
            body="Casey attended sailing",
            frontmatter={"date": "2026-10-14"},
        )
    )
    seen = []

    def answer(evidence):
        seen.append(evidence)
        event = evidence["events"][0]["event_id"]
        return {
            "suggestions": [
                {"kind": "event", "event_ids": [event, event], "confidence": 0.95, "reason": "Two"},
                {
                    "kind": "field",
                    "field": "tags",
                    "value": ["sailing"],
                    "quote": "sailing",
                    "confidence": 0.95,
                    "reason": "Topic",
                },
            ]
        }

    provider(context, answer)
    job = context.load_job(await context.queue_note(note.id))
    await context.run_job(job)
    saved = context.load_job(job.id)
    assert saved.state == "suggested" and saved.pending_approval is None, saved.error
    assert [d["index"] for d in saved.decisions] == [1]
    assert (await store.get_note(note.id)).frontmatter["tags"] == ["sailing"]
    await context.tick()
    assert context.load_job(job.id).state == "suggested"
    assert len(seen) == 1


async def test_a_suggestion_the_note_cannot_take_is_skipped_once_and_stays_available(
    store, monkeypatch
):
    context = await configure(store)
    await context.setup_lifecycle({"revision": context.config().revision})
    config = context.config()
    config.assignments["enrichment"].attendees_field = "account"
    context.save_config(config)
    note = await store.create_note(
        CreateNote(
            title="Planning",
            folder="Review",
            body="Casey attended",
            frontmatter={"date": "2026-10-14", "account": ""},
        )
    )

    def answer(evidence):
        person = evidence["events"][0]["people"][0]["contact_id"]
        return {
            "suggestions": [
                {
                    "kind": "attendee",
                    "contact_ids": [person],
                    "event_ids": [evidence["events"][0]["event_id"]],
                    "quote": "Casey attended",
                    "confidence": 0.95,
                    "reason": "Attended",
                }
            ]
        }

    provider(context, answer)
    job = context.load_job(await context.queue_note(note.id))
    await context.run_job(job)
    saved = context.load_job(job.id)
    assert saved.state == "suggested" and saved.pending_approval is None, saved.error
    assert [d["skipped"] for d in saved.decisions] == [0]
    assert saved.decisions[0]["reason"] == "mapped association field must contain a list"
    assert saved.decided == set()
    calls = []
    original = context.approve

    async def counted(*args, **kwargs):
        calls.append(args)
        return await original(*args, **kwargs)

    monkeypatch.setattr(context, "approve", counted)
    await context.tick()
    assert calls == []
    assert context.load_job(job.id).state == "suggested"
    await context.reject({"id": job.id, "index": 0})
    assert context.load_job(job.id).state == "rejected"


async def test_requeue_asks_for_calendar_suggestions_only_unless_dossiers_changed(store):
    import base64

    from services.store.tests.test_calendar_import import event, ics

    context = await configure(store)
    await context.setup_lifecycle({"revision": context.config().revision})
    note = await store.create_note(
        CreateNote(title="Planning", body="Casey Doe attended", frontmatter={"date": "2026-10-14"})
    )
    provider(context, {"suggestions": []})
    await context.tick()

    def newest():
        return max((j for j in context.jobs() if j.note_id == note.id), key=lambda j: j.created_at)

    first = newest()
    assert first.result is not None and first.purpose == "initial"
    receipt = await context.upload(
        {
            "calendar_id": "calendar",
            "filename": "moved.ics",
            "content_base64": base64.b64encode(ics(event(sequence=1, title="Moved"))).decode(),
        }
    )
    await context.retry_import({"id": receipt["id"]})
    await context.tick()
    second = newest()
    assert second.id != first.id and second.purpose == "calendar"
    await store.entities.create(EntityInput(kind="person", name="Casey Doe"))
    await context.tick()
    third = newest()
    assert third.id != second.id and third.purpose == "initial"


async def test_review_detail_does_not_fail_on_oversized_source_metadata(store, monkeypatch):
    context = await configure(store)
    await context.setup_lifecycle({"revision": context.config().revision})
    note = await store.create_note(
        CreateNote(title="Planning", body="Casey attended", frontmatter={"date": "2026-10-14"})
    )
    job = context.load_job(await context.queue_note(note.id))
    assert (await context.lifecycle_detail({"note_id": note.id}))["stale"] is False
    _fake_transcript(store, monkeypatch, "x", size_bytes=10_000_000)
    detail = await context.lifecycle_detail({"note_id": note.id})
    assert detail["stale"] is False and detail["job_id"] == job.id


async def test_written_fill_is_acknowledged_when_evidence_can_no_longer_be_rebuilt(
    store, monkeypatch
):
    context = await configure(store)
    config = context.config()
    config.assignments["enrichment"].fields = ["account", "tags"]
    context.save_config(config)
    note = await store.create_note(CreateNote(title="Two fields", folder="Review", body="sailing"))
    suggestion = {"quote": "sailing", "confidence": 0.95, "reason": "Topic", "kind": "field"}
    provider(
        context,
        {
            "suggestions": [
                {**suggestion, "field": "account", "value": ["Sailing"]},
                {**suggestion, "field": "tags", "value": ["sailing"]},
            ]
        },
    )
    original = store.patch_frontmatter

    async def interrupted(*args, **kwargs):
        await original(*args, **kwargs)
        raise OSError("simulated lost acknowledgement")

    monkeypatch.setattr(store, "patch_frontmatter", interrupted)
    job = context.load_job(await context.queue_note(note.id))
    await context.run_job(job)
    assert context.load_job(job.id).state == "applying"
    monkeypatch.setattr(store, "patch_frontmatter", original)

    async def unavailable_evidence(*args, **kwargs):
        raise ValueError("evidence unavailable")

    monkeypatch.setattr(context, "note_evidence_hash", unavailable_evidence)
    await context.resume_approval({"id": job.id})
    saved = context.load_job(job.id)
    assert saved.pending_approval is None and len(saved.decisions) == 1
    assert saved.state == "stale" and "regenerate" in saved.error
    current = await store.get_note(note.id)
    assert current.frontmatter["account"] == ["Sailing"]
    assert "tags" not in current.frontmatter or not current.frontmatter["tags"]


async def test_a_note_with_more_than_five_sources_is_extracted_from_the_first_five(
    store, monkeypatch
):
    from types import SimpleNamespace as NS

    context = await configure(store)
    note = await store.create_note(
        CreateNote(
            title="Reviewed meeting",
            body=BODY,
            frontmatter={"reviewed": True, "attendees": ["casey@example.test"]},
        )
    )
    artifact = NS(name="t.txt", mime_type="text/plain", size_bytes=10)
    manifest = NS(current_revision=3, revisions=[NS(revision=3, artifacts=[artifact])])

    async def sources(note_id):
        return [NS(id=f"s{n}") for n in range(1, 8)]

    async def source(identity):
        return manifest

    async def document(identity, revision, name):
        return NS(content=f"casey@example.test spoke in {identity}.")

    monkeypatch.setattr(store, "get_note_sources", sources)
    monkeypatch.setattr(store, "get_source", source)
    monkeypatch.setattr(store, "get_source_artifact", document)
    seen = provider(context, extracted())
    job = context.load_job(await context.queue_reviewed(note.id))
    assert job.source_versions == {f"s{n}": 3 for n in range(1, 6)}
    await context.run_job(job)
    assert job.state == "applied", job.error
    assert seen[0]["transcript_sources_omitted"] == 2
    assert job.evidence["transcript_sources_omitted"] == 2
    sent = "\n".join(seen[0]["transcripts"])
    assert "in s5." in sent and "in s6." not in sent


async def test_applied_extraction_job_keeps_identifiers_not_the_note_or_dossier_snapshots(store):
    context, source, extraction, person, company = await contribute(store)
    saved = context.load_job(extraction.id)
    assert saved.state == "applied"
    assert saved.evidence == {
        "note_id": source.id,
        "note_hash": source.content_hash,
        "entity_ids": sorted([person.id, company.id]),
        "transcript_omitted_characters": 0,
        "transcript_sources_omitted": 0,
    }
    assert BODY not in saved.model_dump_json()


async def test_a_model_filling_every_advertised_field_is_not_rejected(store):
    context = await configure(store)
    config = context.config()
    config.assignments["enrichment"].fields = ["account"]
    config.assignments["enrichment"].invitees_field = "tags"
    config.assignments["enrichment"].event_field = "context"
    context.save_config(config)
    note = await store.create_note(
        CreateNote(title="Sailing", body="sailing", frontmatter={"date": "2026-10-14"})
    )

    def answer(evidence):
        return {
            "suggestions": [
                {
                    "kind": "field",
                    "field": field["name"],
                    "value": ["Sailing"],
                    "quote": "sailing",
                    "confidence": 0.5,
                    "reason": "Topic",
                }
                for field in evidence["allowed_fields"]
            ]
        }

    seen = provider(context, answer)
    job = context.load_job(await context.queue_note(note.id))
    await context.run_job(job)
    assert [f["name"] for f in seen[0]["allowed_fields"]] == ["account"]
    assert job.state == "suggested", job.error
    assert [s.field for s in job.result.suggestions] == ["account"]


async def test_a_retired_identity_in_a_note_does_not_block_the_other_people_in_it(store):
    context = await configure(store)
    gone = await store.entities.create(
        EntityInput(kind="person", name="Gone Person", emails=["gone@example.test"])
    )
    (store.notes_root / gone["path"]).unlink()
    await store.entities.refresh()
    assert store.entities.load().entities[gone["id"]].retired_at is not None
    quotes = [
        "Gone Person (gone@example.test) enjoys hiking.",
        "Casey Doe (casey@example.test) enjoys sailing.",
    ]
    note = await store.create_note(
        CreateNote(title="Two people", body="\n".join(quotes), frontmatter={"reviewed": True})
    )
    seen = provider(
        context,
        {
            "entities": [
                {
                    "ref": "gone",
                    "kind": "person",
                    "name": "Gone Person",
                    "emails": ["gone@example.test"],
                    "quote": quotes[0],
                },
                {
                    "ref": "casey",
                    "kind": "person",
                    "name": "Casey Doe",
                    "emails": ["casey@example.test"],
                    "quote": quotes[1],
                },
            ],
            "claims": [
                {"subject": "gone", "predicate": "interest", "value": "hiking", "quote": quotes[0]},
                {
                    "subject": "casey",
                    "predicate": "interest",
                    "value": "sailing",
                    "quote": quotes[1],
                },
            ],
        },
    )
    job = context.load_job(await context.queue_reviewed(note.id))
    await context.run_job(job)
    assert job.state == "applied", job.error
    assert len(seen) == 1
    entities = store.entities.load().entities
    casey = next(e for e in entities.values() if e.emails == ["casey@example.test"])
    assert [f.value for f in casey.facts if f.predicate == "interest"] == ["sailing"]
    assert entities[gone["id"]].retired_at is not None
    assert not entities[gone["id"]].facts


async def test_tick_reads_the_dossier_catalog_once_however_many_notes_it_queues(store, monkeypatch):
    context = await configure(store)
    loads = []
    original = store.entities.load

    def counted():
        loads.append(1)
        return original()

    monkeypatch.setattr(store.entities, "load", counted)

    async def reads_during_tick(count):
        for i in range(count):
            await store.create_note(
                CreateNote(title=f"Note {i}", body="sailing", frontmatter={"date": "2026-10-14"})
            )
        loads.clear()
        await context.tick()
        return len(loads)

    one = await reads_during_tick(1)
    many = await reads_during_tick(5)
    assert many == one


async def test_reviewed_recovery_selects_newest_matching_dossier_job(store):
    context = await configure(store)
    note = await store.create_note(
        CreateNote(title="Reviewed meeting", body=BODY, frontmatter={"reviewed": True})
    )
    first = context.load_job(await context.queue_reviewed(note.id))
    first.state = "failed"
    context.save_job(first)
    second = context.load_job((await context.retry_job({"id": first.id}))["job"])
    second.state = "failed"
    context.save_job(second)
    config = context.config()
    config.assignments["enrichment"].timeout_s += 1
    context.save_config(config)

    selected = await context.queue_reviewed(note.id, [first, second])

    assert selected == second.id
    assert context.load_job(first.id).state == "failed"
    assert context.load_job(second.id).state == "queued"


async def test_reverted_edit_gets_a_new_newest_dossier_job_not_an_older_one(store):
    context = await configure(store)
    note = await store.create_note(
        CreateNote(title="Reviewed meeting", body=BODY, frontmatter={"reviewed": True})
    )
    first = context.load_job(await context.queue_reviewed(note.id))
    first.state = "failed"
    context.save_job(first)
    edited = await store.patch_frontmatter(
        note.id, PatchFrontmatter(set={"mood": "b"}), note.content_hash
    )
    second = context.load_job(await context.queue_reviewed(note.id))
    reverted = await store.patch_frontmatter(
        note.id, PatchFrontmatter(unset=["mood"]), edited.content_hash
    )
    assert reverted.content_hash == note.content_hash

    selected = await context.queue_reviewed(note.id)

    assert selected not in {first.id, second.id}
    assert context.newest_job_id(note.id) == selected
    assert context.load_job(selected).note_hash == note.content_hash
    assert context.load_job(first.id).state == "failed"
