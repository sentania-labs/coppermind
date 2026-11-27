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

    async def generate(context, job, transcript, voice_map):
        calls.append((job.id, transcript, voice_map))
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
async def test_a_note_edited_by_hand_is_never_overwritten(tmp_path, monkeypatch):
    reviewer, memory, job, note, replaced = build(tmp_path, body=OLD + "\nMy own line\n")
    fake_generate(monkeypatch)
    outcome = await reviewer.write_again(NOTE, JOB)
    assert outcome["state"] == "kept"
    assert replaced == []
    assert any(line.startswith("+Better body") for line in outcome["difference"])
    assert any(line.startswith("-My own line") for line in outcome["difference"])
    # Only an explicit second request puts the new write-up in place.
    memory.data[STATE + NOTE] = outcome
    row = SimpleNamespace(id=NOTE, reviewed=False)
    with pytest.raises(ValueError, match="changed"):
        await reviewer.replace(row, job, "sha256:other")
    assert replaced == []
    answer = await reviewer.replace(row, job, outcome["if_match"])
    assert answer["replaced"] is True
    assert len(replaced) == 1
    assert memory.data["recording-writeups/" + JOB]["earlier_bodies"][0]["body"].endswith(
        "My own line\n"
    )
    assert memory.data[STATE + NOTE]["state"] == "done"


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

    async def slow(context, trial, transcript, voice_map):
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
async def test_a_held_rewrite_is_not_applied_to_a_note_reviewed_since(tmp_path, monkeypatch):
    reviewer, memory, job, note, replaced = build(tmp_path, body=OLD + "\nMine\n")
    fake_generate(monkeypatch)
    outcome = await reviewer.write_again(NOTE, JOB)
    memory.data[STATE + NOTE] = outcome
    note.frontmatter = {**note.frontmatter, "reviewed": True}
    with pytest.raises(ValueError, match="reviewed or moved"):
        await reviewer.replace(SimpleNamespace(id=NOTE, reviewed=True), job, outcome["if_match"])
    assert replaced == []


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
    assert outcome["state"] == "kept"
    assert note.frontmatter["account"] == ["Acme"]
    assert note.body.endswith("My own line\n")
