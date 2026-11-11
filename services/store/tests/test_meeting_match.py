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

        async def calendar_candidates(self, note):
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
