from types import SimpleNamespace
from typing import Any

import pytest
from coppermind_store import calendar_lookup, speaker_names
from coppermind_store.enrichment_models import Suggestion, Suggestions
from coppermind_store.note_lifecycle import NoteLifecycle

from coppermind.meeting_summary import MeetingSummary

NOTE = "01HZ" + "0" * 22
RECEIPT = {"state": "failed", "error": "The note was edited meanwhile.", "target_id": "target"}


def _context(
    calendar_answers: bool,
    suggested_against: str | None = None,
    stored: dict | None = None,
    written_for: str = "sha256:1",
):
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
        **(stored or {}),
    }

    async def get_note(_):
        return note

    async def candidates(_):
        if not calendar_answers:
            raise calendar_lookup.CalendarUnavailable("slow")
        return [{"event_id": "event"}]

    async def hash_of(_):
        if not calendar_answers:
            raise calendar_lookup.CalendarUnavailable("slow")
        return "calendar"

    # Suggestions written for this very text of the note, against the calendar it named.
    job = SimpleNamespace(
        id="job",
        note_id=NOTE,
        purpose="suggestions",
        created_at="2026-11-26T00:00:00Z",
        note_hash=written_for,
        evidence_hash=suggested_against,
        result=Suggestions(
            suggestions=[Suggestion(kind="speaker", field="Unknown voice 1", reason="a name")],
            meeting_summary=MeetingSummary(summary="Agreed the plan."),
        ),
        decisions=[],
        state="succeeded",
        error="",
        plain="",
        notice="",
        evidence={},
    )

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
        jobs=lambda: [job] if suggested_against else [],
        recordings=SimpleNamespace(jobs=lambda: [recording]),
        read=lambda name, default: state.get(name, default),
        config=lambda: SimpleNamespace(assignments={"enrichment": enrichment}),
        calendar_candidates=candidates,
        recording_calendar_candidates=candidates,
        calendar_evidence_hash=hash_of,
        note_evidence_hash=hash_of,
    )


async def _detail(
    monkeypatch,
    calendar_answers: bool,
    suggested_against: str | None = None,
    stored: dict | None = None,
    written_for: str = "sha256:1",
):
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
    context = _context(calendar_answers, suggested_against, stored, written_for)
    return await NoteLifecycle(context).detail({"note_id": NOTE})


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


@pytest.mark.asyncio
async def test_a_voice_is_not_offered_when_the_calendar_could_not_be_checked(monkeypatch):
    # The note is unchanged, so only the calendar could say the suggestion is out of date.
    detail = await _detail(monkeypatch, calendar_answers=False, suggested_against="calendar")
    assert detail["suggestions"] == [] and "stale" not in detail


@pytest.mark.asyncio
async def test_a_voice_is_offered_when_the_calendar_confirms_it(monkeypatch):
    detail = await _detail(monkeypatch, calendar_answers=True, suggested_against="calendar")
    assert [s["field"] for s in detail["suggestions"]] == ["Unknown voice 1"]


@pytest.mark.asyncio
async def test_a_voice_is_not_offered_when_the_calendar_has_changed_since(monkeypatch):
    detail = await _detail(monkeypatch, calendar_answers=True, suggested_against="older")
    assert detail["suggestions"] == [] and "stale" not in detail


@pytest.mark.asyncio
async def test_the_meeting_the_recording_was_matched_to_on_evidence_is_reported(monkeypatch):
    matched = {
        "recording-writeups/rec": {"calendar": {"matched": True, "event": {"event_id": "event"}}}
    }
    unmatched = {"recording-writeups/rec": {"calendar": {"matched": False, "reason": "none"}}}
    detail = await _detail(monkeypatch, calendar_answers=True, stored=matched)
    assert detail["recording_meeting"]["matched_event_id"] == "event"
    detail = await _detail(monkeypatch, calendar_answers=True, stored=unmatched)
    assert detail["recording_meeting"]["matched_event_id"] is None
    detail = await _detail(monkeypatch, calendar_answers=True)
    assert detail["recording_meeting"]["matched_event_id"] is None


@pytest.mark.asyncio
async def test_a_summary_is_offered_only_while_the_note_is_as_it_was_written_for(monkeypatch):
    # Marking reviewed with a summary written for an older text of the note is refused, so the
    # panel must not hold one out.
    current = await _detail(monkeypatch, calendar_answers=True, suggested_against="calendar")
    assert current["meeting_summary"]["summary"] == "Agreed the plan."
    edited = await _detail(
        monkeypatch, calendar_answers=True, suggested_against="calendar", written_for="sha256:0"
    )
    assert edited["meeting_summary"] is None and edited["suggestions"] == []
