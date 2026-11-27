import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from coppermind_store import review_notes
from coppermind_store.review_notes import STATE, ReviewNotes

from coppermind.schema import default_schema

JOB = "a" * 64
NOTE = "01HZ" + "0" * 22
OLD = "# Old\n\nBody\n\n## References\n\n- Source transcript: [[Sources/call]]\n"


class Memory:
    def __init__(self, tmp_path):
        self.root = tmp_path
        self.data: dict[str, Any] = {}
        self.recordings: Any = None
        self.store: Any = None
        self.in_review_folder: Any = None

    def read(self, name, default):
        return self.data.get(name, default)

    def write(self, name, value):
        self.data[name] = value

    def jobs(self):
        return []

    def config(self):
        enrichment = SimpleNamespace(attendees_field="", event_field="")
        return SimpleNamespace(assignments={"enrichment": enrichment})


def build(tmp_path, *, body=OLD, baseline=OLD, mode="markdown-v1"):
    memory = Memory(tmp_path)
    memory.data["recording-writeups/" + JOB] = {
        "body": "old generated",
        "filed_body": baseline,
        "transcript": {"segments": [{"speaker": "Unknown voice 1", "text": "hi"}]},
        "voice_map": {"SPEAKER_1": "Unknown voice 1"},
    }
    memory.data["voice-mappings/" + JOB] = {"SPEAKER_1": "Dana"}
    job: Any = SimpleNamespace(
        id=JOB,
        note_id=NOTE,
        source_id="s",
        generation_mode=mode,
        state="succeeded",
        boundaries=[],
        captured_at="2026-11-02T10:00:00+00:00",
        succeeded_at="2026-11-02T11:00:00+00:00",
        model_copy=lambda update: SimpleNamespace(**{**job.__dict__, **update}),
    )
    note = SimpleNamespace(id=NOTE, body=body, frontmatter={"title": "T"}, content_hash="sha256:1")
    replaced: list[Any] = []

    async def replace_note(note_id, request, if_match):
        replaced.append((request.body, if_match))
        note.body, note.frontmatter = request.body, dict(request.frontmatter)
        note.content_hash = f"sha256:{len(replaced) + 1}"

    async def references(job, fresh, text):
        return (
            text
            + "\n\n## References\n\n- Source transcript: <<coppermind-source-transcript-link>>\n"
        )

    memory.recordings = SimpleNamespace(
        load=lambda _: job,
        save=lambda _: None,
        with_references=references,
        jobs=lambda: [job],
        summary_of=lambda *_: {
            "summary_state": "succeeded",
            "summary_error": "",
            "summary_finished_at": "",
        },
    )
    memory.in_review_folder = lambda path: path.startswith("Review/")
    note.path = "Review/Call.md"
    memory.store = SimpleNamespace(
        control=SimpleNamespace(schema=default_schema),
        entities=SimpleNamespace(load=lambda: SimpleNamespace(entities={})),
        get_note=lambda _: asyncio.sleep(0, note),
        get_source=lambda _: asyncio.sleep(0, SimpleNamespace(projection_path="Sources/call.md")),
        replace_note=replace_note,
    )
    return ReviewNotes(memory), memory, job, note, replaced


def fake_generate(monkeypatch, body="# New\n\nBetter body", fails=None, answers=()):
    calls: list[Any] = []

    async def generate(context, job, transcript, voice_map, model=""):
        calls.append((job.id, transcript, voice_map, model))
        if fails:
            raise fails
        return {
            "body": body,
            "boundaries": [],
            "suggestions": list(answers),
            "transcript": transcript,
        }

    monkeypatch.setattr("coppermind_store.recording_writeup.generate", generate)
    return calls


@pytest.mark.asyncio
async def test_an_unedited_note_is_replaced_and_the_old_version_is_kept(tmp_path, monkeypatch):
    reviewer, memory, job, note, replaced = build(tmp_path)
    calls = fake_generate(monkeypatch)
    outcome = await reviewer.write_again(NOTE, JOB)
    assert outcome["state"] == "done"
    assert replaced[0][1] == "sha256:1"
    saved = memory.data["recording-writeups/" + JOB]
    assert saved["earlier_bodies"][0]["body"] == OLD
    assert saved["filed_body"] == replaced[0][0]
    # The name given to the voice since the first write-up is what the rewrite is given.
    assert calls[0][0] == JOB + "-again"
    assert calls[0][1]["segments"][0]["speaker"] == "Dana"


