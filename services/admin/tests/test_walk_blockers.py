"""What a person walking Admin found missing or misleading, held by a test each."""

from __future__ import annotations

from html.parser import HTMLParser
from types import SimpleNamespace

from coppermind.store_protocol import ValidationFailed
from services.admin.tests.harness import admin as admin
from services.admin.tests.harness import follow, post, token


class Forms(HTMLParser):
    """Every form on a page: where it goes, its fields and the words on its buttons."""

    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict] = []
        self._button: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        found = dict(attrs)
        if tag == "form":
            self.forms.append(
                {
                    "action": found.get("action", ""),
                    "method": (found.get("method") or "get").lower(),
                    "fields": {},
                    "buttons": [],
                }
            )
        elif self.forms and tag == "input" and found.get("name"):
            self.forms[-1]["fields"][found["name"]] = found.get("value", "")
        elif self.forms and tag == "button" and found.get("type", "submit") == "submit":
            self._button = []

    def handle_data(self, data):
        if self._button is not None:
            self._button.append(data)

    def handle_endtag(self, tag):
        if tag == "button" and self._button is not None:
            self.forms[-1]["buttons"].append("".join(self._button).strip())
            self._button = None


def forms(page: str) -> list[dict]:
    reader = Forms()
    reader.feed(page)
    return reader.forms


def form_with(page: str, field: str, action: str) -> dict:
    return next(f for f in forms(page) if f["action"] == action and field in f["fields"])


# -- B2: a box that finds things has a button, and Enter works -------------------------------


def test_finding_a_note_by_name_has_a_button_to_press(admin):
    client, _, store = admin
    box = form_with(client.get("/admin/notes").text, "q", "/admin/notes")
    assert box["method"] == "get"
    assert box["buttons"] == ["Find"]
    # Pressing the button, or Enter, sends the box the way a browser sends a form.
    store.answers["notes-find"] = {"items": []}
    assert client.get("/admin/notes", params={"q": "Budget"}).status_code == 200
    assert store.asked("notes-find") == [{"q": "Budget"}]


def test_trying_a_search_has_a_button_to_press(admin):
    client, _, store = admin
    box = form_with(client.get("/admin/settings/search").text, "q", "/admin/settings/search")
    assert box["method"] == "get"
    assert box["buttons"] == ["Search"]
    store.answers["search-try"] = {"hits": []}
    assert client.get("/admin/settings/search", params={"q": "dispatch"}).status_code == 200
    assert store.asked("search-try") == [{"q": "dispatch"}]


# -- B3: the optional setup steps can be skipped, and setup can finish without them ----------


def unfinished(store, **skipped) -> None:
    """Setup with the needed steps done and nothing optional set up."""
    overview = store.views["overview"]
    overview["setup"] = {"finished_seen": False, "skipped": dict(skipped)}
    overview["sugg"]["state"] = "none"
    overview["trans"]["state"] = "none"
    overview["calendar"]["files"] = 0


def skips(page: str) -> list[str]:
    return [
        f["fields"]["step"]
        for f in forms(page)
        if f["action"] == "/admin/setup/skip" and f["buttons"] == ["Skip for now"]
    ]


def test_every_optional_setup_step_offers_skip_for_now_from_the_start(admin):
    client, _, store = admin
    unfinished(store)
    # Even before the needed steps are done, the optional ones can be skipped.
    store.views["overview"]["companion"]["paired"] = []
    assert skips(client.get("/admin/setup").text) == ["sugg", "trans", "cal"]


def test_skipping_a_step_tells_the_store_which_one(admin):
    client, _, store = admin
    unfinished(store)
    sent = post(client, "/admin/setup/skip", {"step": "cal"}, page="/admin/setup")
    assert sent.status_code == 303 and sent.headers["location"] == "/admin/setup"
    assert store.asked("setup-skip") == [{"step": "cal"}]


def test_setup_finishes_with_suggestions_transcription_and_calendar_all_skipped(admin):
    client, _, store = admin
    unfinished(store, sugg=True, trans=True, cal=True)
    page = client.get("/admin/setup").text
    assert "Setup is finished" in page
    assert skips(page) == []
    assert store.asked("setup-finished") == [{}]


