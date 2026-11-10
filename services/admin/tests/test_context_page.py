"""Admin context forms stay authenticated, CSRF guarded and secret-free."""

from typing import Any

from coppermind.context_config import ContextConfig, ModelConnection
from services.admin.tests.browser import admin_browser as admin_browser
from services.admin.tests.test_admin import form_token


class ContextStore:
    def __init__(self):
        self.calls = []

    async def context_action(self, action, payload):
        self.calls.append((action, payload))
        if action == "snapshot":
            return {
                "config": ContextConfig(
                    connections=[
                        ModelConnection(
                            id="local",
                            name="Local",
                            provider="compatible",
                            base_url="http://local.example/v1",
                        )
                    ]
                ).model_dump(),
                "enrichment_fields": [
                    {"name": "account", "kind": "string"},
                    {"name": "attendees", "kind": "list"},
                    {"name": "review_date", "kind": "date"},
                ],
                "calendar": {"imports": []},
                "events": [
                    {
                        "title": "<script>bad()</script>",
                        "calendar_id": "work",
                        "start": "2026-10-14T14:00:00+00:00",
                        "cancelled": False,
                    }
                ],
                "contacts": [
                    {
                        "id": "casey",
                        "evidence_count": 200,
                        "evidence": [
                            {
                                "name": "Casey",
                                "address": "mailto:casey@example.test",
                                "role": "invitee",
                                "participation": "ACCEPTED",
                                "event_id": "meeting",
                            }
                        ],
                    }
                ],
                "jobs": [],
                "associations": {},
                "watch_status": {},
                "worker_error": "",
                "discovery": {"local": ["detected-model"]},
                "tests": {},
                "contact_corrections": {},
            }
        if action == "backlog":
            return {"notes": [], "cursor": None}
        return {"saved": True}


def test_calendar_models_and_review_render_and_escape(signed_in):
    client, _ = signed_in
    client.app.state.store = ContextStore()
    calendar = client.get("/admin/context")
    assert calendar.status_code == 200
    assert "&lt;script&gt;" in calendar.text
    assert "Showing 1 of 200 retained evidence records" in calendar.text
    assert "9:00 AM CDT" in calendar.text
    assert "14:00:00+00:00" not in calendar.text
    models = client.get("/admin/context?tab=models")
    assert models.status_code == 200
    assert 'type="password"' in models.text
    assert "sample meeting text" in models.text
    review = client.get("/admin/context?tab=review")
    assert review.status_code == 200
    assert "No jobs or unreviewed notes yet." in review.text
    assert "Queue selected notes" not in review.text


