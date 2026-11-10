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

ACTIONS = frozenset(
    {
        "setup-lifecycle",
        "configure-property",
        "dismiss-drop",
        "save-connection",
        "convert-connection",
        "discover",
        "test-model",
        "save-assignment",
        "retry-import",
        "dismiss-import",
        "dismiss-calendar-conflict",
        "enqueue",
        "retry-job",
        "retry-all-failed",
        "reject",
        "approve",
        "resume-approval",
        "correct-contact",
        "correct-association",
        "resume-correction",
    }
)


def _result_message(params: Any, data: dict[str, Any]) -> str:
    action = params.get("result", "")
    if action == "retry-refused":
        return "Retry could not be queued. The note or settings may have changed; check Jobs below."
    if action not in ACTIONS:
        return ""
    if action == "retry-job":
        job = next((j for j in data["jobs"] if j["id"] == params.get("job")), None)
        name = job.get("current_title", job["path"]) if job else "Note"
        message = f"Retry queued for {name}."
        if data["config"]["assignments"]["enrichment"]["paused"]:
            message += " Processing is paused; resume it to start."
        return message
    if action == "retry-all-failed":
        try:
            queued, failed = int(params.get("queued", "0")), int(params.get("failed", "0"))
        except ValueError:
            return ""
        message = f"Retry all failed: {queued} queued."
        if failed:
            message += f" {failed} could not be queued; retry them one at a time to see why."
        if data["config"]["assignments"]["enrichment"]["paused"] and queued:
            message += " Processing is paused; resume it to start."
        return message
    return action.replace("-", " ").capitalize() + " completed."


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
    return (
        f'<label><input type="checkbox" name="{e(name, quote=True)}"{selected}> {e(label)}</label>'
    )


def _select(name: str, label: str, choices: list[tuple[str, str]], selected: str = "") -> str:
    options = "".join(
        f'<option value="{e(value, quote=True)}"'
        + (" selected" if value == selected else "")
        + f">{e(title)}</option>"
        for value, title in choices
    )
    return f'<label>{e(label)}<select name="{name}">{options}</select></label>'


def _speed_note(data: dict[str, Any], assignment: dict[str, Any]) -> str:
    """What the last model test measured, and whether the limits below can be met."""
    speed = data.get("speed", {}).get(f"{assignment['connection']}/{assignment['model']}")
    if not assignment["model"]:
        return ""
    if not speed:
        return (
            '<p class="field-help">Speed not measured yet. Test the model under Connections '
            "to see how long a full reply takes.</p>"
        )
    rate = speed["tokens_per_s"]
    full = assignment["max_output_tokens"] / rate
    text = (
        f"Measured about {rate:g} tokens per second. A full reply of "
        f"{assignment['max_output_tokens']:,} tokens takes about {full:,.0f} seconds."
    )
    if not speed["streamed"] and full > assignment["timeout_s"]:
        return (
            f'<p class="error">{e(text)} This provider sends each reply all at once, so the '
            f"wait limit ({assignment['timeout_s']} seconds) has to cover the whole reply. Raise "
            "the wait limit or lower Maximum response tokens.</p>"
        )
    if speed["streamed"]:
        text += " The app keeps waiting while the model keeps replying."
    return f'<p class="field-help">{e(text)}</p>'


def _outcome_line(data: dict[str, Any]) -> str:
    """How many jobs have produced an answer, failed or gone stale since the start."""
    outcomes = data.get("outcomes") or {"counts": {}, "answered": 0, "last_success": ""}
    counts = outcomes["counts"]
    if not counts:
        return "<p>No enrichment jobs yet.</p>"
    text = (
        f"{outcomes['answered']} of {sum(counts.values())} jobs got an answer from the model "
        f"({counts.get('applied', 0)} applied, {counts.get('suggested', 0)} waiting for review); "
        f"{counts.get('failed', 0)} failed; {counts.get('stale', 0)} out of date."
    )
    if outcomes["last_success"]:
        text += " Last answer: " + _calendar_time(outcomes["last_success"]) + "."
    else:
        text += " No job has succeeded yet."
    return "<p>" + e(text) + "</p>"