@pytest.mark.asyncio
async def test_a_note_edited_by_hand_is_replaced_only_when_the_owner_said_yes(
    tmp_path, monkeypatch
):
    reviewer, memory, job, note, replaced = build(tmp_path, body=OLD + "\nMy own line\n")
    fake_generate(monkeypatch)
    outcome = await reviewer.write_again(NOTE, JOB)
    assert outcome["state"] == "failed" and outcome["error"] == review_notes.EDITED
    assert replaced == []
    assert reviewer.edited(note, job) is True
    outcome = await reviewer.write_again(NOTE, JOB, overwrite=True)
    assert outcome["state"] == "done"
    assert len(replaced) == 1
    # What was replaced stays in the history.
    assert memory.data["recording-writeups/" + JOB]["earlier_bodies"][0]["body"].endswith(
        "My own line\n"
    )


@pytest.mark.asyncio
async def test_a_timestamp_link_in_the_new_write_up_points_at_the_transcript_page(
    tmp_path, monkeypatch
):
    from coppermind.recording_writeup import SOURCE_TARGET_TOKEN

    reviewer, memory, job, note, replaced = build(tmp_path)
    fake_generate(monkeypatch, body=f"# New\n\nSaid it ([[{SOURCE_TARGET_TOKEN}|00:00:05]])\n")
    await reviewer.write_again(NOTE, JOB)
    assert "[[Sources/call|00:00:05]]" in replaced[0][0]
    assert "<<coppermind" not in replaced[0][0]


@pytest.mark.asyncio
async def test_one_run_can_use_another_model_and_only_that_run(tmp_path, monkeypatch):
    reviewer, memory, job, note, replaced = build(tmp_path)
    calls = fake_generate(monkeypatch)
    await reviewer.write_again(NOTE, JOB, "claude-opus-5-5")
    await reviewer.write_again(NOTE, JOB)
    assert [call[3] for call in calls] == ["claude-opus-5-5", ""]


@pytest.mark.asyncio
async def test_resummarize_passes_the_chosen_model_and_the_confirm_to_the_rewrite(
    tmp_path, monkeypatch
):
    reviewer, memory, job, note, replaced = build(tmp_path)
    seen: list[Any] = []

    async def rewrite(note_id, recording_id, model="", overwrite=False):
        seen.append((note_id, model, overwrite))

    monkeypatch.setattr(reviewer, "rewrite", rewrite)
    monkeypatch.setattr(reviewer, "writeup_ready", lambda: True)

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def get(self, model, identity):
            return SimpleNamespace(id=identity, state="ok", reviewed=False)

    memory.store.session_factory = Session
    await reviewer.resummarize({"id": NOTE, "model": " gpt-x ", "overwrite": True})
    await asyncio.gather(*reviewer.live.values())
    assert seen == [(NOTE, "gpt-x", True)]
    with pytest.raises(ValueError, match="not a model"):
        await reviewer.resummarize({"id": NOTE, "model": "a" * 201})


@pytest.mark.asyncio
async def test_a_failed_rewrite_leaves_the_note_and_says_why(tmp_path, monkeypatch):
    from coppermind_store.recording_writeup import WriteupError

    reviewer, memory, job, note, replaced = build(tmp_path)
    fake_generate(monkeypatch, fails=WriteupError("writeup-invalid", "It had no heading."))
    memory.data[STATE + NOTE] = {"state": "running", "started_at": "2026-11-25T10:00:00+00:00"}
    await reviewer.rewrite(NOTE, JOB)
    record = memory.data[STATE + NOTE]
    assert record["state"] == "failed"
    assert "could not be used as a complete note" in record["error"]
    assert "No note was saved" not in record["error"]
    assert record["error"].endswith("The note is unchanged.")
    assert replaced == []
    step = reviewer.writeup_step(NOTE, job, None)
    assert step["state"] == "failed" and step["reason"] == record["error"]


