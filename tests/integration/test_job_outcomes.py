"""Every accepted job ends in a recorded, logged and queryable outcome."""

import logging

import httpx
from coppermind_store.auth import InternalAuth
from coppermind_store.context_api import router as context_router
from coppermind_store.model_provider import ProviderError
from fastapi import FastAPI

from coppermind.store_client import HttpStoreClient
from coppermind.store_protocol import CreateNote, PatchFrontmatter
from tests.integration.test_context_enrichment import configure
from tests.integration.test_store_write_protocol import authorized_api

CONFIRM = "Send selected notes for enrichment"


async def api_for(store):
    internal = FastAPI()
    internal.state.store = store
    internal.state.auth = InternalAuth("synthetic-internal-token")
    internal.include_router(context_router)
    app, headers = authorized_api(store)
    app.state.store = HttpStoreClient(
        "http://store", "synthetic-internal-token", transport=httpx.ASGITransport(app=internal)
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api", headers=headers
    )


async def a_note(store, name="Planning"):
    return await store.create_note(
        CreateNote(title=name, body="Casey attended.", frontmatter={"date": "2026-10-14"})
    )


async def ask(context, note, *, regenerate=True):
    queued = await context.enqueue(
        {"note_ids": [note.id], "confirmation": CONFIRM, "regenerate": regenerate}
    )
    return queued["jobs"][0]


async def test_a_replaced_job_and_the_job_that_replaced_it_both_report(store, caplog):
    note = await a_note(store)
    context = await configure(store)
    older, newer = await ask(context, note), await ask(context, note)
    assert older != newer
    assert (await context.job_outcome({"id": older}))["state"] == "pending"
    caplog.set_level(logging.INFO)
    await context.tick()
    first = await context.job_outcome({"id": older})
    assert first["state"] == "superseded" and first["replaced_by"] == newer
    assert first["finished_at"] and "replaced" in first["error"]
    second = await context.job_outcome({"id": newer})
    assert second["state"] == "succeeded" and second["error"] == "" and second["replaced_by"] == ""
    assert f"enrichment job {older}" in caplog.text and "outcome=superseded" in caplog.text
    assert f"enrichment job {newer}" in caplog.text and "outcome=succeeded" in caplog.text
    async with await api_for(store) as api:
        seen = await api.get("/v1/jobs/" + older)
        assert seen.status_code == 200 and seen.json()["state"] == "superseded"
        assert seen.json()["replaced_by"] == newer
        assert (await api.get("/v1/jobs/" + "0" * 64)).status_code == 422
        assert (await api.get("/v1/jobs/..%2Fconfig")).status_code in {404, 422}


async def test_a_job_that_uses_its_attempts_reports_failed_with_the_reason(store, caplog):
    note = await a_note(store)
    context = await configure(store)

    async def refuse(*args, **kwargs):
        raise ProviderError("synthetic outage", "provider-error")

    context.provider.suggest = refuse
    job = await ask(context, note)
    caplog.set_level(logging.INFO)
    await context.tick()
    waiting = await context.job_outcome({"id": job})
    assert waiting["state"] == "pending" and waiting["attempts"] == 1
    await context.tick()
    done = await context.job_outcome({"id": job})
    assert done["state"] == "failed" and done["attempts"] == done["attempt_limit"] == 2
    assert "synthetic outage" in done["error"] and done["finished_at"]
    assert f"enrichment job {job} for note" in caplog.text and "outcome=failed" in caplog.text


async def test_a_job_for_a_note_reviewed_before_it_ran_reports_skipped(store):
    note = await a_note(store)
    context = await configure(store)
    job = await ask(context, note)
    await store.patch_frontmatter(
        note.id,
        PatchFrontmatter(set={"reviewed": True}),
        if_match=(await store.get_note(note.id)).content_hash,
    )
    await context.run_job(context.load_job(job))
    done = await context.job_outcome({"id": job})
    assert done["state"] == "skipped" and "reviewed" in done["error"] and done["finished_at"]


