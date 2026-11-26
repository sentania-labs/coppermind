from types import SimpleNamespace
from typing import Any

import pytest
from coppermind_store import calendar_lookup, speaker_names
from coppermind_store.note_lifecycle import NoteLifecycle

NOTE = "01HZ" + "0" * 22
RECEIPT = {"state": "failed", "error": "The note was edited meanwhile.", "target_id": "target"}


def _context(calendar_answers: bool):
    note = SimpleNamespace(
        id=NOTE,
        body="# Call\n",
        content_hash="sha256:1",
        path="Review/Call.md",
        frontmatter={},
        model_dump=lambda mode="json": {"id": NOTE},
    )
    recording = SimpleNamespace(
        id="rec", note_id=NOTE, source_id="src", generation_mode="markdown-v1"
    )
    state: dict[str, Any] = {
        "calendar-followups": {NOTE: {"event_id": "event"}},
        "recording-merges/src": RECEIPT,
    }

    async def get_note(_):
        return note

    async def candidates(_):
        if not calendar_answers:
            raise calendar_lookup.CalendarUnavailable("slow")
        return [{"event_id": "event"}]

    async def hash_of(_):
        return "calendar"

    schema = SimpleNamespace(role=lambda name: name, roles={})
    enrichment = SimpleNamespace(enabled=True, attendees_field="attendees")
    return SimpleNamespace(
        store=SimpleNamespace(
            get_note=get_note,
            entities=SimpleNamespace(load=lambda: SimpleNamespace(entities={})),
            control=SimpleNamespace(
                schema=lambda: schema,
                settings=lambda: SimpleNamespace(general=SimpleNamespace(timezone="UTC")),
            ),
        ),
        jobs=lambda: [],
        recordings=SimpleNamespace(jobs=lambda: [recording]),
        read=lambda name, default: state.get(name, default),
        config=lambda: SimpleNamespace(assignments={"enrichment": enrichment}),
        calendar_candidates=candidates,
        recording_calendar_candidates=candidates,
        calendar_evidence_hash=hash_of,
    )


async def _detail(monkeypatch, calendar_answers: bool):
    async def evidence(*_):
        return None

    async def no_collisions(*_):
        return []

    monkeypatch.setattr(speaker_names, "evidence", evidence)
    monkeypatch.setattr("coppermind_store.note_lifecycle.validate_review", lambda *_: None)
    monkeypatch.setattr("coppermind_store.reconciler.note_collisions", no_collisions)
    monkeypatch.setattr(
        "coppermind_store.voice_store.VoiceStore",
        lambda _: SimpleNamespace(unknown_for_note=lambda _id: []),
    )
    return await NoteLifecycle(_context(calendar_answers)).detail({"note_id": NOTE})


@pytest.mark.asyncio
async def test_a_stored_merge_failure_still_shows_while_the_calendar_is_slow(monkeypatch):
    detail = await _detail(monkeypatch, calendar_answers=False)
    assert detail["recording_merge"] == {
        "state": "failed",
        "error": RECEIPT["error"],
        "target_id": "target",
    }
    assert detail["recording_meeting"] is None
    assert detail["calendar_update"] is None
    assert detail["note"] == {"id": NOTE}


@pytest.mark.asyncio
async def test_the_meeting_and_update_come_back_when_the_calendar_answers(monkeypatch):
    detail = await _detail(monkeypatch, calendar_answers=True)
    assert detail["recording_merge"]["state"] == "failed"
    assert detail["recording_meeting"]["candidates"] == [{"event_id": "event"}]
    assert detail["calendar_update"]["candidates"] == [{"event_id": "event"}]