@pytest.mark.asyncio
async def test_a_rewrite_that_says_what_the_note_says_changes_nothing(tmp_path, monkeypatch):
    reviewer, memory, job, note, replaced = build(tmp_path)
    fake_generate(monkeypatch)
    await reviewer.write_again(NOTE, JOB)
    note.body = replaced[0][0]
    memory.data["recording-writeups/" + JOB]["filed_body"] = replaced[0][0]
    replaced.clear()
    outcome = await reviewer.write_again(NOTE, JOB)
    assert outcome["state"] == "done" and replaced == []


def test_where_a_write_up_stands(tmp_path):
    reviewer, memory, job, _, _ = build(tmp_path)
    assert reviewer.writeup_step(NOTE, None, None)["state"] == "none"
    assert reviewer.writeup_step(NOTE, job, None) == {
        "state": "written",
        "reason": "",
        "when": job.succeeded_at,
    }
    memory.data[STATE + NOTE] = {"state": "running", "started_at": review_notes._now()}
    lost = reviewer.writeup_step(NOTE, job, None)
    assert lost["state"] == "failed" and "interrupted" in lost["reason"]
    reviewer.live[NOTE] = SimpleNamespace()
    assert reviewer.writeup_step(NOTE, job, None)["state"] == "working"
    reviewer.live.clear()
    memory.data.pop(STATE + NOTE)
    job.state = "running"
    assert reviewer.writeup_step(NOTE, job, None)["state"] == "missing"


@pytest.mark.asyncio
async def test_a_note_that_did_not_come_from_a_recording_is_refused(tmp_path):
    reviewer, memory, job, note, _ = build(tmp_path)
    memory.recordings.jobs = lambda: []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def get(self, model, identity):
            return SimpleNamespace(id=identity, state="ok", reviewed=False)

    memory.store.session_factory = Session
    with pytest.raises(ValueError, match="did not come from a recording"):
        await reviewer.resummarize({"id": NOTE})


@pytest.mark.asyncio
async def test_rewrites_run_one_at_a_time(tmp_path, monkeypatch):
    reviewer, memory, job, note, replaced = build(tmp_path)
    running = 0
    peak = 0

    async def slow(context, trial, transcript, voice_map, model=""):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return {"body": "# New\n\nBody", "boundaries": [], "transcript": transcript}

    monkeypatch.setattr("coppermind_store.recording_writeup.generate", slow)
    await asyncio.gather(*(reviewer.rewrite(NOTE, JOB) for _ in range(4)))
    assert peak == 1


def test_the_list_never_opens_the_saved_write_up_with_its_transcript(tmp_path):
    reviewer, memory, job, _, _ = build(tmp_path)
    plain_read = memory.read

    def read(name, default):
        assert not name.startswith("recording-writeups/"), "the transcript was opened"
        return plain_read(name, default)

    memory.read = read
    assert reviewer.writeup_step(NOTE, job, None)["state"] == "written"
    assert "suggestions" not in reviewer.writeup_step(NOTE, job, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["reviewed", "moved"])
async def test_a_note_reviewed_while_it_was_being_written_up_is_left_alone(
    tmp_path, monkeypatch, change
):
    reviewer, memory, job, note, replaced = build(tmp_path)
    fake_generate(monkeypatch)
    # The owner marks the note reviewed (or it leaves Review) after the rewrite started.
    if change == "reviewed":
        note.frontmatter = {**note.frontmatter, "reviewed": True}
    else:
        note.path = "Meetings/Call.md"
    memory.data[STATE + NOTE] = {"state": "running", "started_at": review_notes._now()}
    await reviewer.rewrite(NOTE, JOB)
    assert replaced == []
    record = memory.data[STATE + NOTE]
    assert record["state"] == "failed"
    assert "reviewed or moved out of Review" in record["error"]
    assert "PROPOSED" not in memory.data and not any(
        k.startswith("recording-writeup-proposed") for k in memory.data
    )
    assert reviewer.writeup_step(NOTE, job, None)["reason"] == record["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,suggestions_on,writeup_on,offered",
    [
        ("markdown-v1", True, False, False),
        ("markdown-v1", False, True, True),
        ("legacy", False, True, False),
        ("legacy", True, False, True),
    ],
)
async def test_resummarize_is_offered_only_when_the_service_that_does_it_is_on(
    tmp_path, mode, suggestions_on, writeup_on, offered
):
    reviewer, memory, job, _, _ = build(tmp_path, mode=mode)
    row = SimpleNamespace(id=NOTE, title="Call", path="Review/Call.md", date=None, reviewed=False)
    item = await reviewer.row(row, job, None, suggestions_on, writeup_on)
    assert item["can_resummarize"] is offered
    assert item["takes_model"] is (mode == "markdown-v1")


