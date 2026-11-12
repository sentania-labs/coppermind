"""The service Admin reads and acts through, against a real store."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from coppermind_store import admin_service
from coppermind_store.admin_notes import MISSING_GRACE, why_not_filed
from coppermind_store.admin_service import AdminService
from coppermind_store.ai_services import candidates
from coppermind_store.note_lifecycle import SETTLING

from coppermind.api_keys import COMPANION_PERMISSIONS, describe, load_keys, scopes_for
from coppermind.store_protocol import CreateNote
from tests.integration.test_context_enrichment import configure


async def test_the_overview_carries_what_every_page_counts_from(store):
    await configure(store)
    await store.create_note(
        CreateNote(title="Waiting", body="Words.\n", frontmatter={"date": "2026-10-14"})
    )
    view = await store.admin.act("overview", {})
    for part in ("sync", "services", "sugg", "trans", "search", "notes", "incoming", "calendar"):
        assert part in view, part
    for part in ("storage", "companion", "setup", "notices", "recent", "timezone"):
        assert part in view, part
    assert view["notes"]["waiting"] == 1
    assert view["notes"]["stuck"] == []
    assert view["sugg"]["state"] == "on"
    assert view["trans"]["state"] == "none"
    # The same number, asked a second way.
    assert len((await store.admin.act("notes-waiting", {}))["items"]) == view["notes"]["waiting"]


async def test_an_unknown_action_is_refused_by_name(store):
    with pytest.raises(KeyError):
        await store.admin.act("no-such-thing", {})


async def test_a_pairing_code_works_once_and_makes_the_companion_its_own_key(store, monkeypatch):
    monkeypatch.setattr(admin_service.asyncio, "sleep", _no_wait)
    shown = await store.admin.act("pairing-start", {})
    assert len(shown["code"]) == 6 and shown["code"].isdigit()
    # Asking again while it is still good shows the same code, not a new one.
    assert (await store.admin.act("pairing-start", {}))["code"] == shown["code"]
    wrong = "000000" if shown["code"] != "000000" else "111111"
    refused = await store.admin.act("pair-exchange", {"code": wrong, "device": "Phone"})
    assert "was not accepted" in refused["refused"]["message"]
    paired = await store.admin.act(
        "pair-exchange",
        {"code": f"{shown['code'][:3]} {shown['code'][3:]}", "device": "  My  phone "},
    )
    assert paired["name"] == "Obsidian companion on My phone"
    (record,) = [key for key in load_keys(store.control.store).keys if key.companion]
    assert record.device == "My phone"
    assert set(record.scopes) == set(scopes_for(COMPANION_PERMISSIONS))
    assert paired["credential"].startswith(f"cm_{record.key_id}_")
    # Spent: the same code does not make a second key.
    again = await store.admin.act("pair-exchange", {"code": shown["code"], "device": "Other"})
    assert "refused" in again
    state = await store.admin.act("pairing-state", {})
    assert state["code"] == ""
    assert state["done"]["device"] == "My phone"
    assert "The Obsidian companion on My phone connected." in [
        item["text"] for item in (await store.admin.act("overview", {}))["recent"]
    ]


async def test_wrong_guesses_shut_out_the_client_that_made_them_not_everyone(store, monkeypatch):
    monkeypatch.setattr(admin_service.asyncio, "sleep", _no_wait)
    monkeypatch.setattr(admin_service, "PAIRING_QUEUE_SECONDS", 10_000)
    shown = await store.admin.act("pairing-start", {})
    wrong = "000000" if shown["code"] != "000000" else "111111"

    async def guess(code, client):
        return await store.admin.act(
            "pair-exchange", {"code": code, "device": "X", "client": client}
        )

    for _ in range(admin_service.PAIRING_ATTEMPTS):
        assert "refused" in await guess(wrong, "10.0.0.66")
    # The client that guessed is refused even with the right code.
    assert "refused" in await guess(shown["code"], "10.0.0.66")
    assert [key for key in load_keys(store.control.store).keys if key.companion] == []
    # The owner's own device is not shut out by it.
    paired = await guess(shown["code"], "10.0.0.7")
    assert paired["name"] == "Obsidian companion on X"


async def test_a_pairing_code_is_spent_once_enough_wrong_guesses_arrive_from_anywhere(
    store, monkeypatch
):
    monkeypatch.setattr(admin_service.asyncio, "sleep", _no_wait)
    monkeypatch.setattr(admin_service, "PAIRING_QUEUE_SECONDS", 10_000)
    shown = await store.admin.act("pairing-start", {})
    wrong = "000000" if shown["code"] != "000000" else "111111"
    for attempt in range(admin_service.PAIRING_ATTEMPTS_IN_ALL):
        await store.admin.act(
            "pair-exchange", {"code": wrong, "device": "G", "client": f"10.1.{attempt}.1"}
        )
    late = await store.admin.act(
        "pair-exchange", {"code": shown["code"], "device": "G", "client": "10.9.9.9"}
    )
    assert "refused" in late
    assert (await store.admin.act("pairing-start", {}))["code"] != shown["code"]


async def test_a_client_that_floods_the_door_is_turned_away_not_queued(store):
    await store.admin.act("pairing-start", {})
    store.admin._pair_next["10.0.0.66"] = admin_service.time.monotonic() + 60  # noqa: SLF001
    turned_away = await store.admin.act(
        "pair-exchange", {"code": "000000", "device": "X", "client": "10.0.0.66"}
    )
    assert "Too many tries at once" in turned_away["refused"]["message"]


async def _no_wait(seconds):
    return None


async def test_a_note_just_gone_is_not_called_missing_until_time_has_passed(store):
    note = await store.create_note(
        CreateNote(title="Gone", body="Words.\n", frontmatter={"date": "2026-10-14"})
    )
    await store.admin.notes._mark_missing(note.id)  # noqa: SLF001
    from coppermind.store_protocol import ProblemInfo

    problem = ProblemInfo(
        kind="missing", reference=note.path, reason="not observed", note_id=note.id
    )
    assert await store.admin.notes.stuck([problem]) == []
    detail = await store.admin.act("note", {"id": note.id})
    assert (detail["stuck"], detail["recent"], detail["gone"]) == ("missing", True, False)

    import sqlalchemy as sa

    from coppermind.db.models import Note

    old = datetime.now(UTC) - MISSING_GRACE - timedelta(minutes=1)
    async with store.session_factory() as session, session.begin():
        await session.execute(sa.update(Note).where(Note.id == note.id).values(updated_at=old))
    (listed,) = await store.admin.notes.stuck([problem])
    assert (listed["id"], listed["kind"]) == (note.id, "missing")
    # Its owner says it is gone on purpose: it stops being a problem and nothing is deleted.
    assert (await store.admin.act("note-gone", {"id": note.id}))["title"] == "Gone"
    assert await store.admin.notes.stuck([problem]) == []
    assert (await store.admin.act("note", {"id": note.id}))["gone"] is True


def test_why_a_note_was_not_filed_is_said_in_plain_words():
    assert (
        why_not_filed("ValueError: confirm the meeting time, or select Time unknown")[0] == "time"
    )
    assert why_not_filed("ValueError: a note date is required in date")[0] == "date"
    assert why_not_filed("SourceRevisionNeedsReview")[0] == "source"
    assert why_not_filed("PermissionError")[0] == "disk"
    code, words = why_not_filed("SomethingElse")
    assert (code, words) == ("other", "Coppermind keeps trying by itself.")
    for error in ("ValueError: confirm the meeting time", "PermissionError", "Odd"):
        assert "store logs" not in why_not_filed(error)[1]
    assert SETTLING == "waiting for the reviewed file to remain stable"


def test_what_is_recalled_on_home_for_each_change():
    said = AdminService._happened  # noqa: SLF001
    assert said("suggestions-save", {"was": "none", "model": "m1"}) == (
        "Suggestions turned on, using m1."
    )
    assert said("suggestions-save", {"was": "on", "model": "m1"}) == ""
    assert said("transcription-save", {"was": "none", "service": "Home"}) == (
        "Transcription turned on, using Home."
    )
    assert said("calendar-add", {"state": "added", "name": "a.ics"}) == (
        'Calendar file "a.ics" was added.'
    )
    assert said("calendar-add", {"state": "failed", "name": "a.ics"}) == ""
    filed = {"filed": True, "path": "work/2026/2026-11/Budget.md", "name": "Budget"}
    assert said("note-keep-both", filed) == '"Budget" was filed in work/2026/2026-11.'
    assert said("note-check", filed | {"reviewed": True}) == (
        'You reviewed "Budget". Filed in work/2026/2026-11.'
    )
    assert said("note-check", {"filed": False, "settling": True}) == ""
    assert said("note-restore", {"title": "Trip"}) == 'You brought back the deleted note "Trip".'
    assert said("suggestions-save", {"refused": {"message": "no"}}) == ""


def test_an_address_is_tried_the_ways_a_person_might_have_meant_it():
    tried = candidates("192.168.1.20:8000")
    assert tried[0].startswith("http")
    assert any(item.endswith("/v1") for item in tried)
    assert all("192.168.1.20:8000" in item for item in tried)
    assert candidates("https://ai.home.example/v1") == ["https://ai.home.example/v1"]


def test_what_a_key_may_do_is_said_in_words():
    assert describe(scopes_for(COMPANION_PERMISSIONS)) == (
        "Read and change notes, hand in recordings, search"
    )
    assert describe([]) == "Nothing yet"
    assert describe(scopes_for(["read", "search"])) == "Read notes, search"


TEXT = {
    "ok": True,
    "models": [{"name": "m", "ok": True}],
    "best": "m",
}
SPEECH = {"ok": True, "models": [], "model": "w", "format": "diarized_json"}
NO_SPEECH = {"ok": False, "models": [], "reason": ""}
NO_EMBED = {"ok": False, "model": ""}


def _answers(store, monkeypatch, by_name, delay=0.0):
    async def run(connection, key):
        if delay:
            await asyncio.sleep(delay)
        return {"models": ["m"], "embed": NO_EMBED, **by_name[connection.name]}

    monkeypatch.setattr(store.context.services, "_run", run)


async def _add(store, name):
    result = await store.admin.act(
        "service-add", {"kind": "own", "address": f"http://{name}.example/v1", "name": name}
    )
    return result["service"]["id"]


async def _suggestions_connection(store):
    return store.context.config().assignments["enrichment"].connection


async def test_removing_a_service_checks_every_takeover_before_moving_any(store, monkeypatch):
    await configure(store)
    _answers(
        store,
        monkeypatch,
        {
            "Both": {"text": TEXT, "speech": SPEECH},
            "TextOnly": {"text": TEXT, "speech": NO_SPEECH},
        },
    )
    both = await _add(store, "Both")
    text_only = await _add(store, "TextOnly")
    await store.admin.act("suggestions-save", {"connection": both, "model": "m"})
    await store.admin.act("transcription-save", {"connection": both, "wait_s": 3600})

    # The second feature's new service cannot write up recordings: nothing moves.
    refused = await store.admin.act(
        "service-remove", {"id": both, "takeover": {"sugg": text_only, "trans": text_only}}
    )
    assert refused["refused"]["field"] == "rm_trans"
    assert await _suggestions_connection(store) == both
    assert both in [item["id"] for item in store.context.services.listing()]

    # A service cannot take over from itself.
    again = await store.admin.act(
        "service-remove", {"id": both, "takeover": {"sugg": both, "trans": "off"}}
    )
    assert again["refused"]["field"] == "rm_sugg"
    assert await _suggestions_connection(store) == both

    # With every target sound, both move and the service goes.
    done = await store.admin.act(
        "service-remove", {"id": both, "takeover": {"sugg": text_only, "trans": "off"}}
    )
    assert "refused" not in done
    assert await _suggestions_connection(store) == text_only
    assert both not in [item["id"] for item in store.context.services.listing()]


async def test_the_same_server_entered_twice_at_once_is_kept_once(store, monkeypatch):
    await configure(store)
    _answers(store, monkeypatch, {"Twice": {"text": TEXT, "speech": NO_SPEECH}}, delay=0.05)
    payload = {"kind": "own", "address": "http://twice.example/v1", "name": "Twice"}
    first, second = await asyncio.gather(
        store.admin.act("service-add", payload), store.admin.act("service-add", payload)
    )
    assert sorted("refused" in result for result in (first, second)) == [False, True]
    assert [s["name"] for s in store.context.services.listing()].count("Twice") == 1


def _removed_copy(store, title, age_days):
    notes = store.admin.notes
    key = notes._keep_removed(
        note_id="", title=title, path=f"{title}.md", data=b"words", kind="note"
    )
    path = notes.removed_dir / f"{key}.json"
    record = json.loads(path.read_bytes())
    record["removed_at"] = (datetime.now(UTC) - timedelta(days=age_days)).isoformat()
    path.write_text(json.dumps(record))
    return key


async def test_confirming_a_clean_up_deletes_only_what_was_listed(store, monkeypatch):
    await configure(store)
    store.sources_root.mkdir(parents=True, exist_ok=True)
    old = store.admin.storage.keep_days() + 5
    listed = _removed_copy(store, "Listed", old)
    shown = await store.admin.act("cleanup", {})
    assert [item["id"] for item in shown["items"]] == [listed]

    real = store.confirm_purge
    late = []

    async def slow(request):
        late.append(_removed_copy(store, "Aged meanwhile", old))
        return await real(request)

    monkeypatch.setattr(store, "confirm_purge", slow)
    result = await store.admin.act("cleanup-confirm", {"token": shown["token"]})
    assert result["changed"] is False and result["count"] == 1
    assert [item["key"] for item in store.admin.notes.removed()] == late
