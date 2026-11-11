from typing import Any

from coppermind_store.meeting_match import overlapping, suggest


def test_recording_only_matches_timed_events_containing_its_start():
    events = [
        {"event_id": "a", "start": "2026-11-11T09:00:00-06:00", "end": "2026-11-11T10:00:00-06:00"},
        {"event_id": "b", "start": "2026-11-11T15:15:00+00:00", "end": "2026-11-11T16:00:00+00:00"},
        {
            "event_id": "later",
            "start": "2026-11-11T10:00:00-06:00",
            "end": "2026-11-11T11:00:00-06:00",
        },
        {"event_id": "all-day", "start": "2026-11-11", "end": "2026-11-12"},
    ]
    assert [e["event_id"] for e in overlapping(events, "2026-11-11T09:30:00-06:00")] == ["a", "b"]
    assert overlapping(events, "2026-11-11T09:30:00") == []


def test_recording_starting_a_few_minutes_early_still_matches():
    events = [
        {"event_id": "a", "start": "2026-11-11T10:00:00-06:00", "end": "2026-11-11T11:00:00-06:00"}
    ]
    assert overlapping(events, "2026-11-11T09:55:00-06:00") == events
    assert overlapping(events, "2026-11-11T09:30:00-06:00") == []


async def _candidates(events, captured_at, frontmatter=None, associated=()):
    from types import SimpleNamespace

    from coppermind_store.context_service import ContextService

    seen = []

    class Fake:
        recordings = SimpleNamespace(
            jobs=lambda: [SimpleNamespace(note_id="note", source_id="src", captured_at=captured_at)]
        )

        async def calendar_candidates(self, note, descriptions=False):
            seen.append(descriptions)
            return list(events)

        def config(self):
            return SimpleNamespace(
                assignments={"enrichment": SimpleNamespace(event_field="meeting")}
            )

        def read(self, name, default):
            return {"associations": {"note": {"events": list(associated)}}}.get(name, default)

    note = SimpleNamespace(id="note", frontmatter=frontmatter or {})
    found = await ContextService.recording_calendar_candidates(Fake(), note)
    return [event["event_id"] for event in found], seen


async def test_single_event_day_keeps_every_event_as_candidate():
    timed = [
        {"event_id": "a", "start": "2026-11-11T10:00:00-06:00", "end": "2026-11-11T11:00:00-06:00"}
    ]
    assert (await _candidates(timed, "2026-11-11T09:55:00-06:00"))[0] == ["a"]
    assert (await _candidates(timed, "2026-11-11T14:00:00-06:00"))[0] == ["a"]
    all_day = [{"event_id": "d", "start": "2026-11-11", "end": "2026-11-12"}]
    assert (await _candidates(all_day, "2026-11-11T09:55:00-06:00"))[0] == ["d"]
    no_end = [{"event_id": "n", "start": "2026-11-11T10:00:00-06:00", "end": ""}]
    assert (await _candidates(no_end, "2026-11-11T10:05:00-06:00"))[0] == ["n"]
    floating = [{"event_id": "f", "start": "2026-11-11T10:00:00", "end": "2026-11-11T11:00:00"}]
    assert (await _candidates(floating, "2026-11-11T10:05:00-06:00"))[0] == ["f"]
    one_of_two = [
        *timed,
        {"event_id": "b", "start": "2026-11-11T15:00:00-06:00", "end": "2026-11-11T16:00:00-06:00"},
    ]
    assert (await _candidates(one_of_two, "2026-11-11T10:05:00-06:00"))[0] == ["a", "b"]


async def test_several_overlapping_events_narrow_the_candidates():
    events = [
        {"event_id": "a", "start": "2026-11-11T10:00:00-06:00", "end": "2026-11-11T11:00:00-06:00"},
        {"event_id": "b", "start": "2026-11-11T10:30:00-06:00", "end": "2026-11-11T11:30:00-06:00"},
        {"event_id": "c", "start": "2026-11-11T15:00:00-06:00", "end": "2026-11-11T16:00:00-06:00"},
    ]
    assert (await _candidates(events, "2026-11-11T10:40:00-06:00"))[0] == ["a", "b"]
    assert (await _candidates(events, "2026-11-11T09:55:00-06:00"))[0] == ["a", "b", "c"]
    linked = await _candidates(events, "2026-11-11T10:40:00-06:00", {"meeting": "c"})
    assert linked[0] == ["a", "b", "c"]
    associated = await _candidates(events, "2026-11-11T10:40:00-06:00", associated=["c"])
    assert associated[0] == ["a", "b", "c"]


async def test_calendar_descriptions_leave_the_store_only_when_asked(monkeypatch):
    from types import SimpleNamespace

    import coppermind_store.context_service as module
    from coppermind_store.context_service import ContextService

    catalog = SimpleNamespace(events=SimpleNamespace(page=lambda *a, **k: []))

    class Fake:
        archive = SimpleNamespace(catalog=lambda: catalog)
        store = SimpleNamespace(
            control=SimpleNamespace(
                schema=lambda: SimpleNamespace(role=lambda name: "date"),
                settings=lambda: SimpleNamespace(general=SimpleNamespace(timezone="UTC")),
            )
        )

    monkeypatch.setattr(
        module, "occurrences", lambda *a: [{"event_id": "a", "description": "Dial-in PIN 1234"}]
    )
    note = SimpleNamespace(frontmatter={"date": "2026-11-11"})
    hidden = await ContextService.calendar_candidates(Fake(), note)
    shown = await ContextService.calendar_candidates(Fake(), note, True)
    assert hidden == [{"event_id": "a"}]
    assert shown == [{"event_id": "a", "description": "Dial-in PIN 1234"}]