ANSWERS = (
    {"field": "account", "value": ["Acme"], "confidence": 0.9},
    {"field": "type", "value": "work", "confidence": 0.9},
    {"field": "tags", "value": ["vsphere", "brand-new-tag"], "confidence": 0.9},
)


@pytest.mark.asyncio
async def test_resummarize_fills_the_blanks_and_the_default_type(tmp_path, monkeypatch):
    reviewer, memory, job, note, replaced = build(tmp_path)
    note.frontmatter = {"type": "reference", "tags": [], "reviewed": False}
    fake_generate(monkeypatch, answers=ANSWERS)
    await reviewer.write_again(NOTE, JOB)
    assert note.frontmatter["type"] == "work"
    assert note.frontmatter["account"] == ["Acme"]
    # A tag nobody listed is accepted.
    assert note.frontmatter["tags"] == ["vsphere", "brand-new-tag"]
    assert memory.data["what-was-filled/" + NOTE]["account"] == ["Acme"]


@pytest.mark.asyncio
async def test_resummarize_never_overwrites_what_the_owner_typed(tmp_path, monkeypatch):
    reviewer, memory, job, note, replaced = build(tmp_path)
    mine = {"type": "personal", "account": ["Contoso"], "tags": ["mine"], "reviewed": False}
    note.frontmatter = dict(mine)
    fake_generate(monkeypatch, answers=ANSWERS)
    await reviewer.write_again(NOTE, JOB)
    assert note.frontmatter == mine


@pytest.mark.asyncio
async def test_resummarize_refreshes_only_what_coppermind_filled_and_the_owner_left(
    tmp_path, monkeypatch
):
    reviewer, memory, job, note, replaced = build(tmp_path)
    note.frontmatter = {"type": "work", "account": ["Acme"], "tags": ["a"], "reviewed": False}
    memory.data["what-was-filled/" + NOTE] = {
        "type": "work",
        "account": ["Acme"],
        "tags": ["a"],
    }
    # The owner changes the tags; the account and type are as Coppermind left them.
    note.frontmatter["tags"] = ["a", "mine"]
    fake_generate(
        monkeypatch,
        answers=(
            {"field": "account", "value": ["Fabrikam"], "confidence": 0.9},
            {"field": "tags", "value": ["other"], "confidence": 0.9},
        ),
    )
    await reviewer.write_again(NOTE, JOB)
    assert note.frontmatter["account"] == ["Fabrikam"]
    assert note.frontmatter["tags"] == ["a", "mine"]


@pytest.mark.asyncio
async def test_a_note_the_owner_edited_still_has_its_blanks_filled(tmp_path, monkeypatch):
    reviewer, memory, job, note, replaced = build(tmp_path, body=OLD + "\nMy own line\n")
    note.frontmatter = {"type": "reference", "tags": [], "reviewed": False}
    fake_generate(monkeypatch, answers=ANSWERS)
    outcome = await reviewer.write_again(NOTE, JOB)
    assert outcome["state"] == "failed"
    assert note.frontmatter["account"] == ["Acme"]
    assert note.body.endswith("My own line\n")


def writeup_settings(memory, model="gpt-default"):
    memory.lock = asyncio.Lock()
    limits = SimpleNamespace(
        enabled=True, paused=False, connection="main", model=model, timeout_s=60
    )
    config = SimpleNamespace(assignments={"writeup": limits, "enrichment": SimpleNamespace()})
    memory.config = lambda: config
    memory.save_config = lambda _: None
    memory.connection = lambda identity: SimpleNamespace(id=identity)
    memory.key = lambda identity: "key"


