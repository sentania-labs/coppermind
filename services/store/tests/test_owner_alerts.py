"""What tells the owner a recording failed or a note has no summary."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from coppermind_store import owner_alerts
from coppermind_store.ai_services import Refused
from coppermind_store.owner_alerts import OwnerAlerts
from coppermind_store.recordings import Recordings, TranscriptionJob

NOW = datetime.now(UTC)


def stamp(**ago: float) -> str:
    return (NOW - timedelta(**ago)).isoformat()


def job(i: int, **changes: Any) -> TranscriptionJob:
    values: dict[str, Any] = dict(
        id=f"{i:064x}",
        artifact_id="a",
        filename="call.wav",
        mime_type="audio/wav",
        size_bytes=1,
        title=f"Call {i}",
        provider="plaud",
        external_source_id=f"ext-{i}",
        created_at=stamp(minutes=30),
    )
    values.update(changes)
    return TranscriptionJob(**values)


class Fixture:
    def __init__(self, tmp_path, jobs: list[TranscriptionJob]) -> None:
        self.saved: dict[str, Any] = {}
        self.jobs = jobs
        self.notes: dict[str, SimpleNamespace] = {}
        self.summaries: dict[str, dict[str, Any]] = {}
        self.root = tmp_path
        self.asked: list[str] = []
        settings = SimpleNamespace(
            notes=SimpleNamespace(review_folder="Review"),
            general=SimpleNamespace(timezone="America/Chicago"),
        )
        schema = SimpleNamespace(role=lambda _: "reviewed")

        async def get_note(identity: str) -> SimpleNamespace:
            return self.notes[identity]

        store = SimpleNamespace(
            notes_root=tmp_path,
            get_note=get_note,
            control=SimpleNamespace(settings=lambda: settings, schema=lambda: schema),
            admin=SimpleNamespace(
                notes=SimpleNamespace(
                    summary_service_state=lambda: "on",
                    summary_again=self.summary_again,
                )
            ),
        )
        self.context = SimpleNamespace(
            store=store,
            read=lambda name, default: self.saved.get(name, default),
            write=lambda name, value: self.saved.__setitem__(name, json.loads(json.dumps(value))),
            jobs=list,
            config=lambda: SimpleNamespace(
                assignments={"enrichment": SimpleNamespace(enabled=True)}
            ),
        )
        self.recordings = Recordings(self.context)
        self.recordings.jobs = lambda: list(self.jobs)
        self.recordings.assignment = lambda: SimpleNamespace(
            enabled=True, paused=False, connection="c", model="m"
        )
        self.recordings.latest_enrichment = lambda: {}
        self.recordings.summary_of = lambda item, latest: self.summaries.get(
            item.id, {"summary_state": "succeeded", "summary_finished_at": ""}
        )
        self.context.recordings = self.recordings
        self.alerts = OwnerAlerts(self.context)

    async def summary_again(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.asked.append(payload["id"])
        extra = vars(self.notes[payload["id"]])
        if extra.get("busy"):
            return {"title": "x", "already": True}
        if extra.get("refuse"):
            raise Refused("note", extra["refuse"])
        return {"title": "x", "already": False}

    def note(self, identity: str, title: str, **extra: Any) -> None:
        self.notes[identity] = SimpleNamespace(
            id=identity,
            title=title,
            path=f"Review/{title}.md",
            frontmatter={"reviewed": extra.pop("reviewed", False)},
            **extra,
        )

    def summary_failed(self, item: TranscriptionJob, note: str, when: str = "") -> None:
        self.note(note, item.title)
        item.state, item.note_id, item.source_id = "succeeded", note, "src-" + note
        self.summaries[item.id] = {
            "summary_state": "failed",
            "summary_error": "The AI service did not answer.",
            "summary_error_rule": "provider-unreachable",
            "summary_finished_at": when or stamp(minutes=5),
        }


def failed(i: int, **changes: Any) -> TranscriptionJob:
    values: dict[str, Any] = dict(
        state="failed",
        attempts=2,
        attempt_limit=2,
        error_rule="writeup-refused",
        stage="Writing up",
        failed_at=stamp(minutes=10),
    )
    values.update(changes)
    return job(i, **values)


def test_an_address_is_checked_and_shown_without_its_secret_path():
    assert owner_alerts.check_address("  https://hooks.example/a/secret?t=1 ") == (
        "https://hooks.example/a/secret?t=1"
    )
    assert owner_alerts.check_address("") == ""
    for bad in ("ftp://x/y", "hooks.example/x", "https://user:pw@hooks.example/x"):
        with pytest.raises(ValueError):
            owner_alerts.check_address(bad)
    assert owner_alerts.shown_address("https://hooks.example:8443/a/secret?t=1") == (
        "https://hooks.example:8443"
    )


@pytest.mark.asyncio
async def test_setup_keeps_the_address_private_and_ignores_history(tmp_path):
    fixture = Fixture(tmp_path, [])
    view = await fixture.alerts.view({})
    assert view["on"] is False and view["stuck_hours"] == 6
    result = await fixture.alerts.save(
        {"url": "https://hooks.example/a/secret", "stuck_hours": "3"}
    )
    assert result == {
        "on": True,
        "where": "https://hooks.example",
        "stuck_hours": 3,
        "recent": [],
    }
    assert "secret" not in json.dumps(result)
    assert fixture.saved["attention-webhook"]["since"]
    with pytest.raises(Refused) as refusal:
        await fixture.alerts.save({"url": "nope"})
    assert refusal.value.field == "url"
    with pytest.raises(Refused):
        await fixture.alerts.save({"stuck_hours": "500"})
    assert (await fixture.alerts.save({"url": ""}))["on"] is False
    assert fixture.saved["attention-webhook"]["since"] == ""


@pytest.mark.asyncio
async def test_notes_without_a_summary_are_listed_once_and_only_while_the_owner_can_act(tmp_path):
    jobs = [job(1), job(2), job(3), job(4), job(5, state="failed", attempts=2)]
    fixture = Fixture(tmp_path, jobs)
    fixture.summary_failed(jobs[0], "n1")
    fixture.summary_failed(jobs[1], "n2", when=stamp(minutes=1))
    fixture.summary_failed(jobs[2], "n3")
    fixture.notes["n3"].frontmatter["reviewed"] = True  # theirs now
    jobs[3].state = "succeeded"  # summary written
    found = await fixture.recordings.missing_summaries()
    assert {item["id"] for item in found} == {"n1", "n2"}
    assert all(item["can_retry"] and "press Resummarize" in item["message"] for item in found)
    assert found[0]["path"].startswith("Review/")
    waiting = await fixture.recordings.waiting()
    assert {i["id"] for i in waiting["missing_summaries"]} == {"n1", "n2"}
    counts = await fixture.alerts.counts()
    assert counts == {"recordings_failed": 1, "notes_without_summary": 2}


@pytest.mark.asyncio
async def test_a_summary_service_that_is_off_says_so_and_offers_no_retry(tmp_path):
    jobs = [job(1)]
    fixture = Fixture(tmp_path, jobs)
    fixture.summary_failed(jobs[0], "n1")
    fixture.context.store.admin.notes.summary_service_state = lambda: "none"
    found = await fixture.recordings.missing_summaries()
    assert found[0]["can_retry"] is False
    assert "Turn suggestions on" in found[0]["message"]


@pytest.mark.asyncio
async def test_writing_up_every_note_without_a_summary_touches_no_note(tmp_path):
    jobs = [job(1), job(2), job(3)]
    fixture = Fixture(tmp_path, jobs)
    for i, item in enumerate(jobs, 1):
        fixture.summary_failed(item, f"n{i}")
    fixture.notes["n2"].busy = True
    fixture.notes["n3"].refuse = "This note has already been reviewed."
    before = {k: dict(v.frontmatter) for k, v in fixture.notes.items()}
    result = await fixture.alerts.write_up_missing({})
    assert result == {"queued": 1, "already": 1, "skipped": 1}
    assert sorted(fixture.asked) == ["n1", "n2", "n3"]
    assert {k: dict(v.frontmatter) for k, v in fixture.notes.items()} == before


@pytest.mark.asyncio
async def test_writing_up_all_says_why_when_the_service_is_not_on(tmp_path):
    jobs = [job(1)]
    fixture = Fixture(tmp_path, jobs)
    fixture.summary_failed(jobs[0], "n1")
    fixture.context.store.admin.notes.summary_service_state = lambda: "none"
    fixture.notes["n1"].refuse = "Suggestions are off."
    with pytest.raises(ValueError, match="Suggestions are off"):
        await fixture.alerts.write_up_missing({})


@pytest.mark.asyncio
async def test_nothing_to_write_up_is_a_calm_zero(tmp_path):
    fixture = Fixture(tmp_path, [job(1)])
    assert await fixture.alerts.write_up_missing({}) == {
        "queued": 0,
        "already": 0,
        "skipped": 0,
    }


@pytest.mark.asyncio
async def test_the_problems_note_names_what_failed_and_goes_away_when_it_is_fixed(tmp_path):
    jobs = [failed(1), job(2), job(3, state="running", started_at=stamp(hours=9))]
    fixture = Fixture(tmp_path, jobs)
    fixture.summary_failed(jobs[1], "n1")
    await fixture.alerts.keep_problems_note()
    path = tmp_path / "Review" / "Needs attention.md"
    text = path.read_text()
    assert text.startswith("---\ncoppermind-generated: needs-attention\n---\n")
    assert "## Recordings that failed (1)" in text and "### Call 1" in text
    assert "## Recordings that are taking too long (1)" in text and "### Call 3" in text
    assert "## Notes without a summary (1)" in text and "[[Review/Call 2|Call 2]]" in text
    assert "What failed:" in text and "Why and what to do:" in text
    # Nothing changed, so nothing is rewritten and the phone does not sync it again.
    first = path.stat().st_mtime_ns
    await fixture.alerts.keep_problems_note()
    assert path.stat().st_mtime_ns == first
    # The recording is retried and the summary written: both leave the note, and then the note goes.
    jobs[0].state = "queued"
    jobs[0].attempts = 0
    fixture.summaries.clear()
    jobs[2].started_at = stamp(minutes=2)
    await fixture.alerts.keep_problems_note()
    assert not path.exists()


@pytest.mark.asyncio
async def test_a_note_that_is_not_coppermind_s_is_left_alone(tmp_path):
    fixture = Fixture(tmp_path, [failed(1)])
    path = tmp_path / "Review" / "Needs attention.md"
    path.parent.mkdir()
    path.write_text("my own thoughts\n")
    await fixture.alerts.keep_problems_note()
    assert path.read_text() == "my own thoughts\n"
    fixture.jobs.clear()
    await fixture.alerts.keep_problems_note()
    assert path.read_text() == "my own thoughts\n"


@pytest.mark.asyncio
async def test_a_dismissed_failure_leaves_the_note(tmp_path):
    jobs = [failed(1)]
    fixture = Fixture(tmp_path, jobs)
    await fixture.alerts.keep_problems_note()
    assert (tmp_path / "Review" / "Needs attention.md").exists()
    jobs[0].dismissed = True
    await fixture.alerts.keep_problems_note()
    assert not (tmp_path / "Review" / "Needs attention.md").exists()


def webhook(fixture: Fixture, monkeypatch, answers: list[int]) -> list[dict[str, Any]]:
    """Point the webhook at a stand-in receiver that answers with each status in turn."""
    received: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        received.append(json.loads(request.content))
        return httpx.Response(answers.pop(0) if answers else 200)

    real = httpx.AsyncClient
    monkeypatch.setattr(
        owner_alerts.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(respond), **kw),
    )
    fixture.saved["attention-webhook"] = {
        "url": "https://hooks.example/x",
        "stuck_hours": 6,
        "since": stamp(hours=1),
    }
    return received


@pytest.mark.asyncio
async def test_a_final_failure_is_sent_once_with_its_provider_and_external_id(
    tmp_path, monkeypatch
):
    jobs = [failed(1), failed(2, attempts=1, error_rule="writeup-transient", retry_after=stamp())]
    fixture = Fixture(tmp_path, jobs)
    received = webhook(fixture, monkeypatch, [])
    await fixture.alerts.send_events()
    await fixture.alerts.send_events()
    assert len(received) == 1
    event = received[0]
    assert event["event"] == "recording.failed"
    assert event["recording"]["provider"] == "plaud"
    assert event["recording"]["external_source_id"] == "ext-1"
    assert event["recording"]["stage"] == "Writing up"
    assert event["error_rule"] == "writeup-refused" and event["error"]
    assert (await fixture.alerts.view({}))["recent"][0]["state"] == "sent"
    # The same recording failing again after a retry is a new final failure, so a new event.
    jobs[0].failed_at = stamp(minutes=1)
    await fixture.alerts.send_events()
    assert len(received) == 2


@pytest.mark.asyncio
async def test_a_problems_note_that_cannot_be_written_does_not_stop_the_webhook(
    tmp_path, monkeypatch
):
    fixture = Fixture(tmp_path, [failed(1)])
    received = webhook(fixture, monkeypatch, [])
    fixture.context.store.control.settings().notes.review_folder = "///"
    await fixture.alerts.tick()
    assert len(received) == 1


@pytest.mark.asyncio
async def test_nothing_is_sent_without_an_address_or_for_failures_before_it_was_saved(
    tmp_path, monkeypatch
):
    fixture = Fixture(tmp_path, [failed(1, failed_at=stamp(hours=3))])
    received = webhook(fixture, monkeypatch, [])
    await fixture.alerts.send_events()
    assert received == []
    fixture.saved["attention-webhook"]["url"] = ""
    fixture.jobs[0].failed_at = stamp(minutes=1)
    await fixture.alerts.send_events()
    assert received == []


@pytest.mark.asyncio
async def test_a_summary_that_failed_for_good_and_a_stuck_recording_each_get_an_event(
    tmp_path, monkeypatch
):
    jobs = [job(1), job(2, state="queued", created_at=stamp(hours=8))]
    fixture = Fixture(tmp_path, jobs)
    fixture.summary_failed(jobs[0], "n1")
    received = webhook(fixture, monkeypatch, [])
    fixture.saved["attention-webhook"]["since"] = stamp(hours=3)
    await fixture.alerts.send_events()
    assert sorted(item["event"] for item in received) == ["recording.stuck", "summary.failed"]
    summary = next(item for item in received if item["event"] == "summary.failed")
    assert summary["recording"]["provider"] == "plaud" and summary["note"]["id"] == "n1"
    stuck = next(item for item in received if item["event"] == "recording.stuck")
    assert stuck["stuck_hours"] == 6 and stuck["recording"]["external_source_id"] == "ext-2"
    await fixture.alerts.send_events()
    assert len(received) == 2


@pytest.mark.asyncio
async def test_a_receiver_that_does_not_answer_is_tried_again_later_and_then_given_up_on(
    tmp_path, monkeypatch
):
    fixture = Fixture(tmp_path, [failed(1)])
    received = webhook(fixture, monkeypatch, [500] * 10)
    await fixture.alerts.send_events()
    assert len(received) == 1
    state = next(iter(fixture.saved["attention-events"].values()))
    assert state["state"] == "pending" and state["tries"] == 1
    await fixture.alerts.send_events()  # too soon
    assert len(received) == 1
    for _ in range(owner_alerts.TRIES):
        key = next(iter(fixture.saved["attention-events"]))
        fixture.saved["attention-events"][key]["next"] = stamp(minutes=1)
        await fixture.alerts.send_events()
    assert len(received) == owner_alerts.TRIES
    assert next(iter(fixture.saved["attention-events"].values()))["state"] == "gave-up"


@pytest.mark.asyncio
async def test_a_failed_recording_the_owner_deleted_or_dismissed_sends_nothing(
    tmp_path, monkeypatch
):
    fixture = Fixture(
        tmp_path, [failed(1, dismissed=True), failed(2, error_rule="recording-deleted")]
    )
    received = webhook(fixture, monkeypatch, [])
    await fixture.alerts.send_events()
    assert received == []


@pytest.mark.asyncio
async def test_more_failures_than_the_history_bound_are_each_sent_once(tmp_path, monkeypatch):
    jobs = [failed(i) for i in range(1, owner_alerts.EVENTS_KEPT + 6)]
    fixture = Fixture(tmp_path, jobs)
    received = webhook(fixture, monkeypatch, [])
    await fixture.alerts.send_events()
    assert len(received) == len(jobs)
    for _ in range(2):
        await fixture.alerts.send_events()
    assert len(received) == len(jobs)
    assert len({item["recording"]["id"] for item in received}) == len(jobs)


@pytest.mark.asyncio
async def test_markers_of_events_that_are_gone_are_still_cut_to_the_bound(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, [failed(1)])
    received = webhook(fixture, monkeypatch, [])
    fixture.saved["attention-events"] = {
        f"old:{i}": {
            "event": "recording.failed",
            "title": "x",
            "state": "sent",
            "at": stamp(days=2, minutes=i),
        }
        for i in range(owner_alerts.EVENTS_KEPT + 20)
    }
    await fixture.alerts.send_events()
    assert len(received) == 1
    assert len(fixture.saved["attention-events"]) == owner_alerts.EVENTS_KEPT + 1


@pytest.mark.asyncio
async def test_changing_the_review_folder_moves_the_problems_note_with_it(tmp_path):
    fixture = Fixture(tmp_path, [failed(1)])
    await fixture.alerts.keep_problems_note()
    old = tmp_path / "Review" / "Needs attention.md"
    assert old.exists()
    fixture.context.store.control.settings().notes.review_folder = "Inbox"
    await fixture.alerts.keep_problems_note()
    assert not old.exists()
    assert (tmp_path / "Inbox" / "Needs attention.md").exists()
    # The old folder holds a note of the owner's own with that name: it is never touched.
    fixture.context.store.control.settings().notes.review_folder = "Review"
    old.write_text("mine\n")
    await fixture.alerts.keep_problems_note()
    assert not (tmp_path / "Inbox" / "Needs attention.md").exists()
    assert old.read_text() == "mine\n"


@pytest.mark.asyncio
async def test_a_folder_change_while_nothing_is_wrong_leaves_nothing_behind(tmp_path):
    jobs = [failed(1)]
    fixture = Fixture(tmp_path, jobs)
    await fixture.alerts.keep_problems_note()
    jobs[0].dismissed = True
    fixture.context.store.control.settings().notes.review_folder = "Inbox"
    await fixture.alerts.keep_problems_note()
    assert not list(tmp_path.rglob("Needs attention.md"))