def test_an_optional_step_can_be_skipped_from_its_own_page(admin):
    client, _, store = admin
    unfinished(store)
    for path, step in (
        ("/admin/suggestions", "sugg"),
        ("/admin/transcription", "trans"),
        ("/admin/calendar", "cal"),
    ):
        assert skips(client.get(path).text) == [step], path
    # Once it is skipped the page stops offering it.
    unfinished(store, cal=True)
    assert skips(client.get("/admin/calendar").text) == []


def test_after_a_needed_step_the_strip_lets_the_optional_next_one_be_skipped(admin):
    client, _, store = admin
    unfinished(store)
    page = client.get("/admin/sync").text
    assert "Next: Turn on suggestions (optional)" in page
    assert skips(page) == ["sugg"]
    assert "Setup is finished" not in follow(
        client, post(client, "/admin/setup/skip", {"step": "sugg"}, page="/admin/setup")
    )


# -- B4: a recording added while transcription is off -----------------------------------------

KEPT = {
    "id": "a:AUDIO1",
    "kind": "recording",
    "title": "standup",
    "filename": "standup.m4a",
    "when": "2026-11-12T17:16:39+00:00",
    "state": "kept",
    "size": 179200,
}


def write_ups(page: str) -> list[dict]:
    return [f for f in forms(page) if f["action"].endswith("/write-up")]


def test_a_recording_kept_with_transcription_off_is_a_recording_that_is_not_written_up(admin):
    client, _, store = admin
    store.views["overview"]["trans"]["state"] = "none"
    store.answers["incoming"] = {"items": [KEPT]}
    page = client.get("/admin/incoming").text
    assert "Recordings (1)" in page and "Documents (0)" in page
    assert '<span class="lat-badge">Not written up</span>' in page
    assert 'class="lat-badge lat-badge--ok"' not in page
    assert "Kept, but not written up, because transcription was off when it came in." in page
    # Nothing offers to write it up while transcription is off, and it can still be removed.
    assert write_ups(page) == []
    assert "/admin/incoming?ask=a%3AAUDIO1" in page or "/admin/incoming?ask=a:AUDIO1" in page


def test_once_transcription_is_on_its_line_has_write_it_up_now_for_the_stored_audio(admin):
    client, _, store = admin
    store.views["overview"]["trans"]["state"] = "on"
    store.answers["incoming"] = {"items": [KEPT]}
    page = client.get("/admin/incoming").text
    assert "add the recording again" not in page
    (button,) = write_ups(page)
    assert button["method"] == "post" and button["buttons"] == ["Write it up now"]
    # Showing the page queues nothing: only the press does.
    assert store.asked("incoming-write-up") == []

    store.answers["incoming-write-up"] = {"kind": "recording", "title": "standup", "id": "r:J1"}
    landed = follow(
        client, post(client, button["action"], {"back": "/admin/incoming"}, "/admin/incoming")
    )
    assert store.asked("incoming-write-up") == [{"id": "a:AUDIO1"}]
    assert "is in the queue to be written up, from the file Coppermind kept" in landed
    # The audio already stored is used: nothing was uploaded or accepted again.
    assert store.asked("incoming-accept") == []


def test_a_refused_write_up_says_why_in_the_stores_own_plain_words(admin):
    client, _, store = admin
    store.views["overview"]["trans"]["state"] = "on"
    store.answers["incoming"] = {"items": [KEPT]}
    store.answers["incoming-write-up"] = ValidationFailed(
        ["Transcription is off. Turn it on under Transcription, then press Write it up now again."]
    )
    (button,) = write_ups(client.get("/admin/incoming").text)
    landed = follow(
        client, post(client, button["action"], {"back": "/admin/incoming"}, "/admin/incoming")
    )
    assert "That recording was not sent to be written up." in landed
    assert "Transcription is off. Turn it on under Transcription" in landed