@pytest.mark.asyncio
async def test_the_model_choice_is_what_the_connection_reports_with_the_configured_one_first(
    tmp_path,
):
    reviewer, memory, *_ = build(tmp_path)
    writeup_settings(memory)

    async def models(connection, key):
        assert connection.id == "main" and key == "key"
        return ["claude-opus-5-5", "gpt-default"]

    memory.provider = SimpleNamespace(models=models)
    answer = await reviewer.models({})
    assert answer == {
        "default": "gpt-default",
        "models": ["claude-opus-5-5", "gpt-default"],
        "problem": "",
    }

    async def silent(connection, key):
        return ["other"]

    memory.provider = SimpleNamespace(models=silent)
    # The configured model is offered even when the server does not list it.
    assert (await reviewer.models({}))["models"] == ["gpt-default", "other"]


@pytest.mark.asyncio
async def test_a_server_that_will_not_list_its_models_still_offers_the_configured_one(tmp_path):
    from coppermind_store.model_provider import ProviderError

    reviewer, memory, *_ = build(tmp_path)
    writeup_settings(memory)

    async def refuse(connection, key):
        raise ProviderError("down", "provider-connect")

    memory.provider = SimpleNamespace(models=refuse)
    answer = await reviewer.models({})
    assert answer["models"] == ["gpt-default"] and answer["default"] == "gpt-default"
    assert answer["problem"] == "The AI server did not list its models."


MEETINGS = [
    {
        "event_id": "a",
        "title": "Pipeline review",
        "start": "2026-11-02T09:45:00+00:00",
        "end": "2026-11-02T10:30:00+00:00",
        "people": [],
    },
    {
        "event_id": "b",
        "title": "Northwind check-in",
        "start": "2026-11-02T14:00:00+00:00",
        "end": "2026-11-02T15:00:00+00:00",
        "people": [],
    },
]


def calendar(memory, note, patched, renamed):
    async def candidates(_):
        return MEETINGS

    async def patch(note_id, request, if_match):
        patched.append(request.set)
        note.frontmatter = {**note.frontmatter, **request.set}
        return note

    async def rename(note_id, request, if_match):
        renamed.append(request.title)
        note.title = request.title

    memory.calendar_candidates = candidates
    memory.store.patch_frontmatter = patch
    memory.store.rename_note = rename


@pytest.mark.asyncio
async def test_choosing_another_meeting_names_it_and_the_title_follows_while_unchanged(tmp_path):
    reviewer, memory, job, note, _ = build(tmp_path)
    note.title = "2026-11-02 Pipeline review"
    note.frontmatter = {"calendar_event": "a", "reviewed": False}
    patched: list[Any] = []
    renamed: list[str] = []
    calendar(memory, note, patched, renamed)
    await reviewer.set_meeting({"id": NOTE, "event_id": "b"})
    assert patched[0]["calendar_event"] == "b"
    assert renamed == ["2026-11-02 Northwind check-in"]
    with pytest.raises(ValueError, match="not on the calendar"):
        await reviewer.set_meeting({"id": NOTE, "event_id": "zzz"})


@pytest.mark.asyncio
async def test_a_title_that_cannot_be_renamed_still_leaves_the_meeting_saved(tmp_path):
    from coppermind.store_protocol import PathCollision

    reviewer, memory, job, note, _ = build(tmp_path)
    note.title = "2026-11-02 Pipeline review"
    note.frontmatter = {"calendar_event": "a", "reviewed": False}
    patched: list[Any] = []
    calendar(memory, note, patched, [])

    async def collide(note_id, request, if_match):
        raise PathCollision("Review/2026-11-02 Northwind check-in.md")

    memory.store.rename_note = collide
    assert await reviewer.set_meeting({"id": NOTE, "event_id": "b"}) == {"meeting": "b"}
    assert patched[0]["calendar_event"] == "b" and note.title == "2026-11-02 Pipeline review"