def _stale_reason(error: str) -> str:
    if "reviewed note changed" in error:
        return "The reviewed note changed."
    if "source" in error:
        return "The supporting information changed."
    if "date" in error.lower():
        return "The note's date changed."
    return "The note or its supporting information changed."


def _job_status(job: dict[str, Any]) -> str:
    if job["state"] == "stale":
        reason = "Out of date: " + _stale_reason(job["error"])
        next_step = (
            "Retry to use the latest information."
            if job.get("can_retry", True)
            else job.get("retry_reason", "Open Inspect for details.")
        )
        return reason + " " + next_step
    text = job["state"].capitalize() + (": " + job["error"] if job["error"] else "")
    if not job.get("can_retry", True) and job["state"] == "failed":
        text += " " + job.get("retry_reason", "Open Inspect for details.")
    return text


def _limit_link(job: dict[str, Any]) -> str:
    """Point a size failure at the setting that controls it."""
    if not job.get("input_limit"):
        return ""
    return (
        '<br><a href="/admin/workflow?tab=enrichment#processing-limits">'
        "Change Maximum note and transcript characters</a>"
    )


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


def _calendar_time(value: str) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from coppermind_admin.pages.search_index import local_time

    if not value:
        return "Date not recorded"
    stamp = datetime.fromisoformat(value)
    if "T" not in value:
        return stamp.strftime("%B %d, %Y")
    if stamp.tzinfo:
        return local_time(stamp, ZoneInfo("America/Chicago"))
    return stamp.strftime("%b %d, %Y %I:%M %p") + " (floating)"


def _event_count(count: int) -> str:
    return f"{count} event" + ("" if count == 1 else "s")


def _address(value: str) -> str:
    return value[7:] if value.lower().startswith("mailto:") else value


def _contact_label(contact: dict[str, Any], corrections: dict[str, Any]) -> str:
    return corrections.get(contact["id"], {}).get("name") or next(
        (item["name"] or _address(item["address"]) for item in contact["evidence"]),
        "Unnamed contact",
    )


