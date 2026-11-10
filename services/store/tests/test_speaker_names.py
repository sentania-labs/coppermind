import pytest
from coppermind_store.speaker_names import rename_speakers, speaker_labels


def test_renaming_only_changes_matching_segment_labels():
    transcript = {
        "segments": [
            {"speaker": "Speaker 1", "text": "Speaker 1 is mentioned."},
            {"speaker": "Speaker 10", "text": "Hello"},
            {"speaker": "Speaker 1", "text": "Again"},
        ]
    }
    assert speaker_labels(transcript) == ["Speaker 1", "Speaker 10"]
    renamed = rename_speakers(transcript, {"Speaker 1": "Alex Morgan"})
    assert [s["speaker"] for s in renamed["segments"]] == [
        "Alex Morgan",
        "Speaker 10",
        "Alex Morgan",
    ]
    assert renamed["segments"][0]["text"] == "Speaker 1 is mentioned."
    assert transcript["segments"][0]["speaker"] == "Speaker 1"


async def test_calendar_candidates_only_come_from_linked_events():
    import json
    from types import SimpleNamespace

    from coppermind_store.speaker_names import evidence

    transcript = {"segments": [{"start": 0, "speaker": "Speaker 1", "text": "Hello"}]}

    class Store:
        entities = SimpleNamespace(load=lambda: SimpleNamespace(entities={}))

        async def get_source(self, source_id):
            return SimpleNamespace(current_revision=1)

        async def get_source_artifact(self, source_id, revision, name):
            return SimpleNamespace(content=json.dumps(transcript))

    class Context:
        store = Store()
        recordings = SimpleNamespace(
            jobs=lambda: [SimpleNamespace(note_id="note", source_id="src")]
        )

        def config(self):
            return SimpleNamespace(assignments={"enrichment": SimpleNamespace(event_field="event")})

        def read(self, name, default):
            return default

    note = SimpleNamespace(id="note", title="", body="", frontmatter={"event": "linked"})
    events = [
        {
            "event_id": "linked",
            "people": [{"contact_id": "c1", "name": "Alex", "address": "mailto:alex@example.test"}],
        },
        {
            "event_id": "unrelated",
            "people": [
                {"contact_id": "c2", "name": "Blair", "address": "mailto:blair@example.test"}
            ],
        },
    ]
    data = await evidence(Context(), note, events)
    assert [c["name"] for c in data["candidates"]] == ["Alex"]


def _meeting(hours: float) -> dict:
    """Two speakers trading 20-second turns for the whole recording."""
    turns = int(hours * 3600 / 20)
    return {
        "segments": [
            {
                "start": 20 * i,
                "speaker": f"Speaker {1 + i % 2}",
                "text": f"Turn {i}: " + "we reviewed the rollout plan and the budget. " * 4,
            }
            for i in range(turns)
        ]
        + [
            {"start": 20 * turns, "speaker": "Speaker 2", "text": "Thanks, Morgan."},
            {"start": 20 * turns + 1, "speaker": "Speaker 1", "text": "Taylor Reed owns it."},
        ]
    }


def _people(count: int) -> dict:
    from coppermind.entities import Entity

    people = {
        f"p{i}": Entity(id=f"p{i}", path=f"People/Person{i}.md", kind="person", name=f"Person{i} X")
        for i in range(count)
    }
    people["morgan"] = Entity(
        id="morgan", path="People/Morgan Lee.md", kind="person", name="Morgan Lee"
    )
    people["taylor"] = Entity(
        id="taylor", path="People/Taylor Reed.md", kind="person", name="Taylor Reed"
    )
    return people


def _context(transcript: dict, people: dict):
    import json
    from types import SimpleNamespace

    class Store:
        entities = SimpleNamespace(load=lambda: SimpleNamespace(entities=people))

        async def get_source(self, source_id):
            return SimpleNamespace(current_revision=1)

        async def get_source_artifact(self, source_id, revision, name):
            return SimpleNamespace(content=json.dumps(transcript))

    class Context:
        store = Store()
        recordings = SimpleNamespace(
            jobs=lambda: [SimpleNamespace(note_id="note", source_id="src")]
        )

        def config(self):
            return SimpleNamespace(assignments={"enrichment": SimpleNamespace(event_field="event")})

        def read(self, name, default):
            return default

    return Context()


@pytest.mark.parametrize("hours", [1, 3])
async def test_long_recordings_send_a_bounded_sample_and_a_bounded_people_list(hours):
    import json
    from types import SimpleNamespace

    from coppermind_store.speaker_names import MAX_CANDIDATES, SAMPLE_BUDGET, evidence

    transcript = _meeting(hours)
    note = SimpleNamespace(id="note", title="Plan", body="", frontmatter={})
    data = await evidence(_context(transcript, _people(400)), note, [])

    assert len(json.dumps(transcript)) > 3 * SAMPLE_BUDGET
    assert sum(len(line) + 1 for line in data["excerpts"]) <= SAMPLE_BUDGET
    assert "transcript" not in data
    assert data["speakers"] == ["Speaker 1", "Speaker 2"]
    for label in data["speakers"]:
        assert any(line.split("] ", 1)[1].startswith(label + ":") for line in data["excerpts"])
    assert any("Thanks, Morgan." in line for line in data["excerpts"])
    names = [c["name"] for c in data["candidates"]]
    assert len(names) <= MAX_CANDIDATES < 400
    assert names[:2] == ["Taylor Reed", "Morgan Lee"]


async def test_a_recording_too_big_for_the_budget_drops_only_the_speaker_evidence():
    from types import SimpleNamespace

    from coppermind_store.speaker_names import MIN_SAMPLE, evidence

    note = SimpleNamespace(id="note", title="", body="", frontmatter={})
    data = await evidence(_context(_meeting(1), _people(5)), note, [], MIN_SAMPLE - 1)
    assert data["omitted"] is True
    assert data["speakers"] == [] and data["candidates"] == [] and data["excerpts"] == []


def test_a_chosen_person_is_followed_through_a_rename_and_refused_once_retired():
    from datetime import UTC, datetime

    from coppermind_store.speaker_names import current_person

    people = _people(0)
    context = _context({}, people)
    stale = {
        "id": "person:morgan",
        "name": "Morgan Lee",
        "email": "",
        "path": "People/Morgan Lee.md",
    }
    people["morgan"].name = "Morgan Lee-Park"
    assert current_person(context, stale)["name"] == "Morgan Lee-Park"
    people["morgan"].retired_at = datetime.now(UTC)
    assert current_person(context, stale) is None
    contact = {"id": "contact:c1", "name": "Alex", "email": "", "path": ""}
    assert current_person(context, contact) == contact
