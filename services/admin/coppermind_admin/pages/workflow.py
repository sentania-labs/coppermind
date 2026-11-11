"Task-oriented lifecycle, intake and review pages using the shared store operations."

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime
from html import escape as e
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from coppermind.context_config import FeatureAssignment
from coppermind.settings import Wiring
from coppermind.store_protocol import PatchFrontmatter, StoreError
from coppermind_admin.pages.context import (
    _calendar_time,
    _check,
    _event_count,
    _form,
    _hidden,
    _input,
    _outcome_line,
    _select,
    _speed_note,
)
from coppermind_admin.pages.problems import store_client


def router(wiring: Wiring) -> APIRouter:
    routes = APIRouter()

    def client(request):
        return store_client(request, wiring)

    def page(title: str, body: str, status: int = 200) -> HTMLResponse:
        from coppermind_admin.main import page as document

        nav = (
            '<p class="section-links">'
            + "".join(
                f'<a href="/admin/workflow?tab={key}">{label}</a>'
                for key, label in [
                    ("setup", "Workflow"),
                    ("intake", "Imports"),
                    ("enrichment", "Suggestions"),
                    ("review", "Review status"),
                    ("dossiers", "People"),
                ]
            )
            + "</p>"
        )
        return HTMLResponse(document(title, "<h1>" + e(title) + "</h1>" + nav + body), status)

    @routes.get("/admin/workflow", include_in_schema=False)
    async def workflow(request: Request) -> Response:
        try:
            data = await client(request).context_action("snapshot", {})
            config = data["config"]
            tab = request.query_params.get("tab", "setup")
            body = '<p class="success">Saved.</p>' if request.query_params.get("saved") else ""
            if data.get("worker_error"):
                body += '<p class="error">' + e(data["worker_error"]) + "</p>"
            if tab == "setup":
                title = "Workflow setup"
                body += "<p>New note → suggestions → your review → filing and confirmed facts.</p>"
                body += (
                    "<p>Reviewed notes file under a folder named for their type, then year "
                    "and month. After this upgrade, existing reviewed notes move once "
                    "automatically from the old notes folder into their type folders as filing "
                    "runs. Links by note name keep working, and Coppermind updates its own "
                    "links. If you wrote a link with the full old folder path, update it by hand. "
                    "Changing a folder setting does not move existing files.</p>"
                )
                if not config.get("lifecycle_enabled"):
                    body += (
                        "<h2>Set up the workflow</h2><p>Add optional attendees, invitees, cal"
                        "endar reference and work meeting properties. Existing notes and prop"
                        "erty values stay unchanged.</p>"
                    )
                    body += _form(
                        "setup-lifecycle",
                        _input("folder", "Dropbox folder", config.get("dropbox_folder", "Dropbox")),
                        "Enable note workflow",
                        revision=config["revision"],
                    )
                else:
                    body += (
                        '<p class="success">Note workflow enabled. Work notes need a start'
                        " time in date or an explicit Time unknown choice before filing.</p>"
                    )
                body += (
                    '<h2>Review in Obsidian</h2><ol><li><a href="/admin/workflow/plugin.z'
                    'ip">Download the companion plugin</a>.</li><li>Extract its folder in'
                    "to your vault’s .obsidian/plugins directory and enable Coppermind Re"
                    "view in Community plugins.</li><li>In plugin settings, enter this se"
                    "rver address and select an API credential with notes:read, notes:wri"
                    "te and sources:read.</li><li>Open a note and run “Coppermind Review:"
                    " Review current note”.</li></ol><p>Requires Obsidian 1.11 or later. "
                    "Credentials use Obsidian SecretStorage; only the secret name is stor"
                    "ed in plugin settings.</p>"
                )
                body += (
                    '<p><a href="/admin/properties">Configure note properties</a> · <a hr'
                    'ef="/admin/workflow?tab=enrichment">Choose enrichment behavior</a></'
                    "p>"
                )
            elif tab == "intake":
                title = "Imports"
                dropbox = data.get("dropbox", {"folder": "Dropbox", "items": [], "calendars": []})
                body += (
                    "<p>Drop ICS or ZIP files containing ICS into <strong>"
                    + e(dropbox["folder"])
                    + (
                        "</strong> in Obsidian. Markdown follows the normal note workflow. Ot"
                        "her files are preserved and reported as unsupported.</p>"
                    )
                )
                body += (
                    '<p class="muted">Files must stay unchanged for 30 seconds. '
                    "Intake limit: 200 MB. ZIP limit: 41.94 MB expanded, "
                    "100 entries. No files are deleted.</p>"
                    '<p>Every export merges into <a href="/admin/context?tab=calendar">'
                    "your calendar</a>. Event details show which exports witnessed it. "
                    "Completed receipts clear automatically; import history and originals "
                    "remain. Failed receipts stay until you dismiss them.</p>"
                )
                if any(
                    item["state"] in {"pending", "importing", "stabilizing"}
                    for item in dropbox["items"]
                ):
                    body += (
                        '<p role="status">Imports run in the background. '
                        '<a href="/admin/workflow?tab=intake">Refresh progress</a>.</p>'
                    )
                for item in dropbox["items"]:
                    body += (
                        '<section class="connection-card"><h2>'
                        + e(item["filename"])
                        + "</h2><p>"
                        + e(
                            {
                                "stabilizing": (
                                    "Waiting for the file to settle (30 seconds without changes)"
                                ),
                                "pending": "Waiting to import",
                                "importing": "Importing",
                                "succeeded": "Imported",
                                "already_imported": "Already imported",
                                "failed": "Import failed",
                                "unsupported": "Unsupported file type",
                            }.get(item["state"], "Processing")
                        )
                        + "</p>"
                    )
                    if item["state"] in {"succeeded", "already_imported"}:
                        body += "<p>" + e(_event_count(item.get("event_count", 0))) + "</p>"
                        if item.get("first_imported_at"):
                            body += (
                                "<p>First imported: "
                                + e(_calendar_time(item["first_imported_at"]))
                                + "</p>"
                            )
                    if item["error"]:
                        body += '<p class="error">' + e(item["error"]) + "</p>"
                    if item["state"] in {"failed", "succeeded", "already_imported", "unsupported"}:
                        body += _form("dismiss-drop", _hidden("id", item["id"]), "Dismiss receipt")
                    if item["state"] == "failed" and not item.get("retryable"):
                        body += (
                            '<p>Review <a href="/admin/context?tab=calendar">import '
                            "history</a>, correct the export and drop it again.</p>"
                        )
                    body += "</section>"
                if not dropbox["items"]:
                    body += (
                        '<p class="notice">No receipts to show. '
                        "Add an ICS or ZIP file to the "
                        + e(dropbox["folder"])
                        + " folder in your Obsidian vault to import it.</p>"
                    )
                body += (
                    "<details><summary>Calendar and import history</summ"
                    'ary><a href="/admin/context?tab=calendar">Inspect calendar records</'
                    "a></details>"
                )
            elif tab == "enrichment":
                title = "Suggestions"
                assignment = {
                    **FeatureAssignment().model_dump(),
                    **config["assignments"]["enrichment"],
                }
                body += (
                    "<p>Make suggestions once for each new unreviewed note. Editing a not"
                    "e does not repeatedly call the model.</p>"
                ) + _outcome_line(data)
                body += (
                    '<p><a href="/admin/context?tab=models">'
                    "Manage connections and test models</a></p>"
                )
                fields = "<fieldset><legend>Where suggestions run</legend>"
                fields += _select(
                    "connection",
                    "Connection",
                    [
                        ("", "Choose a connection"),
                        *[(c["id"], c["name"]) for c in config["connections"]],
                    ],
                    assignment["connection"],
                )
                # Per-connection options let a single selection update the available models.
                models = data["discovery"].get(assignment["connection"], [])
                fields += _select(
                    "model",
                    "Model",
                    [("", "Choose a discovered model"), *[(m, m) for m in models]],
                    assignment["model"],
                )
                fields += (
                    '<p class="muted">Test this model under Connections before enabling e'
                    "nrichment.</p></fieldset>"
                )
                fields += (
                    "<fieldset><legend>What should it suggest?</legend><p>Suggestions with "
                    "confidence of at least 0.8 fill blank selected fields in the review folder. "
                    "Review them in Obsidian. "
                    "Lower-confidence suggestions stay in Admin. Bodies and filenames stay "
                    "unchanged. Reviewed notes contribute dossier facts automatically.</p>"
                )
                mapped = {
                    assignment[k]
                    for k in ("attendees_field", "invitees_field", "event_field", "contacts_field")
                }
                for field in data["enrichment_fields"]:
                    if field["name"] not in mapped:
                        fields += _check(
                            "field_" + field["name"],
                            field["name"],
                            field["name"] in assignment["fields"],
                        )
                fields += "</fieldset><fieldset><legend>People and calendar associations</legend>"
                for key, label, preset in [
                    ("attendees_field", "Confirmed attendees", "attendees"),
                    ("invitees_field", "Invited people", "invitees"),
                    ("event_field", "Calendar reference", "calendar_event"),
                    ("contacts_field", "Contact references", "contacts"),
                ]:
                    name = assignment[key]
                    fields += (
                        "<p><strong>"
                        + label
                        + "</strong>: "
                        + (e(name) if name else "Not configured")
                        + "</p>"
                    )
                    fields += _hidden(key, name)
                    if not name:
                        # External form avoids nesting a configuration mutation inside
                        # the unsaved assignment form. All other fields remain unchanged.
                        form_id = "setup-" + preset
                        fields += (
                            f'<button type="submit" form="{form_id}">'
                            f"Set up {e(label.lower())}</button>"
                        )
                        body += _form(
                            "configure-property",
                            _hidden("property", preset),
                            "Create and use " + preset,
                            revision=config["revision"],
                        ).replace("<form ", f'<form id="{form_id}" hidden ')
                fields += "</fieldset><fieldset><legend>When to run</legend>"
                fields += _check(
                    "enabled", "Automatically enrich new unreviewed notes", assignment["enabled"]
                )
                fields += _check("paused", "Pause processing", assignment["paused"])
                fields += (
                    '<p><a href="/admin/workflow?tab=review">Choose existing notes separa'
                    "tely</a></p></fieldset>"
                )
                fields += '<details id="processing-limits"><summary>Processing limits</summary>'
                for key, label in [
                    ("timeout_s", "Wait limit (seconds, how long the model may stay silent)"),
                    ("max_input_chars", "Maximum note and transcript characters"),
                    ("max_output_tokens", "Maximum response tokens"),
                    ("summary_max_input_chars", "Maximum summary input characters"),
                    ("summary_timeout_s", "Summary wait limit (seconds)"),
                    ("attempts", "Maximum attempts"),
                ]:
                    fields += _input(key, label, str(assignment[key]), kind="number")
                fields += _speed_note(data, assignment)
                fields += "</details>"
                body += _form(
                    "save-assignment", fields, "Save enrichment", revision=config["revision"]
                )
                discovery = json.dumps(data["discovery"]).replace("<", "\\u003c")
                body += (
                    "<script>const models="
                    + discovery
                    + (
                        ';const connection=document.querySelector("select[name=connection]");'
                        'connection.addEventListener("change",()=>{const select=document.quer'
                        'ySelector("select[name=model]");select.replaceChildren(new Option("C'
                        'hoose a discovered model",""));for(const name of models[connection.v'
                        "alue]||[])select.add(new Option(name,name));});</script>"
                    )
                )
            elif tab == "dossiers":
                title = "People"
                body += (
                    "<p>Read people and companies in Obsidian under People/ and Companies/. "
                    "Edit a fact there to correct it. This view is for setup and repair.</p>"
                )
                voices = (
                    await client(request).entity_action("voice-list", {})
                    if any(e["kind"] == "person" for e in data.get("entities", []))
                    else {}
                )
                for kind, heading in (("person", "People"), ("company", "Companies")):
                    count = sum(e["kind"] == kind for e in data.get("entities", []))
                    body += "<h2>" + heading + f" ({count})</h2>"
                    for entity in data.get("entities", []):
                        if entity["kind"] != kind:
                            continue
                        body += (
                            '<details class="connection-card" '
                            'style="overflow-wrap:anywhere"><summary>' + e(entity["name"])
                        )
                        body += " (retired)" if entity.get("retired_at") else ""
                        body += "</summary><p>" + e(entity["path"]) + "</p>"
                        if (
                            kind == "person"
                            and not entity.get("retired_at")
                            and not entity.get("redirect")
                        ):
                            voice = voices.get(entity["id"]) or {
                                "enabled": False,
                                "signatures": [],
                            }
                            body += (
                                "<h3>Voice recognition</h3><p>Off by default. Voice signatures "
                                "are stored, not audio clips. Unidentified signatures are held "
                                "privately with the recording until review and removal.</p>"
                            )
                            body += "<p>" + str(len(voice["signatures"])) + " saved signatures</p>"
                            for signature in voice["signatures"]:
                                body += (
                                    "<p>"
                                    + e(signature["created_at"][:10])
                                    + ' · <a href="/admin/review/'
                                    + quote(signature["note_id"])
                                    + '">Source note</a></p>'
                                )
                            body += (
                                '<form method="post" action="/v1/admin/voice/set">'
                                + _hidden("id", entity["id"])
                                + _hidden("enabled", "false" if voice["enabled"] else "true")
                                + "<button>"
                                + ("Turn off" if voice["enabled"] else "Turn on")
                                + "</button></form>"
                            )
                            if voice["signatures"]:
                                body += (
                                    '<form method="post" action="/v1/admin/voice/delete" '
                                    'onsubmit="return confirm(&quot;Delete all saved '
                                    "voice signatures for this person? "
                                    'This cannot be undone.&quot;)">'
                                    + _hidden("id", entity["id"])
                                    + '<button class="danger">Delete signatures</button></form>'
                                )
                        if entity.get("problem"):
                            body += '<p class="error">' + e(entity["problem"]) + "</p>"
                        if entity.get("aliases"):
                            body += "<p>Aliases: " + e(", ".join(entity["aliases"])) + "</p>"
                        for section in ("Facts", "Relationships", "Interests"):
                            members = ""
                            for fact in entity["facts"]:
                                category = (
                                    "Relationships"
                                    if fact.get("target")
                                    else "Interests"
                                    if fact["predicate"] == "interest"
                                    else "Facts"
                                )
                                if category != section or fact["state"] != "confirmed":
                                    continue
                                labels = {
                                    "role": "Role",
                                    "organization": "Works at",
                                    "relationship": "Relationship",
                                    "mentioned": "Mentioned in",
                                    "note": "Note",
                                }
                                label = labels.get(
                                    fact["predicate"],
                                    fact["predicate"].replace("_", " ").capitalize(),
                                )
                                members += "<li>" + e(
                                    fact["value"]
                                    if section == "Interests"
                                    else label + ": " + fact["value"]
                                )
                                evidence = fact["evidence"]
                                if evidence.get("note_id"):
                                    members += (
                                        ' · <a href="/admin/review/'
                                        + quote(evidence["note_id"])
                                        + '">'
                                        + e(evidence["path"])
                                        + "</a>"
                                    )
                                else:
                                    members += " · " + e(evidence["path"])
                                if evidence.get("quote"):
                                    members += (
                                        "<blockquote>" + e(evidence["quote"]) + "</blockquote>"
                                    )
                                members += "</li>"
                            if members:
                                body += "<h3>" + section + "</h3><ul>" + members + "</ul>"
                        body += "</details>"
                if not data.get("entities"):
                    body += (
                        '<p class="notice">No dossiers yet. Reviewed notes contribute people.</p>'
                    )
            else:
                title = "Review status"
                body += (
                    "<p>Review and correct notes in Obsidian, optionally using the companion. "
                    "Checking Reviewed files the note. The links below offer an Admin fallback.</p>"
                )
                for job in data["jobs"]:
                    body += (
                        '<p><a href="/admin/review/'
                        + quote(job["note_id"])
                        + '">'
                        + e(job["path"])
                        + "</a> · "
                        + e(job["state"])
                        + "</p>"
                    )
                body += "<h2>Existing notes</h2>"
                backlog = await client(request).context_action(
                    "backlog", {"cursor": request.query_params.get("cursor")}
                )
                if not backlog["notes"]:
                    body += (
                        "<p>No notes are waiting for review. "
                        "New notes appear here after they arrive.</p>"
                    )
                if not config["assignments"]["enrichment"]["enabled"]:
                    body += (
                        '<p class="notice">Enrichment is off. <a href="/admin/workflow?tab=en'
                        'richment">Finish enrichment setup</a> to queue notes. You can still '
                        "review notes without suggestions.</p>"
                    )
                elif backlog["notes"]:
                    fields = "".join(_check("note_" + n["id"], n["path"]) for n in backlog["notes"])
                    fields += _check("confirm_send", "Send selected notes for enrichment")
                    body += _form("enqueue", fields, "Enrich selected notes")
                for note in backlog["notes"]:
                    body += (
                        '<p><a href="/admin/review/'
                        + quote(note["id"])
                        + '">'
                        + e(note["path"])
                        + "</a></p>"
                    )
                if backlog.get("cursor"):
                    body += (
                        '<a href="/admin/workflow?tab=review&amp;cursor='
                        + quote(backlog["cursor"], safe="")
                        + '">Next notes</a>'
                    )
                for followup in data.get("calendar_followups", {}).values():
                    body += (
                        '<p class="notice"><a href="/admin/review/'
                        + quote(followup["note_id"])
                        + '">Review calendar update</a>: '
                        + e(followup["message"])
                        + "</p>"
                    )
            return page(title, body)
        except (StoreError, ValueError) as exc:
            return page("Workflow unavailable", '<p class="error">' + e(str(exc)) + "</p>", 503)

    @routes.get("/admin/review/{identity}", include_in_schema=False)
    async def review(identity: str, request: Request) -> Response:
        try:
            data = await client(request).context_action("lifecycle", {"note_id": identity})
            note = data["note"]
            body = (
                "<h2>"
                + e(note["title"])
                + '</h2><div class="review-grid"><article><h3>Note</h3><pre>'
                + e(note["body"])
                + "</pre><details><summary>Current properties</summary><pre>"
                + e(json.dumps(note["frontmatter"], indent=2))
                + "</pre></details></article><section><h3>Suggested changes</h3>"
            )
            from coppermind.schema import format_note_time, note_date, note_start_time
            from coppermind_admin.pages.fields import read_schema

            _, schema = read_schema(request.app.state.control)
            type_key = data["roles"]["type_key"]
            kind = str(note["frontmatter"].get(type_key, ""))
            definition = next(k for k in schema.keys if k.name == type_key)
            fallback_day = note_date(note["frontmatter"], schema)
            start = note_start_time(note["frontmatter"], schema)
            current_time = (
                format_note_time(datetime.combine(fallback_day, start))
                if fallback_day is not None and start is not None
                else fallback_day.isoformat()
                if fallback_day is not None
                else ""
            )
            properties = _hidden("note_id", identity) + _hidden("if_match", note["content_hash"])
            properties += _input(
                "date",
                "Date, or date and start time (YYYY-MM-DDTHH:MM:SS)",
                current_time,
            )
            type_choices = [(v, v.title()) for v in definition.vocabulary]
            if kind and kind not in definition.vocabulary:
                type_choices.append((kind, kind))
            properties += _select("note_type", "Note type", type_choices or [(kind, kind)], kind)
            if kind == "work":
                properties += _check(
                    "time_unknown", "Time unknown", note["frontmatter"].get("time_unknown") is True
                )
            body += "<details><summary>Confirm date and meeting time</summary>"
            body += (
                '<form method="post" action="/v1/admin/workflow/save-properties">'
                + properties
                + "<button>Save properties</button></form></details>"
            )
            if data.get("review_problem"):
                body += '<p class="notice">' + e(data["review_problem"]) + "</p>"
            if note["frontmatter"].get(data["roles"]["reviewed_key"]):
                body += '<p class="success">Reviewed. The final note is authoritative.</p>'
            else:
                if data["stale"]:
                    body += (
                        '<p class="notice">The note or evidence changed. Finish without old s'
                        "uggestions, or request fresh ones.</p>"
                    )
                fields = (
                    _hidden("note_id", identity)
                    + _hidden("if_match", note["content_hash"])
                    + _hidden("job_id", data["job_id"] or "")
                )
                for suggestion in data["suggestions"]:
                    fields += '<div class="connection-card">'
                    if not data["stale"]:
                        fields += _check(
                            "selected_" + str(suggestion["index"]),
                            suggestion["field"] or suggestion["kind"],
                        )
                    fields += (
                        "<p>"
                        + e(suggestion.get("display_value", str(suggestion["value"])))
                        + "</p><p>"
                        + e(suggestion["reason"])
                        + f" (confidence: {suggestion.get('confidence', 0):.2f})"
                        + "</p><blockquote>"
                        + e(suggestion["quote"])
                        + "</blockquote></div>"
                    )
                if any(s["kind"] == "filename" for s in data["suggestions"]):
                    fields += _check(
                        "accept_link_risk",
                        "Allow selected filename changes (existing links may need repair)",
                    )
                fields += (
                    "<p>Unchecked suggestions are dismissed. To keep your own edits, leav"
                    "e all unchecked.</p>"
                )
                fields += _hidden("confirmation", "Complete review")
                body += (
                    '<form method="post" action="/v1/admin/workflow/complete-review">'
                    + fields
                    + (
                        "<button>Apply selected and mark reviewed</button></form>"
                        if data["suggestions"] and not data["stale"]
                        else "<button>Mark reviewed without changes</button></form>"
                    )
                )
                body += (
                    '<p class="muted">For work notes, enter date as YYYY-MM-DDTHH:MM:SS or'
                    " time_unknown: true in Obsidian before finishing.</p>"
                )
                body += _form(
                    "enqueue",
                    _hidden("note_" + identity, "on")
                    + _hidden("regenerate", "yes")
                    + _check("confirm_send", "Send this note for fresh suggestions"),
                    "Refresh suggestions",
                )
            if data.get("calendar_update"):
                update = data["calendar_update"]
                fields = (
                    _hidden("note_id", identity)
                    + _hidden("if_match", note["content_hash"])
                    + _hidden("calendar_hash", update["calendar_hash"])
                )
                fields += _select(
                    "event_id",
                    "Calendar association",
                    [
                        ("", "Keep existing decisions; dismiss update"),
                        *[(c["event_id"], c["title"]) for c in update.get("candidates", [])],
                    ],
                )
                body += (
                    '<form method="post" action="/v1/admin/workflow/resolve-calendar">'
                    + fields
                    + "<button>Confirm calendar decision</button></form>"
                )
                body += '<p class="notice">' + e(data["calendar_update"]["message"]) + "</p>"
            completion = data.get("completion", {})
            if completion.get("state") == "applying":
                body += (
                    '<form method="post" action="/v1/admin/workflow/resume-review">'
                    + _hidden("note_id", identity)
                    + "<button>Resume recorded review</button></form>"
                )
            body += "</section></div>"
            return page("Review note", body)
        except (StoreError, ValueError) as exc:
            return page("Review unavailable", '<p class="error">' + e(str(exc)) + "</p>", 422)

    @routes.post("/v1/admin/voice/{action}", include_in_schema=False)
    async def voice_action(action: str, request: Request) -> Response:
        from coppermind_admin.main import submitted

        form = await submitted(request)
        if action == "set":
            if form.get("enabled") not in ("true", "false"):
                return page("Voice recognition", "<p>Invalid setting.</p>", 422)
            await client(request).entity_action(
                "voice-set", {"id": form["id"], "enabled": form["enabled"] == "true"}
            )
        elif action == "delete":
            await client(request).entity_action("voice-delete", {"id": form["id"]})
        else:
            return page("Voice recognition", "<p>Unknown action.</p>", 404)
        return RedirectResponse("/admin/workflow?tab=dossiers&saved=1", 303)

    @routes.post("/v1/admin/workflow/{action}", include_in_schema=False)
    async def complete(action: str, request: Request) -> Response:
        from coppermind_admin.main import submitted

        form = await submitted(request)
        try:
            identity = form["note_id"]
            if action == "save-properties":
                from coppermind.schema import normalize_note_time, parse_note_time
                from coppermind_admin.pages.fields import read_schema

                _, schema = read_schema(request.app.state.control)
                entered = str(form.get("date", "")).strip()
                parsed = parse_note_time(entered)
                if parsed.has_offset or (entered and not parsed.valid):
                    raise ValueError(
                        "enter date as YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS with no UTC offset"
                    )
                changes: dict[str, object] = {
                    schema.role("type_key"): form["note_type"],
                }
                unset: list[str] = []
                if entered:
                    changes[schema.role("date_key")] = (
                        normalize_note_time(entered) if parsed.time_value is not None else entered
                    )
                    unset = ["time", "timezone"]
                if form["note_type"] == "work" and "time_unknown" in form:
                    changes["time_unknown"] = True
                elif form["note_type"] == "work" and "date" in form:
                    changes["time_unknown"] = False
                await client(request).patch_frontmatter(
                    identity, PatchFrontmatter(set=changes, unset=unset), if_match=form["if_match"]
                )
                return RedirectResponse("/admin/review/" + quote(identity), 303)
            if action == "resolve-calendar":
                await client(request).context_action(
                    "resolve-calendar",
                    {k: form[k] for k in ("note_id", "if_match", "calendar_hash", "event_id")},
                )
                return RedirectResponse("/admin/review/" + quote(identity), 303)
            if action == "resume-review":
                detail = await client(request).context_action("lifecycle", {"note_id": identity})
                payload = detail["completion"]["payload"]
            elif action == "complete-review":
                payload = {
                    "note_id": identity,
                    "if_match": form["if_match"],
                    "job_id": form.get("job_id") or None,
                    "confirmation": "Complete review",
                    "accept_link_risk": "accept_link_risk" in form,
                    "selected": [
                        int(k.removeprefix("selected_")) for k in form if k.startswith("selected_")
                    ],
                }
            else:
                raise ValueError("unknown review action")
            await client(request).context_action("complete-review", payload)
            return RedirectResponse("/admin/review/" + quote(identity), 303)
        except (StoreError, ValueError, KeyError) as exc:
            return page(
                "Review not completed",
                '<p class="error">'
                + e(str(exc))
                + "</p><p>Your edits are preserved. Reload the note to inspect or resume.</p>",
                422,
            )

    @routes.get("/admin/workflow/plugin.zip", include_in_schema=False)
    async def plugin() -> Response:
        buffer = io.BytesIO()
        root = Path(__file__).parent.parent / "obsidian_plugin"
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name in ("manifest.json", "main.js", "styles.css"):
                archive.write(root / name, "coppermind-review/" + name)
        return Response(
            buffer.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="coppermind-review.zip"'},
        )

    return routes
