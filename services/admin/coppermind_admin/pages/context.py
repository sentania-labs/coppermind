"""Calendar intake, provider configuration and human-reviewed enrichment."""

from __future__ import annotations

import json
from html import escape as e
from typing import Any, cast
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from coppermind.context_config import FeatureAssignment
from coppermind.errors import to_http
from coppermind.settings import Wiring
from coppermind.store_client import HttpStoreClient
from coppermind.store_protocol import StoreError
from coppermind_admin.pages.problems import store_client


def _input(name: str, label: str, value: str = "", *, kind: str = "text") -> str:
    return (
        f'<label>{e(label)}<input name="{e(name)}" type="{kind}" '
        f'value="{e(value, quote=True)}"></label>'
    )


def _form(action: str, content: str, button: str, *, revision: int = 0) -> str:
    return (
        f'<form method="post" action="/v1/admin/context/{action}">'
        f'<input type="hidden" name="revision" value="{revision}">{content}'
        f"<button>{e(button)}</button></form>"
    )


def _hidden(name: str, value: str) -> str:
    return f'<input type="hidden" name="{e(name)}" value="{e(value, quote=True)}">'


def _check(name: str, label: str, checked: bool = False) -> str:
    selected = " checked" if checked else ""
    return f'<label><input type="checkbox" name="{name}"{selected}> {e(label)}</label>'


def _select(name: str, label: str, choices: list[tuple[str, str]], selected: str = "") -> str:
    options = "".join(
        f'<option value="{e(value, quote=True)}"'
        + (" selected" if value == selected else "")
        + f">{e(title)}</option>"
        for value, title in choices
    )
    return f'<label>{e(label)}<select name="{name}">{options}</select></label>'


def _proposal_value(value: Any) -> str:
    if isinstance(value, str):
        return _hidden("value_kind", "text") + _input("value", "Proposed value", value)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return (
            _hidden("value_kind", "list")
            + '<label>Proposed values (one per line)<textarea name="value">'
            + e("\n".join(value))
            + "</textarea></label>"
        )
    if isinstance(value, bool):
        return _hidden("value_kind", "bool") + _select(
            "value",
            "Proposed value",
            [("true", "Yes"), ("false", "No")],
            "true" if value else "false",
        )
    if isinstance(value, (int, float)):
        return _hidden("value_kind", "number") + _input(
            "value", "Proposed value", str(value), kind="number"
        )
    return _hidden("value_kind", "unchanged")