async def test_a_retried_job_is_not_finished_again_and_keeps_one_outcome(store):
    note = await a_note(store)
    context = await configure(store)
    job = await ask(context, note)
    await context.run_job(context.load_job(job))
    first = await context.job_outcome({"id": job})
    assert first["state"] == "succeeded"
    again = context.load_job(job)
    context.save_job(again)
    assert (await context.job_outcome({"id": job}))["finished_at"] == first["finished_at"]


async def test_recordings_can_be_found_again_and_say_how_their_summary_stands(store):
    from services.store.tests.test_transcription_call import Service
    from tests.integration.test_recording_writeup import BOUNDARY_BLOCK
    from tests.integration.test_recordings import request, setup_transcription, store_audio

    async with Service() as service:
        context = await setup_transcription(store, service)

        async def write_text(connection, key, limits, system, evidence, **kwargs):
            return (
                "# " + evidence["resolved_title"] + "\n\n## Top line\nDone.\n" + BOUNDARY_BLOCK
            ), {}

        context.provider.write_text = write_text
        audio = await store_audio(store)
        one = await context.accept_recording(request(audio, external_source_id="plaud/one"))
        two = await context.accept_recording(
            request(audio, external_source_id="plaud/two", provider="other")
        )
        assert one["summary_state"] == "pending" and one["external_source_id"] == "plaud/one"
        await context.recordings.tick()
        await context.recordings.tick()
        async with await api_for(store) as api:
            everything = (await api.get("/v1/recordings")).json()
            assert [item["id"] for item in everything["items"]] == [two["id"], one["id"]]
            assert everything["next_cursor"] is None
            assert {item["summary_state"] for item in everything["items"]} == {"succeeded"}
            found = (await api.get("/v1/recordings?external_source_id=plaud/one")).json()
            assert [item["id"] for item in found["items"]] == [one["id"]]
            other = (await api.get("/v1/recordings?provider=other")).json()
            assert [item["id"] for item in other["items"]] == [two["id"]]
            assert (await api.get("/v1/recordings?state=failed")).json()["items"] == []
            first = (await api.get("/v1/recordings?limit=1")).json()
            assert [item["id"] for item in first["items"]] == [two["id"]]
            rest = (await api.get("/v1/recordings?limit=1&cursor=" + first["next_cursor"])).json()
            assert [item["id"] for item in rest["items"]] == [one["id"]]
            assert rest["next_cursor"] is None
            assert (await api.get("/v1/recordings?state=bogus")).status_code == 422
            assert (await api.get("/v1/recordings?cursor=nonsense")).status_code == 422
            polled = (await api.get("/v1/recordings/" + one["id"])).json()
            assert polled["state"] == "succeeded" and polled["summary_state"] == "succeeded"
            assert polled["summary_finished_at"] and polled["summary_attempts"] == 1
            assert polled["note_id"] and polled["title"] == "Standup"


async def test_a_merged_recording_lists_the_note_it_now_lives_in(store):
    from services.store.tests.test_transcription_call import Service
    from tests.integration.test_recording_writeup import BOUNDARY_BLOCK
    from tests.integration.test_recordings import request, setup_transcription, store_audio

    async with Service() as service:
        context = await setup_transcription(store, service)

        async def write_text(connection, key, limits, system, evidence, **kwargs):
            return (
                "# " + evidence["resolved_title"] + "\n\n## Top line\nDone.\n" + BOUNDARY_BLOCK
            ), {}

        context.provider.write_text = write_text
        accepted = await context.accept_recording(request(await store_audio(store)))
        await context.recordings.tick()
        job = context.recordings.load(accepted["id"])
        target = await a_note(store, "Existing meeting")
        context.write(
            "recording-merges/" + job.source_id, {"state": "complete", "target_id": target.id}
        )
        single = await context.recording_status({"id": job.id})
        assert single["note_id"] == target.id != job.note_id
        async with await api_for(store) as api:
            listed = (await api.get("/v1/recordings")).json()["items"][0]
            polled = (await api.get("/v1/recordings/" + job.id)).json()
        assert listed["note_id"] == polled["note_id"] == target.id
        assert listed["note_path"] == polled["note_path"] == single["note_path"] != ""