def test_adding_a_recording_with_transcription_off_shows_one_notice_not_two(admin):
    client, _, store = admin
    store.views["overview"]["trans"]["state"] = "none"

    async def already_stored(reservation):
        return SimpleNamespace(state="stored", artifact=SimpleNamespace(id="AUDIO1"), failure="")

    store.reserve_staged_upload = already_stored
    store.answers["incoming-accept"] = {"kind": "audio", "title": "standup", "id": "a:AUDIO1"}
    store.answers["incoming"] = {"items": [KEPT]}
    standing = "Transcription is off, so a recording you add is kept as a file"
    assert standing in client.get("/admin/incoming").text

    added = client.post(
        "/admin/incoming/add",
        data={"csrf": token(client, "/admin/incoming")},
        files={"file": ("standup.m4a", b"\x00" * 64, "audio/mp4")},
        follow_redirects=False,
    )
    landed = follow(client, added)
    assert "It is kept, but not written up, because transcription is off." in landed
    assert standing not in landed
    # The standing notice is back once the one about this recording has been read.
    assert standing in client.get("/admin/incoming").text


# -- B6: a recording on the move keeps its row and its page current by itself ----------------

WAITING = {
    "id": "r:REC1",
    "artifact_id": "AUDIO2",
    "kind": "recording",
    "title": "Customer sync",
    "filename": "Customer sync.mp3",
    "minutes": 3,
    "when": "2026-11-12T19:20:00+00:00",
    "state": "waiting",
    "held": False,
    "error": "",
    "rule": "",
    "retry_after": "",
    "attempts": 0,
    "attempt_limit": 2,
    "started_at": "",
    "done_at": "",
    "took_minutes": None,
    "speakers": 0,
    "note_id": "",
    "audio_removed": False,
    "dismissed": False,
    "size": 42276,
    "has_audio": True,
}


class Live(HTMLParser):
    """The body's refresh interval and the text inside the part that replaces itself."""

    def __init__(self) -> None:
        super().__init__()
        self.refresh = ""
        self.depth = 0
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        found = dict(attrs)
        if tag == "body":
            self.refresh = found.get("data-refresh", "")
        if self.depth:
            self.depth += tag == "div"
        elif tag == "div" and "data-live" in found:
            self.depth = 1

    def handle_endtag(self, tag):
        if self.depth and tag == "div":
            self.depth -= 1

    def handle_data(self, data):
        if self.depth:
            self.text.append(data)


def live(page: str) -> tuple[str, str]:
    reader = Live()
    reader.feed(page)
    return reader.refresh, " ".join("".join(reader.text).split())


def test_the_list_updates_by_itself_while_a_recording_waits_and_says_so(admin):
    client, _, store = admin
    store.views["overview"]["trans"]["state"] = "on"
    store.answers["incoming"] = {"items": [WAITING]}
    refresh, part = live(client.get("/admin/incoming").text)
    assert refresh == "5"
    assert "Customer sync" in part and "Waiting for its turn" in part
    assert "This list updates by itself" in part

    # Written up: the row says so and the page stops asking.
    done = {**WAITING, "state": "done", "done_at": "2026-11-12T19:24:00+00:00", "speakers": 2}
    store.answers["incoming"] = {"items": [done]}
    refresh, part = live(client.get("/admin/incoming").text)
    assert refresh == ""
    assert "Written up with 2 speakers told apart" in part
    assert "updates by itself" not in part


def test_a_list_that_cannot_change_by_itself_does_not_keep_asking(admin):
    client, _, store = admin
    store.answers["incoming"] = {"items": [WAITING, KEPT]}
    for state in ("paused", "none"):
        store.views["overview"]["trans"]["state"] = state
        refresh, part = live(client.get("/admin/incoming").text)
        assert refresh == "", state
        assert "updates by itself" not in part