def router(wiring: Wiring) -> APIRouter:
    routes = APIRouter()

    def client(request: Request) -> HttpStoreClient:
        return cast(HttpStoreClient, store_client(request, wiring))

    def page(body: str, status: int = 200) -> HTMLResponse:
        from coppermind_admin.main import page as render

        return HTMLResponse(
            render("Calendar and enrichment", '<div class="context-content">' + body + "</div>"),
            status_code=status,
        )

    def error(exc: Exception) -> HTMLResponse:
        status = to_http(exc)[0] if isinstance(exc, StoreError) else 422
        return page(
            '<h1>Could not complete that action</h1><p class="error">'
            + e(str(exc))
            + '</p><a href="/admin/context">Back to calendar and enrichment</a>',
            status,
        )

    @routes.get("/admin/context", include_in_schema=False)
    async def overview(request: Request) -> Response:
        try:
            data = await client(request).context_action(
                "snapshot",
                {
                    "offset": int(request.query_params.get("offset", "0")),
                },
            )
        except (StoreError, ValueError) as exc:
            return error(exc)
        tab = request.query_params.get("tab", "calendar")
        config = data["config"]
        revision = config["revision"]
        head = """<h1>Calendar and enrichment</h1><p class="section-links">
<a href="/admin/context?tab=calendar">Calendars and contacts</a>
<a href="/admin/context?tab=models">Connections and model</a>
<a href="/admin/context?tab=review">Suggestions and backlog</a></p>"""
        if request.query_params.get("saved"):
            head += '<p class="success">Action completed.</p>'
        if data["worker_error"]:
            head += '<p class="error">' + e(data["worker_error"]) + "</p>"
        for problem in data.get("eligibility_errors", {}).values():
            head += '<p class="notice">' + e(problem) + "</p>"
        if tab == "calendar":
            body = (
                "<p>Drop ICS or ZIP files into a configured Obsidian folder. "
                "Originals are retained. "
            )
            body += "Importing does not send calendar data to a model.</p><h2>Calendar sources</h2>"
            for source in config["sources"]:
                fields = _hidden("id", source["id"])
                fields += _input("name", "Name", source["name"])
                fields += _input("folder", "Vault drop folder", source["folder"])
                fields += _input("timezone", "Calendar timezone", source["timezone"])
                fields += _check("enabled", "Watch this folder", source["enabled"])
                body += "<fieldset><legend>" + e(source["id"]) + "</legend>"
                body += (
                    "<p>"
                    + e(data["watch_status"].get(source["id"], "Waiting for first scan"))
                    + "</p>"
                )
                body += (
                    _form("save-source", fields, "Save source", revision=revision) + "</fieldset>"
                )
            fields = _input("id", "New calendar ID (lowercase letters, numbers, hyphens)")
            fields += _input("name", "Calendar name")
            fields += _input("folder", "Vault drop folder", "_Calendar/Work")
            fields += _input("timezone", "Timezone", "America/Chicago")
            fields += _check("enabled", "Watch this folder", True)
            body += "<details><summary>Add calendar</summary>"
            body += _form("save-source", fields, "Add calendar", revision=revision) + "</details>"
            body += "<h2>Imports</h2><p>10 MiB upload; 40 MiB expanded ZIP; 100 archive entries. "
            body += (
                "Files must stay unchanged for 30 seconds. Non-ICS ZIP entries are rejected.</p>"
            )
            body += "<table><tr><th>File</th><th>Result</th><th>Action</th></tr>"
            for receipt in data["calendar"]["imports"]:
                body += "<tr><td>" + e(receipt["filename"]) + "</td><td>"
                body += e(receipt["state"]) + ": " + e(receipt["error"]) + "</td><td>"
                if receipt["state"] == "failed":
                    body += _form("retry-import", _hidden("id", receipt["id"]), "Retry import")
                body += "</td></tr>"
            body += (
                "</table><h2>Events</h2><table><tr><th>Event</th><th>Date</th><th>State</th></tr>"
            )
            from datetime import datetime
            from zoneinfo import ZoneInfo

            from coppermind_admin.pages.search_index import local_time

            zone = ZoneInfo("America/Chicago")
            for event in data["events"]:
                start = event["start"]
                if "T" in start:
                    stamp = datetime.fromisoformat(start)
                    start = (
                        local_time(stamp, zone)
                        if stamp.tzinfo
                        else stamp.strftime("%b %d, %Y %I:%M %p") + " (floating)"
                    )
                body += (
                    f"<tr><td>{e(event['title'])}<br><small>{e(event['calendar_id'])}</small></td>"
                )
                status = "Cancelled" if event["cancelled"] else "Recorded"
                body += f"<td>{e(start)}</td><td>{status}</td></tr>"
            body += "</table><h2>Contacts</h2><p>Calendar invitees are not confirmed attendees. "
            body += (
                "Matching email addresses share an identity; names alone do not merge contacts.</p>"
            )
            for contact in data["contacts"]:
                correction = data["contact_corrections"].get(contact["id"], {})
                evidence = contact["evidence"]
                title = correction.get("name") or next(
                    (item["name"] for item in evidence if item["name"]), contact["id"]
                )
                body += "<details><summary>" + e(title) + "</summary>"
                body += (
                    "<p>" + e(", ".join(sorted({item["address"] for item in evidence}))) + "</p>"
                )
                body += (
                    "<ul>"
                    + "".join(
                        "<li>"
                        + e(item["role"] + ": " + item["participation"])
                        + " <code>"
                        + e(item["event_id"])
                        + "</code></li>"
                        for item in evidence
                    )
                    + "</ul>"
                )
                linked = [
                    identity
                    for identity, assoc in data["associations"].items()
                    if contact["id"] in assoc["contacts"]
                ]
                body += (
                    "<p>Approved notes: "
                    + ", ".join(
                        f'<a href="/admin/notes/{quote(identity, safe="")}">{e(identity)}</a>'
                        for identity in linked
                    )
                    + "</p>"
                )
                fields = _hidden("id", contact["id"]) + _input(
                    "name", "Corrected name", correction.get("name", "")
                )
                fields += _input(
                    "same_as",
                    "Same person as contact ID (blank clears alias)",
                    correction.get("same_as", ""),
                )
                fields += _hidden("confirmation", "Save contact correction")
                body += _form("correct-contact", fields, "Save contact correction") + "</details>"
        elif tab == "models":
            body = (
                "<p>Connections hold credentials; enrichment separately selects a "
                "connection and model. "
            )
            body += (
                "A future assistant can have its own assignment. No assistant is enabled here.</p>"
            )
            for connection in [
                *config["connections"],
                {"id": "", "name": "", "provider": "compatible", "base_url": ""},
            ]:
                body += (
                    "<fieldset><legend>" + e(connection["name"] or "New connection") + "</legend>"
                )
                fields = _input("id", "Connection ID", connection["id"])
                fields += _input("name", "Name", connection["name"])
                fields += _select(
                    "provider",
                    "Provider",
                    [
                        ("anthropic", "Anthropic"),
                        ("openai", "OpenAI"),
                        ("compatible", "OpenAI-compatible"),
                    ],
                    connection["provider"],
                )
                fields += _input(
                    "base_url",
                    "Base URL, compatible providers only (include /v1 if needed)",
                    connection["base_url"],
                )
                fields += _input("api_key", "API key (blank keeps existing key)", kind="password")
                fields += _check(
                    "clear_key", "Remove stored key for an unauthenticated local server"
                )
                body += _form("save-connection", fields, "Save connection", revision=revision)
                if connection["id"]:
                    body += _form(
                        "discover",
                        _hidden("connection", connection["id"]),
                        "Refresh available models",
                    )
                    model_ids = data["discovery"].get(connection["id"], [])
                    fields = _hidden("connection", connection["id"])
                    fields += _select(
                        "model", "Discovered model", [(item, item) for item in model_ids]
                    )
                    fields += _input(
                        "manual_model", "Manual model ID (if discovery is unavailable)"
                    )
                    body += _form("test-model", fields, "Test model with synthetic content")
                body += "</fieldset>"
            assignment = config["assignments"]["enrichment"]
            fields = _select(
                "connection",
                "Enrichment connection",
                [(c["id"], c["name"]) for c in config["connections"]],
                assignment["connection"],
            )
            model_ids = sorted(
                {item for values in data["discovery"].values() for item in values}
                | {assignment["model"]}
            )
            fields += _select(
                "model",
                "Enrichment model",
                [(item, item) for item in model_ids],
                assignment["model"],
            )
            fields += _input("manual_model", "Manual model ID")
            fields += _input(
                "fields", "Allowed schema fields (comma-separated)", ", ".join(assignment["fields"])
            )
            for key, label in [
                ("attendees_field", "Confirmed attendee field"),
                ("invitees_field", "Invitee field"),
                ("event_field", "Calendar event field"),
                ("contacts_field", "Contact reference field"),
            ]:
                fields += _input(key, label + " (optional, must exist in Fields)", assignment[key])
            for key, label in [
                ("timeout_s", "Request timeout (seconds)"),
                ("max_input_chars", "Maximum input characters"),
                ("max_output_tokens", "Maximum output tokens"),
                ("attempts", "Maximum attempts"),
            ]:
                fields += _input(key, label, str(assignment[key]), kind="number")
            fields += _check(
                "enabled", "Enable enrichment for new unreviewed notes", assignment["enabled"]
            )
            fields += _check("paused", "Pause model calls", assignment["paused"])
            body += (
                '<h2>Enrichment assignment</h2><p class="notice">When enabled, '
                "selected note text, associated transcripts and nearby calendar/contact records "
                "are sent to this provider. Existing notes require an explicit backlog selection. "
                "Calls run one at a time. Keys stay outside the vault and Git.</p>"
            )
            body += _form(
                "save-assignment", fields, "Save enrichment assignment", revision=revision
            )
        else:
            body = (
                "<p>Suggestions never edit a note automatically. Applying changes "
                "leaves it unreviewed.</p>"
            )
            body += "<h2>Jobs</h2><table><tr><th>Note</th><th>Status</th><th>Open</th></tr>"
            for job in data["jobs"]:
                body += (
                    f"<tr><td>{e(job['path'])}</td><td>{e(job['state'])}<br>{e(job['error'])}</td>"
                )
                body += f'<td><a href="/admin/context/jobs/{job["id"]}">Inspect</a></td></tr>'
            body += "</table><h2>Existing Review backlog</h2>"
            try:
                backlog = await client(request).context_action(
                    "backlog", {"cursor": request.query_params.get("cursor")}
                )
            except StoreError as exc:
                return error(exc)
            fields = "".join(
                _check("note_" + note["id"], note["path"]) for note in backlog["notes"]
            )
            fields += _check("confirm_send", "Send selected notes for enrichment")
            body += _form("enqueue", fields, "Queue selected notes")
            if backlog["cursor"]:
                body += (
                    '<a href="/admin/context?tab=review&amp;cursor='
                    + quote(backlog["cursor"], safe="")
                    + '">Next notes</a>'
                )
        body += "<p>Showing up to 100 records per section.</p>"
        if data.get("next_offset") is not None:
            target = f"/admin/context?tab={quote(tab)}&amp;offset={data['next_offset']}"
            body += f'<a href="{target}">Next records</a>'
        return page(head + body)

    @routes.get("/admin/context/jobs/{identity}", include_in_schema=False)
    async def job_page(identity: str, request: Request) -> Response:
        try:
            job = await client(request).context_action("job", {"id": identity})
        except StoreError as exc:
            return error(exc)
        body = (
            "<h1>Review suggestions</h1><p>"
            + e(job["path"])
            + "</p><p>"
            + e(job["state"] + ": " + job["error"])
            + "</p>"
        )
        body += (
            "<p>Current metadata</p><pre>"
            + e(json.dumps(job["current"]["frontmatter"], indent=2))
            + "</pre>"
        )
        if job["result"]:
            body += "<p>Unresolved: " + e("; ".join(job["result"]["unresolved"])) + "</p>"
            decided = {item.get("index") for item in job["decisions"]}
            for index, suggestion in enumerate(job["result"]["suggestions"]):
                if index in decided:
                    continue
                body += (
                    "<fieldset><legend>"
                    + e(suggestion["kind"] + " " + suggestion["field"])
                    + "</legend>"
                )
                body += (
                    "<p>"
                    + e(suggestion["reason"])
                    + "</p><blockquote>"
                    + e(suggestion["quote"])
                    + "</blockquote>"
                )
                body += (
                    "<pre>"
                    + e(
                        json.dumps(
                            {
                                "events": suggestion["event_ids"],
                                "contacts": suggestion["contact_ids"],
                            },
                            indent=2,
                        )
                    )
                    + "</pre>"
                )
                fields = _hidden("id", identity) + _hidden("index", str(index))
                if suggestion["kind"] in {"filename", "field"}:
                    fields += _proposal_value(suggestion["value"])
                fields += _check("confirm_apply", "Apply selected suggestion")
                if suggestion["kind"] == "filename":
                    fields += (
                        '<p class="notice">The filename changes; the heading and body do '
                        "not. Existing filename links may require manual correction.</p>"
                    )
                    fields += _check(
                        "accept_link_risk",
                        "I accept that existing filename links may need correction",
                    )
                if job["state"] == "suggested":
                    body += _form("approve", fields, "Apply this suggestion")
                    body += _form(
                        "reject",
                        _hidden("id", identity) + _hidden("index", str(index)),
                        "Reject this suggestion",
                    )
                body += "</fieldset>"
            from datetime import datetime
            from zoneinfo import ZoneInfo

            from coppermind_admin.pages.search_index import local_time

            body += "<details><summary>Supplied calendar evidence</summary>"
            for event in job["evidence"].get("events", []):
                start = event["start"]
                if "T" in start:
                    moment = datetime.fromisoformat(start)
                    start = (
                        local_time(moment, ZoneInfo("America/Chicago"))
                        if moment.tzinfo
                        else moment.strftime("%b %d, %Y %I:%M %p") + " (floating)"
                    )
                body += "<h3>" + e(event["title"]) + "</h3><p>" + e(start) + "</p><ul>"
                for person in event["people"]:
                    body += (
                        "<li>"
                        + e(person["name"] + " " + person["address"] + " (" + person["role"] + ")")
                        + "</li>"
                    )
                body += "</ul>"
            body += "</details>"
            if job["usage"]:
                body += (
                    "<p>Provider usage: "
                    + e(
                        ", ".join(
                            key.replace("_", " ") + ": " + str(value)
                            for key, value in job["usage"].items()
                        )
                    )
                    + "</p>"
                )
        body += (
            "<p>Apply the suggestions you want. A date change requires fresh calendar matching.</p>"
        )
        if job["state"] == "suggested":
            body += _form("reject", _hidden("id", identity), "Reject proposal")
        if job["state"] in {"failed", "stale"}:
            body += _form("retry-job", _hidden("id", identity), "Retry with current evidence")
        if job["state"] == "applying":
            body += _form("resume-approval", _hidden("id", identity), "Resume recorded approval")
        if job["state"] == "applied":
            body += _form(
                "enqueue",
                _hidden("note_" + job["note_id"], "on")
                + _hidden("regenerate", "yes")
                + _check("confirm_send", "Send selected notes for enrichment"),
                "Generate fresh proposal",
            )
        body += "<h2>Approved associations</h2>"
        for kind, targets in job.get("associations", {}).items():
            for target in targets:
                fields = _hidden("note_id", job["note_id"]) + _hidden("kind", kind)
                fields += _hidden("target", target) + _hidden(
                    "note_hash", job["current"]["content_hash"]
                )
                fields += _hidden("associations_hash", job["associations_hash"])
                fields += _check("confirm_remove", "Remove mistaken association")
                body += "<p>" + e(kind + ": " + target) + "</p>"
                body += _form("correct-association", fields, "Remove this association")
        for correction in job.get("corrections", []):
            if correction["state"] == "pending":
                body += _form(
                    "resume-correction",
                    _hidden("id", correction["id"]),
                    "Resume recorded association correction",
                )
        return page(body + '<p><a href="/admin/context?tab=review">Back to suggestions</a></p>')

    @routes.post("/v1/admin/context/{action}", include_in_schema=False)
    async def action(action: str, request: Request) -> Response:
        from coppermind_admin.main import submitted

        form = await submitted(request)
        try:
            payload: dict[str, Any] = {key: value for key, value in form.items() if key != "csrf"}
            payload["revision"] = int(form.get("revision", "0"))
            if action == "save-source":
                payload["source"] = {
                    key: form.get(key, "") for key in ("id", "name", "folder", "timezone")
                }
                payload["source"]["enabled"] = "enabled" in form
            elif action == "save-connection":
                payload["connection"] = {
                    key: form.get(key, "") for key in ("id", "name", "provider", "base_url")
                }
                payload.pop("api_key", None)
                if form.get("api_key") or "clear_key" in form:
                    payload["api_key"] = "" if "clear_key" in form else form["api_key"]
            elif action == "test-model":
                payload["model"] = form.get("manual_model") or form.get("model", "")
            elif action == "save-assignment":
                assignment = FeatureAssignment().model_dump()
                for key in (
                    "connection",
                    "attendees_field",
                    "invitees_field",
                    "event_field",
                    "contacts_field",
                ):
                    assignment[key] = form.get(key, "")
                assignment["model"] = form.get("manual_model") or form.get("model", "")
                assignment["fields"] = [
                    value.strip() for value in form.get("fields", "").split(",") if value.strip()
                ]
                for key in ("timeout_s", "max_input_chars", "max_output_tokens", "attempts"):
                    assignment[key] = int(form[key])
                assignment["enabled"], assignment["paused"] = "enabled" in form, "paused" in form
                payload["assignment"] = assignment
            elif action == "enqueue":
                payload["regenerate"] = form.get("regenerate") == "yes"
                payload["note_ids"] = [key[5:] for key in form if key.startswith("note_")]
                payload["confirmation"] = (
                    "Send selected notes for enrichment" if "confirm_send" in form else ""
                )
            elif action == "approve":
                kind = form.get("value_kind", "unchanged")
                if kind == "text":
                    payload["value"] = form["value"]
                elif kind == "list":
                    payload["value"] = [
                        line.strip() for line in form["value"].splitlines() if line.strip()
                    ]
                elif kind in {"number", "bool"}:
                    payload["value"] = json.loads(form["value"])
                payload["confirmation"] = (
                    "Apply selected suggestion" if "confirm_apply" in form else ""
                )
                payload["accept_link_risk"] = "accept_link_risk" in form
            elif action == "correct-association":
                payload["confirmation"] = (
                    "Remove mistaken association" if "confirm_remove" in form else ""
                )
            allowed = {
                "save-source",
                "save-connection",
                "discover",
                "test-model",
                "save-assignment",
                "retry-import",
                "enqueue",
                "retry-job",
                "reject",
                "approve",
                "resume-approval",
                "correct-contact",
                "correct-association",
                "resume-correction",
            }
            if action not in allowed:
                raise ValueError("unknown action")
            await client(request).context_action(action, payload)
        except (StoreError, ValueError, KeyError) as exc:
            return error(exc)
        tab = (
            "models"
            if action in {"save-connection", "discover", "test-model", "save-assignment"}
            else "review"
            if action in {"enqueue", "retry-job", "reject", "approve", "resume-approval"}
            else "calendar"
        )
        return RedirectResponse(f"/admin/context?tab={tab}&saved=1", status_code=303)

    return routes