def router(wiring: Wiring) -> APIRouter:
    routes = APIRouter()

    def client(request: Request) -> HttpStoreClient:
        return cast(HttpStoreClient, store_client(request, wiring))

    def page(
        body: str, status: int = 200, *, title: str = "Calendar and enrichment"
    ) -> HTMLResponse:
        from coppermind_admin.main import page as render

        return HTMLResponse(
            render(title, '<div class="context-content">' + body + "</div>"),
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
        heading = "Calendar" if tab == "calendar" else "Calendar and enrichment"
        head = f"""<h1>{heading}</h1><p class="section-links">
<a href="/admin/context?tab=calendar">Calendar and contacts</a>
<a href="/admin/context?tab=models">Connections and model</a>
<a href="/admin/context?tab=review">Suggestions and backlog</a></p>"""
        message = _result_message(request.query_params, data)
        if message:
            style = "notice" if request.query_params.get("result") == "retry-refused" else "success"
            head += f'<p class="{style}">' + e(message) + "</p>"
        if data["worker_error"]:
            head += '<p class="error">' + e(data["worker_error"]) + "</p>"
        for problem in data.get("eligibility_errors", {}).values():
            head += '<p class="notice">' + e(problem) + "</p>"
        if tab == "calendar":
            body = (
                "<p>All exports merge into your one calendar. Add ICS or ZIP files to the "
                '<a href="/admin/workflow?tab=intake">Dropbox folder in your vault</a>. '
                "Originals are retained; importing does not send calendar data to a model.</p>"
                "<h2>Import history</h2><p>200 MB per upload; 41.94 MB expanded ZIP; "
                "100 archive entries. Files must stay unchanged for 30 seconds.</p>"
            )
            imports = data["calendar"]["imports"]
            if not imports:
                body += (
                    '<p class="notice">No import history on this page. '
                    "Add an ICS or ZIP to the Dropbox folder in your vault to begin.</p>"
                )
            else:
                body += "<table><tr><th>File</th><th>Result</th><th>Action</th></tr>"
            for receipt in imports:
                state = receipt["state"]
                label = {
                    "succeeded": "Imported (" + _event_count(receipt.get("event_count", 0)) + ")",
                    "failed": "Import failed",
                    "pending": "Waiting to import",
                }[state]
                if state == "failed" and receipt.get("dismissed"):
                    label += " (dismissed)"
                body += "<tr><td>" + e(receipt["filename"]) + "</td><td>" + e(label)
                if receipt.get("error"):
                    body += ": " + e(receipt["error"])
                body += "</td><td>"
                if state == "failed" and not receipt.get("dismissed"):
                    body += _form("dismiss-import", _hidden("id", receipt["id"]), "Dismiss failure")
                    if not receipt.get("retryable"):
                        body += (
                            "<p>Correct the export in its source calendar and drop it again.</p>"
                        )
                body += "</td></tr>"
            if imports:
                body += "</table>"
            for conflict in data["calendar"].get("conflicts", []):
                body += (
                    "<section><h2>Legacy calendar conflict</h2><p>"
                    + e(
                        (conflict["title"] or "Untitled event")
                        + " · "
                        + _calendar_time(conflict["start"])
                        + " · UID "
                        + conflict["uid"]
                        + " · "
                        + conflict["source_entry"]
                        + f" · Incoming revision: SEQUENCE {conflict['sequence']}, "
                        + f"LAST-MODIFIED {conflict['modified'] or 'not supplied'}"
                        + f" · Stored event: {conflict['stored_title']}, "
                        + f"SEQUENCE {conflict['stored_sequence']}, "
                        + f"LAST-MODIFIED {conflict['stored_modified'] or 'not supplied'}"
                    )
                    + "</p><p>Legacy exports disagree. The current event and original exports "
                    "are retained. Dismiss acknowledges this conflict.</p>"
                )
                body += (
                    _form(
                        "dismiss-calendar-conflict",
                        _hidden("id", conflict["id"]) + _hidden("version", conflict["version"]),
                        "Dismiss conflict",
                    )
                    + "</section>"
                )
            body += "<h2>Events</h2>"
            if data["events"]:
                body += "<table><tr><th>Event</th><th>Date</th><th>State</th></tr>"
            else:
                body += (
                    '<p class="notice">No calendar events on this page. '
                    "Imported events will appear here.</p>"
                )
            for event in data["events"]:
                body += "<tr><td>" + e(event["title"] or "Untitled event")
                people = event.get("people", [])
                if people:
                    body += "<ul>"
                    for person in people:
                        address = _address(person["address"])
                        label = person.get("name") or address
                        if person.get("name") and address:
                            label += " (" + address + ")"
                        role = "Organizer" if person["role"] == "organizer" else "Invitee"
                        body += "<li>" + e(role + ": " + label) + "</li>"
                    body += "</ul>"
                else:
                    body += "<p>No organizer or invitees recorded.</p>"
                body += "<details><summary>Export provenance</summary><ul>"
                for witness in event.get("provenance", []):
                    parts = [witness["filename"]]
                    if witness["entry"] and witness["entry"] != witness["filename"]:
                        parts.append(witness["entry"])
                    if witness["imported_at"]:
                        parts.append(_calendar_time(witness["imported_at"]))
                    if witness.get("legacy_calendar"):
                        parts.append("Previous calendar: " + witness["legacy_calendar"])
                    body += "<li>" + e(" · ".join(parts)) + "</li>"
                body += "</ul></details></td>"
                status = "Cancelled" if event["cancelled"] else "Recorded"
                body += "<td>" + e(_calendar_time(event["start"])) + f"</td><td>{status}</td></tr>"
            if data["events"]:
                body += "</table>"
            body += (
                "<h2>Contacts</h2><p>Calendar invitees are not confirmed attendees. "
                "Matching email addresses share an identity; names alone do not merge contacts.</p>"
            )
            if not data["contacts"]:
                body += (
                    '<p class="notice">No calendar contacts on this page. '
                    "Contacts come from organizers and invitees in your exports.</p>"
                )
            event_lookup = {ev.get("id"): ev for ev in data["events"]}
            for contact in data["contacts"]:
                correction = data["contact_corrections"].get(contact["id"], {})
                evidence = contact["evidence"]
                body += (
                    "<details><summary>"
                    + e(_contact_label(contact, data["contact_corrections"]))
                    + "</summary>"
                )
                body += (
                    "<p>"
                    + e(", ".join(sorted({_address(item["address"]) for item in evidence})))
                    + "</p><ul>"
                )
                for item in evidence:
                    event = event_lookup.get(item["event_id"], {})
                    title = item.get("event_title") or event.get("title") or "Untitled event"
                    start = item.get("event_start") or event.get("start", "")
                    role = "Organizer" if item["role"] == "organizer" else "Invitee"
                    body += (
                        "<li>" + e(role + ": " + title + " · " + _calendar_time(start)) + "</li>"
                    )
                body += "</ul>"
                total = contact.get("evidence_count", len(evidence))
                if total > len(evidence):
                    body += f"<p>Showing {len(evidence)} of {total} retained evidence records.</p>"
                linked = [
                    identity
                    for identity, assoc in data["associations"].items()
                    if contact["id"] in assoc["contacts"]
                ]
                body += (
                    "<p>Approved notes: "
                    + (
                        ", ".join(
                            f'<a href="/admin/notes/{quote(identity, safe="")}">{e(identity)}</a>'
                            for identity in linked
                        )
                        or "None"
                    )
                    + "</p>"
                )
                fields = _hidden("id", contact["id"]) + _input(
                    "name", "Corrected name", correction.get("name", "")
                )
                choices = [("", "No linked contact")] + [
                    (other["id"], _contact_label(other, data["contact_corrections"]))
                    for other in data["contacts"]
                    if other["id"] != contact["id"]
                ]
                alias = correction.get("same_as", "")
                if alias and alias not in {value for value, _ in choices}:
                    choices.append((alias, "Previously linked contact"))
                fields += _select("same_as", "Same person as", choices, alias)
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
                provider = {
                    "anthropic": "Anthropic",
                    "openai": "OpenAI",
                    "compatible": "OpenAI-compatible",
                }[connection["provider"]]
                label = (
                    e(connection["name"]) + " <small>" + provider + "</small>"
                    if connection["id"]
                    else "Add connection"
                )
                body += '<details class="connection-card"><summary>' + label + "</summary>"
                body += "<p>Choose a name you recognize. Coppermind manages its internal ID.</p>"
                fields = _hidden("id", connection["id"])
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
                    if connection["id"] not in config.get("automatic_connection_ids", []):
                        body += (
                            "<p>This connection uses a manually chosen ID. "
                            "Conversion preserves its settings and history.</p>"
                        )
                        body += _form(
                            "convert-connection",
                            _hidden("connection", connection["id"]),
                            "Convert to automatic ID",
                            revision=revision,
                        )
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
                    if not model_ids:
                        body += (
                            "<p>No models discovered. Refresh the list before choosing a model.</p>"
                        )
                    if model_ids:
                        body += _form("test-model", fields, "Test model with sample meeting text")
                body += "</details>"
            assignment = config["assignments"]["enrichment"]
            fields = "<fieldset><legend>1. Choose a model</legend>"
            options = '<option value="">Choose a connection</option>'
            for item in config["connections"]:
                models = data["discovery"].get(item["id"], [])
                selected = " selected" if item["id"] == assignment["connection"] else ""
                options += (
                    f'<option value="{e(item["id"], quote=True)}"{selected} '
                    f'data-models="{e(json.dumps(models), quote=True)}">'
                    + e(item["name"])
                    + "</option>"
                )
            fields += (
                '<label>Connection<select name="connection" id="enrichment-connection">'
                + options
                + "</select></label>"
            )
            model_ids = data["discovery"].get(assignment["connection"], [])
            choices = [(item, item) for item in model_ids]
            if assignment["model"] and assignment["model"] not in model_ids:
                fields += (
                    '<p class="notice">The saved model is not in discovery. Refresh or choose '
                    "another.</p>"
                )
            fields += _select(
                "model", "Model", [("", "Choose a discovered model"), *choices], assignment["model"]
            )
            fields += (
                "<p>Expand the connection above to refresh models and test your choice "
                "before enabling.</p></fieldset>"
            )
            schema_fields = data.get("enrichment_fields", [])
            fields += (
                "<fieldset><legend>2. Suggested metadata</legend><p>Select fields the model "
                "may fill when blank. Confident suggestions appear in Review notes "
                "for your review.</p>"
            )
            for item in schema_fields:
                fields += _check(
                    "field_" + item["name"],
                    item["name"] + " (" + item["kind"] + ")",
                    item["name"] in assignment["fields"],
                )
            fields += (
                '<p><a href="/admin/fields">Manage fields and tags</a> to define additional '
                "fields.</p></fieldset>"
            )
            fields += "<fieldset><legend>3. Calendar and contact links (optional)</legend>"
            for key, label, kind, help_text in [
                (
                    "attendees_field",
                    "Confirmed attendees",
                    "list",
                    "Email addresses supported by note or transcript evidence.",
                ),
                (
                    "invitees_field",
                    "Calendar invitees",
                    "list",
                    "Invited email addresses. An invitation does not prove attendance.",
                ),
                (
                    "event_field",
                    "Calendar event reference",
                    "string",
                    "The identifier of the matched calendar event.",
                ),
                (
                    "contacts_field",
                    "Contact references",
                    "list",
                    "Identifiers linking the note to known contact records.",
                ),
            ]:
                choices = [
                    (item["name"], item["name"]) for item in schema_fields if item["kind"] == kind
                ]
                if assignment[key] and assignment[key] not in {value for value, _ in choices}:
                    choices.append(
                        (assignment[key], assignment[key] + " (no longer eligible; choose another)")
                    )
                fields += _select(
                    key, label, [("", "Do not write this linkage"), *choices], assignment[key]
                )
                fields += '<p class="field-help">' + help_text + "</p>"
            fields += (
                '</fieldset><details class="connection-card"><summary>Advanced limits</summary>'
            )
            for key, label, help_text in [
                (
                    "timeout_s",
                    "Wait limit (seconds)",
                    "Default 120. How long the model may stay silent, before it starts "
                    "and while replying. A reply that keeps coming is not cut off, up to one hour.",
                ),
                (
                    "max_input_chars",
                    "Maximum context size (characters)",
                    "Default 60,000. Oversized context fails before sending; no silent truncation.",
                ),
                (
                    "max_output_tokens",
                    "Maximum response length (tokens)",
                    "Default 2,048. Limits how much the model can return.",
                ),
                (
                    "attempts",
                    "Total attempts per job",
                    "Default 2, including the first call. Retrying may incur another charge.",
                ),
            ]:
                fields += _input(key, label, str(assignment[key]), kind="number")
                fields += '<p class="field-help">' + help_text + "</p>"
            fields += _speed_note(data, assignment)
            fields += (
                '</details><fieldset id="automatic-processing">'
                "<legend>4. Automatic processing</legend>"
            )
            fields += _check(
                "enabled", "Enable enrichment for new unreviewed notes", assignment["enabled"]
            )
            fields += _check("paused", "Pause model calls", assignment["paused"])
            fields += "</fieldset>"
            body += (
                '<h2>Enrichment setup</h2><p class="notice">When enabled, '
                "selected note text, associated transcripts, relevant dossiers and nearby "
                "calendar/contact records "
                "are sent to this provider. Existing notes require an explicit backlog selection; "
                "existing notes that were never sent are not swept when reviewed. "
                "Calls run one at a time. Keys stay outside the vault and Git.</p>"
                '<p><a href="/admin/context?tab=review">Select existing notes for enrichment</a>. '
                "Suggestions with confidence of at least 0.8 fill blank fields in the "
                "review folder. "
                "Lower confidence stays here; bodies and filenames stay unchanged.</p>"
            )
            body += _form("save-assignment", fields, "Save enrichment settings", revision=revision)
            body += """<script>
const picker = document.getElementById('enrichment-connection');
picker.addEventListener('change', () => {
  const form = picker.closest('form');
  const models = JSON.parse(picker.selectedOptions[0].dataset.models || '[]');
  form.elements.model.replaceChildren(new Option('Choose a discovered model', ''));
  for (const model of models) form.elements.model.add(new Option(model, model));
});
</script>"""
        else:
            body = (
                "<p>Confident suggestions fill blank fields on notes in the review folder "
                "automatically. Other suggestions need your approval, and applying any "
                "change leaves the note unreviewed.</p>"
            )
            assignment = config["assignments"]["enrichment"]
            try:
                backlog = await client(request).context_action(
                    "backlog", {"cursor": request.query_params.get("cursor")}
                )
            except StoreError as exc:
                return error(exc)
            body += "<h2>Jobs</h2>"
            if assignment["paused"]:
                body += (
                    '<p class="notice">Processing is paused. Queued retries will wait until '
                    'you resume. <a href="/admin/context?tab=models#automatic-processing">'
                    "Resume processing</a></p>"
                )
            if data["jobs"]:
                body += _outcome_line(data)
                if any(
                    job["state"] == "failed" and job.get("can_retry", True) for job in data["jobs"]
                ):
                    body += (
                        "<p>Retry all failed notes with current settings and evidence. "
                        "A retry may repeat a request that was already billed.</p>"
                    )
                    body += _form("retry-all-failed", "", "Retry all failed")
                body += "<table><tr><th>Note</th><th>Status</th><th>Actions</th></tr>"
                for job in data["jobs"]:
                    state = job["state"]
                    path = job.get("current_path", job["path"])
                    title = job.get("current_title", path.rsplit("/", 1)[-1].removesuffix(".md"))
                    body += f"<tr><td>{e(title)}<br>{e(path)}"
                    if path != job["path"]:
                        body += "<br><small>Moved from " + e(job["path"]) + "</small>"
                    body += "</td><td>"
                    can_retry = job.get("can_retry", True)
                    body += e(_job_status(job)) + _limit_link(job)
                    body += "</td><td>"
                    if state == "stale" and not assignment["enabled"]:
                        body += (
                            '<a href="/admin/context?tab=models#automatic-processing">'
                            "Enable enrichment</a><br>"
                        )
                    if state in {"failed", "stale"} and can_retry:
                        body += (
                            "<p>Retry with the latest information. "
                            "This may repeat a billed request.</p>"
                        )
                        body += _form("retry-job", _hidden("id", job["id"]), "Retry")
                    body += f'<a href="/admin/context/jobs/{job["id"]}">Inspect</a></td></tr>'
                body += "</table>"
            else:
                body += (
                    "<p>No jobs yet; select notes below to enrich them.</p>"
                    if backlog["notes"]
                    else "<p>No jobs or unreviewed notes yet.</p>"
                )
            body += "<h2>Existing unreviewed notes</h2>"
            if backlog["notes"]:
                fields = "".join(
                    _check("note_" + note["id"], note["path"]) for note in backlog["notes"]
                )
                fields += _check("confirm_send", "Send selected notes for enrichment")
                body += _form("enqueue", fields, "Queue selected notes")
            elif data["jobs"]:
                body += "<p>No unreviewed notes available on this page.</p>"
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
        return page(head + body, title=heading)

    @routes.get("/admin/context/jobs/{identity}", include_in_schema=False)
    async def job_page(identity: str, request: Request) -> Response:
        try:
            job = await client(request).context_action("job", {"id": identity})
        except StoreError as exc:
            return error(exc)
        body = (
            "<h1>Review suggestions</h1><p>"
            + e(job["current"].get("path", job["path"]))
            + "</p><p>"
            + e(_job_status(job))
            + "</p>"
            + _limit_link(job)
        )
        if (
            job.get("retry_after")
            and job["state"] in {"failed", "queued"}
            and job["attempts"] < job["attempt_limit"]
        ):
            body += (
                "<p>The app is waiting before it tries again. Next automatic try after "
                + e(_calendar_time(job["retry_after"]))
                + ".</p>"
            )
        if job.get("reply"):
            body += (
                "<details><summary>What the model replied (the first 4,000 characters)"
                "</summary><pre>" + e(job["reply"]) + "</pre></details>"
            )
        body += (
            "<details><summary>Technical details</summary><p>Current metadata</p><pre>"
            + e(json.dumps(job["current"]["frontmatter"], indent=2))
            + "</pre><p>Job status: "
            + e(job["state"] + ": " + job["error"])
            + "</p></details>"
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
                    + f" (confidence: {suggestion.get('confidence', 0):.2f})"
                    + "</p><blockquote>"
                    + e(suggestion["quote"])
                    + "</blockquote>"
                )
                body += (
                    "<details><summary>Technical details</summary><pre>"
                    + e(
                        json.dumps(
                            {
                                "events": suggestion["event_ids"],
                                "contacts": suggestion["contact_ids"],
                            },
                            indent=2,
                        )
                    )
                    + "</pre></details>"
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
        if job["state"] == "suggested":
            body += (
                "<p>Apply the suggestions you want. "
                "A date change requires fresh calendar matching.</p>"
            )
            body += _form("reject", _hidden("id", identity), "Reject proposal")
        if job["state"] in {"failed", "stale"} and job["newest"] and job.get("can_retry", True):
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
        if job.get("history"):
            body += "<details><summary>Earlier jobs</summary><ul>"
            for earlier in job["history"]:
                stamp = _calendar_time(earlier["created_at"])
                label = f"{earlier['path']}: {earlier['state']} ({stamp})"
                body += f'<li><a href="/admin/context/jobs/{earlier["id"]}">{e(label)}</a></li>'
            body += "</ul></details>"
        body += "<details><summary>Technical details: approved associations</summary>"
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
        return page(body + '</details><p><a href="/admin/context?tab=review">Back to Jobs</a></p>')

    @routes.post("/v1/admin/context/{action}", include_in_schema=False)
    async def action(action: str, request: Request) -> Response:
        from coppermind_admin.main import submitted

        form = await submitted(request)
        try:
            payload: dict[str, Any] = {key: value for key, value in form.items() if key != "csrf"}
            payload["revision"] = int(form.get("revision", "0"))
            if action in {"setup-lifecycle", "dismiss-drop", "configure-property"}:
                pass
            elif action == "save-connection":
                payload["connection"] = {
                    key: form.get(key, "") for key in ("id", "name", "provider", "base_url")
                }
                payload.pop("api_key", None)
                if form.get("api_key") or "clear_key" in form:
                    payload["api_key"] = "" if "clear_key" in form else form["api_key"]
            elif action == "test-model":
                payload["model"] = form.get("model", "")
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
                assignment["model"] = form.get("model", "")
                assignment["fields"] = [
                    key.removeprefix("field_") for key in form if key.startswith("field_")
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
            if action not in ACTIONS:
                raise ValueError("unknown action")
            result = await client(request).context_action(action, payload)
            if action == "retry-import" and result.get("state") == "failed":
                return error(
                    ValueError(
                        result.get("error")
                        or "The import still failed. Correct the export and drop it again."
                    )
                )
        except (StoreError, ValueError, KeyError) as exc:
            if action == "retry-job":
                return RedirectResponse(
                    "/admin/context?tab=review&result=retry-refused", status_code=303
                )
            return error(exc)
        if action == "configure-property":
            return RedirectResponse("/admin/workflow?tab=enrichment&saved=1", status_code=303)
        if action in {"setup-lifecycle", "dismiss-drop"}:
            return RedirectResponse("/admin/workflow?tab=intake&saved=1", status_code=303)
        tab = (
            "models"
            if action
            in {
                "save-connection",
                "convert-connection",
                "discover",
                "test-model",
                "save-assignment",
            }
            else "review"
            if action
            in {"enqueue", "retry-job", "retry-all-failed", "reject", "approve", "resume-approval"}
            else "calendar"
        )
        query = f"tab={tab}&result={action}"
        if action == "retry-job":
            query += "&job=" + quote(result["job"], safe="")
        if action == "retry-all-failed":
            query += f"&queued={int(result['queued'])}&failed={len(result['errors'])}"
        return RedirectResponse(f"/admin/context?{query}", status_code=303)

    return routes