def test_a_waiting_recordings_own_page_updates_by_itself_and_says_so(admin):
    client, _, store = admin
    store.views["overview"]["trans"]["state"] = "on"
    store.answers["incoming-item"] = WAITING
    refresh, part = live(client.get("/admin/incoming/r:REC1").text)
    assert refresh == "5"
    assert "Waiting for its turn" in part and "This page updates by itself." in part

    store.answers["incoming-item"] = {**WAITING, "state": "working", "started_at": WAITING["when"]}
    refresh, part = live(client.get("/admin/incoming/r:REC1").text)
    assert refresh == "5"
    assert "Being written up" in part and "This page updates by itself" in part

    store.answers["incoming-item"] = {
        **WAITING,
        "state": "done",
        "done_at": "2026-11-12T19:24:00+00:00",
        "note_id": "01M78C5WZH4QTSR5QBRTX456K7",
    }
    refresh, part = live(client.get("/admin/incoming/r:REC1").text)
    assert refresh == ""
    assert "Written up" in part and "See the note" in part


# -- B5: the note history speaks plainly ------------------------------------------------------

RAW = (
    "The model's filename suggestion quoted text that is not in the note or transcript "
    "(quote-not-found). Retry the job; if it keeps failing, try a different model."
)


class Outside(HTMLParser):
    """The words a person sees without opening any "Details for support"."""

    def __init__(self) -> None:
        super().__init__()
        self.closed = 0
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "details":
            self.closed += 1

    def handle_endtag(self, tag):
        if tag == "details":
            self.closed -= 1

    def handle_data(self, data):
        if not self.closed:
            self.text.append(data)


def shown(page: str) -> str:
    reader = Outside()
    reader.feed(page)
    return " ".join("".join(reader.text).split())


def failed_line(store, **extra) -> str:
    note = store.views["note"]
    note["history"].append(
        {
            "at": "2026-11-12T17:30:00+00:00",
            "text": "Suggestions could not be written for this note.",
            "details": RAW,
            **extra,
        }
    )
    return note["id"]


def test_a_failed_suggestion_is_a_plain_sentence_with_the_raw_text_for_support_only(admin):
    client, _, store = admin
    page = client.get(f"/admin/notes/{failed_line(store, summary_again=False)}").text
    seen = shown(page)
    assert "Suggestions could not be written for this note." in seen
    assert "quote-not-found" not in seen and "Retry the job" not in seen
    # The raw text is still there for support, behind its own link.
    assert ">Details for support</summary>" in page
    assert "quote-not-found" in page
    assert "Write the summary again" not in seen


def test_a_failed_suggestion_on_a_recording_note_points_at_write_the_summary_again(admin):
    client, _, store = admin
    identity = failed_line(store, summary_again=True)
    page = client.get(f"/admin/notes/{identity}").text
    assert "press Write the summary again" in shown(page)
    assert f'href="/admin/notes/{identity}/review"' in page


# -- B9: an address that was just changed and does not answer ---------------------------------


def changed_and_silent(store) -> dict:
    """The first service, saved to an address nothing answers at."""
    service = store.views["services"]["services"][0]
    service.update(
        address="standin:9999",
        address_before="standin:9000/home",
        answering=False,
        down_since="2026-11-12T19:40:00+00:00",
        fails=1,
        detail="connection refused",
    )
    store.answers["service"] = {"service": service}
    return service


def test_saving_an_address_nothing_answers_at_names_the_one_before(admin):
    client, _, store = admin
    service = changed_and_silent(store)
    store.answers["service-save"] = {
        "service": service,
        "tested": True,
        "answering": False,
        "moved": True,
        "key_refused": False,
    }
    path = f"/admin/services/{service['id']}"
    landed = follow(
        client,
        post(
            client,
            path + "/save",
            {"name": service["name"], "address": "standin:9999", "back": path},
            page=path,
        ),
    )
    assert "Saved, but nothing answers at standin:9999." in landed
    assert "The address before was standin:9000/home." in landed
    assert "type it back in and save" in landed


def test_a_changed_address_is_doubted_before_a_sleeping_computer(admin):
    client, _, store = admin
    service = changed_and_silent(store)
    page = shown(client.get(f"/admin/services/{service['id']}").text)
    assert "was changed, and nothing has answered at the new one (standin:9999)" in page
    assert "The address before was standin:9000/home" in page
    assert "switched off or asleep" not in page

    # A service whose address was not changed keeps the usual first guess.
    service.update(address_before="")
    page = shown(client.get(f"/admin/services/{service['id']}").text)
    assert "switched off or asleep" in page