def test_content_match_requires_unique_corroborating_evidence():
    events = [
        {"event_id": "a", "title": "Launch planning", "people": [{"name": "Alex Morgan"}]},
        {"event_id": "b", "title": "Budget review", "people": [{"name": "Jordan Patel"}]},
    ]
    assert suggest(events, "Alex Morgan discussed launch planning.") == "a"
    assert suggest(events, "Alex Morgan spoke.") is None
    assert (
        suggest(events, "Alex Morgan and Jordan Patel discussed launch planning and budget review.")
        is None
    )


async def test_owner_choice_validates_current_candidates_and_preserves_existing_meeting():
    import asyncio
    from types import SimpleNamespace

    import pytest
    from coppermind_store.note_lifecycle import NoteLifecycle

    note = SimpleNamespace(id="note", content_hash="hash", frontmatter={}, body="")

    class Store:
        async def get_note(self, _):
            return note

        async def patch_frontmatter(self, _, patch, if_match):
            assert if_match == "hash"
            note.frontmatter.update(patch.set)
            note.content_hash = "new-hash"
            return note

    class Context:
        store = Store()
        lock = asyncio.Lock()
        recordings = SimpleNamespace(
            jobs=lambda: [SimpleNamespace(note_id="note", source_id="src", title="Recording")]
        )
        state: dict[str, Any] = {}

        def read(self, name, default):
            return self.state.get(name, default)

        def write(self, name, value):
            self.state[name] = value

        def config(self):
            return SimpleNamespace(
                assignments={"enrichment": SimpleNamespace(event_field="meeting")}
            )

        async def calendar_evidence_hash(self, _):
            return "calendar"

        async def recording_calendar_candidates(self, _):
            return [{"event_id": "event"}]

    context = Context()
    lifecycle = NoteLifecycle(context)
    payload = {
        "note_id": "note",
        "if_match": "hash",
        "calendar_hash": "calendar",
        "event_id": "other",
    }
    with pytest.raises(ValueError, match="current matching"):
        await lifecycle.choose_recording_meeting(payload)
    note.frontmatter["meeting"] = "human-choice"
    with pytest.raises(ValueError, match="different meeting"):
        await lifecycle.choose_recording_meeting({**payload, "event_id": "event"})
    note.frontmatter.clear()
    await lifecycle.choose_recording_meeting({**payload, "event_id": "event"})
    assert note.frontmatter["meeting"] == "event"
    assert context.state["associations"]["note"]["events"] == ["event"]


async def test_merge_candidates_require_same_event_and_respect_dismissal():
    from datetime import date
    from types import SimpleNamespace

    from coppermind_store.note_lifecycle import NoteLifecycle

    manual = SimpleNamespace(
        id="manual",
        title="My notes",
        path="Notes/My notes.md",
        content_hash="m",
        frontmatter={"meeting": "event"},
        sources=[],
    )
    unrelated = SimpleNamespace(
        id="other",
        title="Other",
        path="Notes/Other.md",
        content_hash="o",
        frontmatter={"meeting": "other"},
        sources=[],
    )
    generated = SimpleNamespace(
        id="generated",
        title="Generated",
        path="Review/Generated.md",
        content_hash="g",
        frontmatter={"meeting": "event"},
        sources=[],
    )
    documents = {n.id: n for n in (manual, unrelated, generated)}

    class Schema:
        def role(self, name):
            return {"date_key": "date"}.get(name, name)

    class Store:
        control = SimpleNamespace(
            schema=lambda: Schema(),
            settings=lambda: SimpleNamespace(general=SimpleNamespace(timezone="America/Chicago")),
        )

        async def list_notes(self, query):
            assert query.from_date == date(2026, 11, 11) == query.to_date
            return SimpleNamespace(items=list(documents.values()), next_cursor=None)

        async def get_note(self, identity):
            return documents[identity]

    class Context:
        store = Store()
        recordings = SimpleNamespace(jobs=lambda: [SimpleNamespace(note_id="generated")])
        state: dict[str, Any] = {}
        events: list[dict[str, str]] = []

        def in_review_folder(self, path):
            return path.startswith("Review/")

        async def recording_calendar_candidates(self, note):
            return self.events

        def read(self, name, default):
            return self.state.get(name, default)

        def config(self):
            return SimpleNamespace(
                assignments={"enrichment": SimpleNamespace(event_field="meeting")}
            )

    context = Context()
    note = SimpleNamespace(
        id="recording",
        path="Review/Recording.md",
        frontmatter={"meeting": "event", "date": "2026-11-11"},
    )
    recording = SimpleNamespace(source_id="source")
    assert [m["id"] for m in await NoteLifecycle(context).merge_candidates(note, recording)] == [
        "manual"
    ]
    context.events = [
        {
            "event_id": "event",
            "start": "2026-11-11T09:00:00-06:00",
            "end": "2026-11-11T10:00:00-06:00",
        },
        {
            "event_id": "event",
            "start": "2026-11-11T14:00:00-06:00",
            "end": "2026-11-11T15:00:00-06:00",
        },
    ]
    recording.captured_at = "2026-11-11T09:15:00-06:00"
    manual.frontmatter["date"] = "2026-11-11T14:00:00"
    assert await NoteLifecycle(context).merge_candidates(note, recording) == []
    manual.frontmatter["date"] = "2026-11-11T09:00:00"
    assert [m["id"] for m in await NoteLifecycle(context).merge_candidates(note, recording)] == [
        "manual"
    ]
    context.state["recording-merge-dismissals"] = {"source": True}
    assert await NoteLifecycle(context).merge_candidates(note, recording) == []
