"""A recording note whose summary failed is found, written up again, and cleared, end to end."""

import json
from typing import Any

import httpx
import sqlalchemy as sa
from coppermind_store.reconciler import reconcile_once

from coppermind.db.models import Note
from coppermind.store_protocol import ReplaceNote
from tests.integration.test_meeting_summary import PROPOSAL, setup_recording
from tests.integration.test_speaker_names import standin as model_standin

standin = model_standin


def answers_well(request: httpx.Request) -> httpx.Response:
    evidence = json.loads(json.loads(request.content)["messages"][1]["content"])
    output: dict[str, Any] = (
        PROPOSAL if "transcript" in evidence else {"suggestions": [], "unresolved": []}
    )
    return httpx.Response(
        200,
        json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(output)}}]},
    )


async def test_a_note_without_a_summary_is_counted_listed_and_cleared_without_touching_its_body(
    store, standin
):
    context, note, job, _ = await setup_recording(store, standin, failure="context")
    assert job.state == "failed"
    edited = await store.replace_note(
        note.id,
        ReplaceNote(frontmatter=note.frontmatter, body="My own meeting notes, edited.\n"),
        note.content_hash,
    )

    waiting = await context.recordings.waiting()
    (missing,) = waiting["missing_summaries"]
    assert missing["id"] == note.id and missing["path"] == edited.path
    assert missing["can_retry"] is True and "Resummarize" in missing["message"]

    counters = (await store.get_status()).counters
    assert counters.notes_without_summary == 1 and counters.recordings_failed == 0

    # The problems note says so in plain words and is never mistaken for a note of the owner's.
    await context.alerts.keep_problems_note()
    problems = store.notes_root / "Review" / "Needs attention.md"
    text = problems.read_text()
    assert "## Notes without a summary (1)" in text and "Why and what to do" in text
    kept = {path: (store.notes_root / path).read_bytes() for path in [edited.path]}
    for _ in range(2):
        counts = await reconcile_once(store, unidentified={})
        assert counts["adopted"] == 0
    async with store.session_factory() as session:
        paths = [row.path for row in (await session.scalars(sa.select(Note))).all()]
    assert paths == [edited.path]
    assert (await store.get_status()).counters.excluded_count == 1
    assert (await store.get_status()).counters.notes_awaiting_review == 1
    assert problems.exists()

    # One step writes up every note without a summary; no note body is written.
    standin["handler"] = answers_well
    result = await context.alerts.write_up_missing({})
    assert result == {"queued": 1, "already": 0, "skipped": 0}
    assert {path: (store.notes_root / path).read_bytes() for path in kept} == kept
    queued = context.newest_job(note.id)
    await context.run_job(queued)
    assert context.load_job(queued.id).state == "suggested"
    assert (await store.get_note(note.id)).body == "My own meeting notes, edited.\n"

    # The item has left every list, so the problems note goes and the counters fall to zero.
    assert (await context.recordings.waiting())["missing_summaries"] == []
    counters = (await store.get_status()).counters
    assert counters.notes_without_summary == 0 and counters.recordings_failed == 0
    await context.alerts.keep_problems_note()
    assert not problems.exists()
    assert (await reconcile_once(store, unidentified={}))["excluded"] == 0


async def test_a_reviewed_note_is_left_alone_by_write_up_every_note(store, standin):
    context, note, job, _ = await setup_recording(store, standin, failure="context")
    reviewed_key = store.control.schema().role("reviewed_key")
    await store.patch_frontmatter(
        note.id,
        __import__("coppermind.store_protocol", fromlist=["PatchFrontmatter"]).PatchFrontmatter(
            set={reviewed_key: True}
        ),
        note.content_hash,
    )
    assert (await context.recordings.waiting())["missing_summaries"] == []
    assert await context.alerts.write_up_missing({}) == {"queued": 0, "already": 0, "skipped": 0}
    assert (await store.get_status()).counters.notes_without_summary == 0
