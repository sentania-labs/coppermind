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
from tests.integration.test_context_enrichment import configure

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
    await context.retry_job({"id": jobs[0].id})
    provider(context, extracted())
    await context.tick()
    assert context.load_job(jobs[0].id).state == "applied"


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


async def test_notes_reviewed_before_enablement_are_not_swept_to_the_provider(store):
    old = await store.create_note(
        CreateNote(title="Old reviewed", body=BODY, frontmatter={"reviewed": True})
    )
    context = await configure(store)
    seen = provider(context, extracted())
    await context.tick()
    assert not [j for j in context.jobs() if j.note_id == old.id]
    assert not seen
    fresh = await store.create_note(
        CreateNote(title="New reviewed", body=BODY, frontmatter={"reviewed": True})
    )
    await context.tick()
    assert [j.purpose for j in context.jobs() if j.note_id == fresh.id] == ["dossier"]
    assert len(seen) == 1


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