@pytest.mark.asyncio
async def test_a_title_the_owner_changed_is_left_when_the_meeting_changes(tmp_path):
    reviewer, memory, job, note, _ = build(tmp_path)
    note.title = "2026-11-02 My own words"
    note.frontmatter = {"calendar_event": "a", "reviewed": False}
    patched: list[Any] = []
    renamed: list[str] = []
    calendar(memory, note, patched, renamed)
    await reviewer.set_meeting({"id": NOTE, "event_id": "b"})
    assert patched[0]["calendar_event"] == "b" and renamed == []


@pytest.mark.asyncio
async def test_a_reviewed_note_cannot_have_its_meeting_changed(tmp_path):
    reviewer, memory, job, note, _ = build(tmp_path)
    note.frontmatter = {"reviewed": True}
    calendar(memory, note, [], [])
    with pytest.raises(ValueError, match="not waiting for review"):
        await reviewer.set_meeting({"id": NOTE, "event_id": "b"})


@pytest.mark.asyncio
async def test_the_meetings_of_the_day_are_listed_nearest_the_recording_first(tmp_path):
    reviewer, memory, job, note, _ = build(tmp_path)
    note.frontmatter = {"calendar_event": "b"}
    memory.calendar_candidates = lambda _: asyncio.sleep(0, list(reversed(MEETINGS)))
    answer = await reviewer.meeting(note, job)
    assert answer["selected"] == "b"
    assert [e["event_id"] for e in answer["candidates"]] == ["a", "b"]


@pytest.mark.asyncio
async def test_undo_puts_a_filed_note_back_in_review_and_clears_the_earlier_filing(tmp_path):
    reviewer, memory, job, note, _ = build(tmp_path)
    note.path = "work/2026/2026-11/Call.md"
    note.frontmatter = {"reviewed": True}
    state = tmp_path / "state"
    for kind in ("filing", "reviews"):
        (state / kind).mkdir(parents=True)
        (state / kind / f"{NOTE}.json").write_text("{}")
    moved: list[str] = []

    async def patch(note_id, request, if_match):
        note.frontmatter = {**note.frontmatter, **request.set}
        return note

    async def move(note_id, request, if_match):
        moved.append(request.target_folder)
        note.path = "Review/Call.md"
        return note

    memory.store.patch_frontmatter = patch
    memory.store.move_note = move
    memory.store.control.settings = lambda: SimpleNamespace(
        notes=SimpleNamespace(review_folder="Review")
    )
    memory.store.control.store = SimpleNamespace(state_dir=state)
    answer = await reviewer.reopen({"id": NOTE})
    assert answer == {"reopened": True, "path": "Review/Call.md"}
    assert note.frontmatter["reviewed"] is False and moved == ["Review"]
    assert not list(state.glob("*/*.json"))


def test_the_choices_name_what_a_card_writes_and_what_it_offers(tmp_path):
    reviewer, memory, *_ = build(tmp_path)
    choices = reviewer.choices()
    assert choices["keys"] == {
        "type": "type",
        "account": "account",
        "tags": "tags",
        "reviewed": "reviewed",
        "meeting": "calendar_event",
        "attendees": "attendees",
    }
    assert {"work", "personal", "reference"} <= set(choices["types"])
    assert choices["accounts"] == [] and choices["tags_closed"] in {True, False}


@pytest.mark.asyncio
async def test_a_card_from_a_recording_says_whether_the_body_was_edited_and_offers_the_day(
    tmp_path,
):
    reviewer, memory, job, note, _ = build(tmp_path)
    note.frontmatter = {"calendar_event": "b"}
    memory.calendar_candidates = lambda _: asyncio.sleep(0, list(MEETINGS))
    row = SimpleNamespace(id=NOTE, title="Call", path="Review/Call.md", date=None, reviewed=False)
    item = await reviewer.row(row, job, None, suggestions_on=False, writeup_on=True)
    assert item["edited"] is False and item["can_resummarize"] is True
    assert item["meeting"]["selected"] == "b" and len(item["meeting"]["candidates"]) == 2
    note.body = OLD + "\nMine\n"
    assert (await reviewer.row(row, job, None, False, True))["edited"] is True
    # A note that did not come from a recording has neither.
    plain = await reviewer.row(row, None, None, False, True)
    assert "edited" not in plain and "meeting" not in plain
