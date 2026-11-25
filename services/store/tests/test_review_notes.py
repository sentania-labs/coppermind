import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from coppermind_store import review_notes
from coppermind_store.review_notes import STATE, ReviewNotes

JOB = "a" * 64
NOTE = "01HZ" + "0" * 22
OLD = "# Old\n\nBody\n\n## References\n\n- Source transcript: [[Sources/call]]\n"


class Memory:
    def __init__(self, tmp_path):
        self.root = tmp_path
        self.data: dict[str, Any] = {}
        self.recordings: Any = None
        self.store: Any = None

    def read(self, name, default):
        return self.data.get(name, default)

    def write(self, name, value):
        self.data[name] = value


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
    )
    memory.store = SimpleNamespace(
        get_note=lambda _: asyncio.sleep(0, note),
        get_source=lambda _: asyncio.sleep(0, SimpleNamespace(projection_path="Sources/call.md")),
        replace_note=replace_note,
    )
    return ReviewNotes(memory), memory, job, note, replaced


def fake_generate(monkeypatch, body="# New\n\nBetter body", fails=None):
    calls: list[Any] = []

    async def generate(context, job, transcript, voice_map):
        calls.append((job.id, transcript, voice_map))
        if fails:
            raise fails
        return {"body": body, "boundaries": [], "suggestions": [], "transcript": transcript}

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


def test_where_the_suggestions_stand(tmp_path):
    reviewer, memory, job, _, _ = build(tmp_path)
    working = SimpleNamespace(state="running", created_at="t", ended="")
    assert reviewer.suggestions_step(NOTE, job, working, True)["state"] == "working"
    failed = SimpleNamespace(
        state="failed",
        created_at="t",
        ended="failed",
        plain="Not answering.",
        error="",
        finished_at="f",
    )
    assert reviewer.suggestions_step(NOTE, job, failed, True) == {
        "state": "failed",
        "reason": "Not answering.",
        "when": "f",
    }
    done = SimpleNamespace(state="suggested", created_at="t", ended="succeeded", finished_at="f")
    assert reviewer.suggestions_step(NOTE, job, done, True)["state"] == "written"
    assert reviewer.suggestions_step(NOTE, job, None, True)["state"] == "missing"
    assert reviewer.suggestions_step(NOTE, job, None, False)["state"] == "off"


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
    memory.data["recording-writeup-status/" + JOB] = {
        "suggestions": False,
        "suggestion_error": "The write-up did not include field suggestions.",
    }
    plain_read = memory.read

    def read(name, default):
        assert not name.startswith("recording-writeups/"), "the transcript was opened"
        return plain_read(name, default)

    memory.read = read
    assert reviewer.writeup_step(NOTE, job, None)["state"] == "written"
    step = reviewer.suggestions_step(NOTE, job, None, True)
    assert step["state"] == "failed" and "field suggestions" in step["reason"]
    memory.data["recording-writeup-status/" + JOB] = {"suggestions": True, "suggestion_error": ""}
    assert reviewer.suggestions_step(NOTE, job, None, True)["state"] == "written"