def test_configuration_and_backlog_are_csrf_guarded(signed_in):
    client, _ = signed_in
    fake = ContextStore()
    client.app.state.store = fake
    assert client.post("/v1/admin/context/enqueue", data={}).status_code == 403
    csrf = form_token(client, "/admin/context?tab=review")
    response = client.post(
        "/v1/admin/context/enqueue",
        data={
            "csrf": csrf,
            "note_example": "on",
            "confirm_send": "on",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    action, payload = fake.calls[-1]
    assert action == "enqueue"
    assert payload["note_ids"] == ["example"]
    assert payload["confirmation"] == "Send selected notes for enrichment"


def test_connections_are_collapsible_and_schema_drives_choices(signed_in):
    from html.parser import HTMLParser

    client, _ = signed_in
    client.app.state.store = ContextStore()
    response = client.get("/admin/context?tab=models")
    assert 'class="connection-card"' in response.text
    assert "Add connection" in response.text
    assert "Connection ID" not in response.text
    assert "manual_model" not in response.text
    assert "Advanced limits" in response.text
    assert "Convert to automatic ID" in response.text

    class Choices(HTMLParser):
        def __init__(self):
            super().__init__()
            self.name = None
            self.values = {}

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "select":
                self.name = attrs.get("name")
                self.values.setdefault(self.name, [])
            elif tag == "option" and self.name:
                self.values[self.name].append(attrs.get("value"))

        def handle_endtag(self, tag):
            if tag == "select":
                self.name = None

    parsed = Choices()
    parsed.feed(response.text)
    assert parsed.values["attendees_field"] == ["", "attendees"]
    assert parsed.values["event_field"] == ["", "account"]
    assert "field_account" in response.text and "field_review_date" in response.text


def test_conversion_is_csrf_protected(signed_in):
    client, _ = signed_in
    fake = ContextStore()
    client.app.state.store = fake
    assert (
        client.post(
            "/v1/admin/context/convert-connection", data={"connection": "local"}
        ).status_code
        == 403
    )
    token = form_token(client, "/admin/context?tab=models")
    response = client.post(
        "/v1/admin/context/convert-connection",
        data={"connection": "local", "csrf": token, "revision": "0"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert fake.calls[-1][0] == "convert-connection"
    assert fake.calls[-1][1]["connection"] == "local"


def test_size_failure_shows_both_numbers_and_links_to_the_setting(signed_in):
    client, _ = signed_in
    job = {
        "id": "a" * 64,
        "path": "Long.md",
        "state": "failed",
        "error": "The note and transcript text is 52,000 characters; the limit is 40,000.",
        "error_rule": "input-limit",
        "input_size": 52000,
        "input_limit": 40000,
        "result": None,
        "decisions": [],
        "newest": True,
        "current": {"frontmatter": {}},
    }

    class JobStore(ContextStore):
        async def context_action(self, action, payload):
            if action == "job":
                return job
            data = await super().context_action(action, payload)
            if action == "snapshot":
                data["jobs"] = [job]
            return data

    client.app.state.store = JobStore()
    link = 'href="/admin/workflow?tab=enrichment#processing-limits"'
    for url in ("/admin/context?tab=review", f"/admin/context/jobs/{job['id']}"):
        page = client.get(url)
        assert "52,000 characters; the limit is 40,000" in page.text
        assert link in page.text


def test_calendar_import_failures_can_be_dismissed_and_dismissed_ones_are_marked(signed_in):
    class HistoryStore(ContextStore):
        async def context_action(self, action, payload):
            result = await super().context_action(action, payload)
            if action == "snapshot":
                result["calendar"] = {
                    "imports": [
                        {
                            "id": "open",
                            "filename": "open.ics",
                            "state": "failed",
                            "error": "conflict",
                            "dismissed": False,
                        },
                        {
                            "id": "closed",
                            "filename": "closed.ics",
                            "state": "failed",
                            "error": "conflict",
                            "dismissed": True,
                        },
                    ]
                }
            return result

    client, _ = signed_in
    fake = HistoryStore()
    client.app.state.store = fake
    page = client.get("/admin/context?tab=calendar")
    assert page.status_code == 200
    assert page.text.count("Dismiss failure") == 1
    assert "Retry import" not in page.text
    assert "Import failed (dismissed)" in page.text
    assert client.post("/v1/admin/context/dismiss-import", data={"id": "open"}).status_code == 403
    csrf = form_token(client, "/admin/context?tab=calendar")
    result = client.post(
        "/v1/admin/context/dismiss-import",
        data={"id": "open", "csrf": csrf},
        follow_redirects=False,
    )
    assert result.status_code == 303
    assert fake.calls[-1] == ("dismiss-import", {"id": "open", "revision": 0})


def test_dropbox_receipts_can_be_dismissed_through_guarded_form(signed_in):
    class ReceiptStore(ContextStore):
        async def context_action(self, action, payload):
            result = await super().context_action(action, payload)
            if action == "snapshot":
                result["dropbox"] = {
                    "folder": "Dropbox",
                    "items": [
                        {
                            "id": "receipt",
                            "filename": "Exchange.zip",
                            "state": "failed",
                            "error": "Conflicting event revision",
                            "parts": [],
                        }
                    ],
                }
            return result

    client, _ = signed_in
    fake = ReceiptStore()
    client.app.state.store = fake
    page = client.get("/admin/workflow?tab=intake")
    assert page.status_code == 200
    assert "Dismiss receipt" in page.text and "Import failed" in page.text
    assert "Identify calendar" not in page.text
    assert client.post("/v1/admin/context/dismiss-drop", data={"id": "receipt"}).status_code == 403
    csrf = form_token(client, "/admin/workflow?tab=intake")
    result = client.post(
        "/v1/admin/context/dismiss-drop",
        data={"id": "receipt", "csrf": csrf},
        follow_redirects=False,
    )
    assert result.status_code == 303
    assert fake.calls[-1][0] == "dismiss-drop"
    assert fake.calls[-1][1]["id"] == "receipt"


class CalendarUXStore(ContextStore):
    async def context_action(self, action, payload):
        result = await super().context_action(action, payload)
        if action == "snapshot":
            result["calendar"]["imports"] = [
                {
                    "id": "ok",
                    "filename": "one.ics",
                    "state": "succeeded",
                    "event_count": 2,
                    "error": "",
                },
                {
                    "id": "failed",
                    "filename": "bad.zip",
                    "state": "failed",
                    "error": "Conflicting Planning",
                    "dismissed": False,
                },
                {
                    "id": "dismissed",
                    "filename": "old.zip",
                    "state": "failed",
                    "error": "Old conflict",
                    "dismissed": True,
                },
            ]
            result["events"] = [
                {
                    "id": "a" * 64,
                    "title": "Planning",
                    "start": "2026-11-03T15:00:00+00:00",
                    "cancelled": False,
                    "people": [
                        {
                            "address": "mailto:casey@example.test",
                            "name": "Casey",
                            "role": "attendee",
                        }
                    ],
                    "provenance": [
                        {
                            "filename": "one.ics",
                            "entry": "one.ics",
                            "imported_at": "2026-11-03T15:00:00+00:00",
                        }
                    ],
                }
            ]
            result["contacts"] = [
                {
                    "id": "b" * 64,
                    "evidence": [
                        {
                            "event_id": "a" * 64,
                            "name": "Casey",
                            "address": "mailto:casey@example.test",
                            "role": "attendee",
                            "participation": "ACCEPTED",
                            "event_title": "Planning",
                            "event_start": "2026-11-03T15:00:00+00:00",
                        }
                    ],
                }
            ]
            result["dropbox"] = {
                "folder": "Calendar exports",
                "items": [
                    {
                        "id": "",
                        "filename": "copying.zip",
                        "state": "stabilizing",
                        "parts": [],
                        "error": "",
                    },
                    {
                        "id": "again",
                        "filename": "one.ics",
                        "state": "already_imported",
                        "parts": [],
                        "error": "",
                        "event_count": 2,
                        "first_imported_at": "2026-11-03T15:00:00+00:00",
                    },
                ],
            }
        if action == "retry-import":
            return {"state": "failed", "error": "Planning still conflicts"}
        return result


def test_calendar_receipts_and_people_are_actionable(signed_in):
    from html.parser import HTMLParser

    class Page(HTMLParser):
        def __init__(self):
            super().__init__()
            self.text = []
            self.dismissals = []
            self.dismiss_form = False

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "form":
                self.dismiss_form = attrs.get("action") == "/v1/admin/context/dismiss-import"
            if tag == "input" and self.dismiss_form and attrs.get("name") == "id":
                self.dismissals.append(attrs["value"])

        def handle_endtag(self, tag):
            if tag == "form":
                self.dismiss_form = False

        def handle_data(self, value):
            self.text.append(value)

    client, _ = signed_in
    client.app.state.store = CalendarUXStore()
    page = client.get("/admin/context?tab=calendar")
    parsed = Page()
    parsed.feed(page.text)
    text = " ".join(parsed.text)
    assert "Imported (2 events)" in text and "Imported:" not in text
    assert "Import failed (dismissed)" in text
    assert "Retry import" not in text
    assert "Invitee: Casey (casey@example.test)" in text
    assert "one.ics · one.ics" not in text
    assert "a" * 64 not in text and "b" * 64 not in text
    assert "mailto:" not in text
    assert parsed.dismissals == ["failed"]
    assert client.post("/v1/admin/context/dismiss-import", data={"id": "failed"}).status_code == 403
    csrf = form_token(client, "/admin/context?tab=calendar")
    response = client.post("/v1/admin/context/retry-import", data={"csrf": csrf, "id": "failed"})
    assert response.status_code == 422
    assert "Planning still conflicts" in response.text
    assert "Action completed." not in response.text


def test_receipts_show_waiting_original_and_import(signed_in):
    client, _ = signed_in
    fake = CalendarUXStore()
    client.app.state.store = fake
    page = client.get("/admin/workflow?tab=intake")
    assert "Waiting for the file to settle" in page.text
    assert "No receipts to show" not in page.text
    assert "First imported: November 3, 2026, 9:00 AM CST" in page.text
    assert "2 events" in page.text
    assert "Receipt settings" not in page.text
    csrf = form_token(client, "/admin/workflow?tab=intake")
    response = client.post(
        "/v1/admin/context/save-intake", data={"csrf": csrf, "retention_days": "1.5"}
    )
    assert response.status_code == 422


def test_calendar_empty_sections_explain_next_step(signed_in):
    class EmptyStore(ContextStore):
        async def context_action(self, action, payload):
            result = await super().context_action(action, payload)
            if action == "snapshot":
                result["events"] = []
                result["contacts"] = []
            return result

    client, _ = signed_in
    client.app.state.store = EmptyStore()
    calendar = client.get("/admin/context?tab=calendar")
    assert "No calendar events" in calendar.text and "No calendar contacts" in calendar.text
    assert "<th>Event</th>" not in calendar.text and "<th>File</th>" not in calendar.text
    dropbox = client.get("/admin/workflow?tab=intake")
    assert "folder in your Obsidian vault" in dropbox.text
    assert "file here" not in dropbox.text


def test_speed_note_and_outcome_line_speak_plainly():
    from coppermind_admin.pages.context import _outcome_line, _speed_note

    assignment = {
        "connection": "c",
        "model": "m",
        "max_output_tokens": 2048,
        "timeout_s": 45,
    }
    streamed = {"speed": {"c/m": {"tokens_per_s": 32.0, "streamed": True}}}
    assert "takes about 64 seconds" in _speed_note(streamed, assignment)
    assert "error" not in _speed_note(streamed, assignment)
    whole = {"speed": {"c/m": {"tokens_per_s": 32.0, "streamed": False}}}
    assert 'class="error"' in _speed_note(whole, assignment)
    assert "not measured" in _speed_note({}, assignment)
    none = {"outcomes": {"counts": {"failed": 2}, "answered": 0, "last_success": ""}}
    assert "No job has succeeded yet" in _outcome_line(none)


def test_failed_jobs_offer_retry_and_report_bulk_outcome(signed_in):
    class FailedJobs(ContextStore):
        async def context_action(self, action, payload):
            data = await super().context_action(action, payload)
            if action == "snapshot":
                data["jobs"] = [
                    {
                        "id": "a" * 64,
                        "path": "Review/Example.md",
                        "state": "failed",
                        "error": "Request failed",
                        "input_limit": None,
                    }
                ]
            if action == "retry-all-failed":
                return {"queued": 1, "errors": []}
            return data

    client, _ = signed_in
    client.app.state.store = FailedJobs()
    page = client.get("/admin/context?tab=review")
    assert "Retry all failed" in page.text
    assert "may repeat a billed request" in page.text
    assert ">Retry</button>" in page.text
    csrf = form_token(client, "/admin/context?tab=review")
    response = client.post("/v1/admin/context/retry-all-failed", data={"csrf": csrf})
    assert "Retry all failed: 1 queued." in response.text


def test_bulk_retry_banner_counts_failures_without_echoing_text(signed_in):
    class ManyErrors(ContextStore):
        async def context_action(self, action, payload):
            if action == "retry-all-failed":
                return {"queued": 2, "errors": [f"Review/N{i}.md: no model" for i in range(40)]}
            return await super().context_action(action, payload)

    client, _ = signed_in
    client.app.state.store = ManyErrors()
    csrf = form_token(client, "/admin/context?tab=review")
    response = client.post(
        "/v1/admin/context/retry-all-failed", data={"csrf": csrf}, follow_redirects=False
    )
    assert response.status_code == 303
    page = client.get(response.headers["location"])
    assert "Retry all failed: 2 queued." in page.text
    assert "40 could not be queued" in page.text
    assert "Review/N0.md" not in page.text


def test_result_banner_ignores_arbitrary_query_text(signed_in):
    client, _ = signed_in
    client.app.state.store = ContextStore()
    page = client.get("/admin/context?tab=review&result=Your+password+expired")
    assert 'class="success"' not in page.text
    assert "Your password expired" not in page.text
    page = client.get("/admin/context?tab=review&result=retry-all-failed&queued=x")
    assert 'class="success"' not in page.text


def test_only_the_newest_job_offers_retry_on_inspect(signed_in):
    client, _ = signed_in
    base: dict[str, Any] = {
        "id": "a" * 64,
        "path": "Long.md",
        "state": "failed",
        "error": "Request failed",
        "error_rule": "",
        "input_size": None,
        "input_limit": None,
        "result": None,
        "decisions": [],
        "current": {"frontmatter": {}},
    }

    class JobStore(ContextStore):
        newest = True

        async def context_action(self, action, payload):
            if action == "job":
                return {**base, "newest": self.newest}
            return await super().context_action(action, payload)

    store = JobStore()
    client.app.state.store = store
    assert "Retry with current evidence" in client.get(f"/admin/context/jobs/{'a' * 64}").text
    store.newest = False
    assert "Retry with current evidence" not in client.get(f"/admin/context/jobs/{'a' * 64}").text


def test_failed_job_that_cannot_retry_shows_reason_and_no_bulk_retry(signed_in):
    class BlockedJobs(ContextStore):
        async def context_action(self, action, payload):
            data = await super().context_action(action, payload)
            if action == "snapshot":
                data["jobs"] = [
                    {
                        "id": "a" * 64,
                        "path": "Review/Example.md",
                        "state": "failed",
                        "error": "Request failed",
                        "input_limit": None,
                        "can_retry": False,
                        "retry_reason": "Enable enrichment to retry.",
                    }
                ]
            return data

    client, _ = signed_in
    client.app.state.store = BlockedJobs()
    page = client.get("/admin/context?tab=review")
    assert "Enable enrichment to retry." in page.text
    assert "Retry all failed" not in page.text
    assert ">Retry</button>" not in page.text


def test_jobs_explain_stale_pause_and_moved_note(signed_in):
    class UXStore(ContextStore):
        async def context_action(self, action, payload):
            data = await super().context_action(action, payload)
            if action == "snapshot":
                data["config"]["assignments"]["enrichment"]["paused"] = True
                data["jobs"] = [
                    {
                        "id": "a" * 64,
                        "path": "Review/Old.md",
                        "current_path": "Notes/New.md",
                        "current_title": "New",
                        "state": "stale",
                        "error": "stale: associated source revisions changed",
                        "input_limit": None,
                        "can_retry": True,
                    }
                ]
            return data

    client, _ = signed_in
    client.app.state.store = UXStore()
    response = client.get("/admin/context?tab=review")
    assert "Processing is paused. Queued retries will wait until you resume." in response.text
    assert "Resume processing" in response.text
    assert "Notes/New.md" in response.text and "Moved from Review/Old.md" in response.text
    assert (
        "Out of date: The supporting information changed. Retry to use the latest information."
        in response.text
    )
    assert "stale: associated" not in response.text
    assert ">Retry</button>" in response.text
    assert "Existing Review backlog" not in response.text
    retry = client.get("/admin/context?tab=review&result=retry-job&job=" + "a" * 64)
    assert "Retry queued for New. Processing is paused; resume it to start." in retry.text
