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

from coppermind.settings import Wiring
from coppermind.store_protocol import PatchFrontmatter, StoreError
from coppermind_admin.pages.context import _check, _form, _hidden, _input, _select
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
                    ("intake", "Dropbox"),
                    ("enrichment", "Enrichment"),
                    ("review", "Review notes"),
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
                title = "Note workflow"
                body += "<p>New note → suggestions → your review → filing and confirmed facts.</p>"
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
                title = "Dropbox"
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
                    '<p class="muted">Files must stay unchanged for 30 seconds. Intake li'
                    "mit: 10 MiB. ZIP limit: 40 MiB expanded, 100 entries. No files are d"
                    "eleted.</p>"
                )
                calendars = [(s["id"], s["name"]) for s in config["sources"]] + [
                    (s["id"], s["name"]) for s in dropbox["calendars"]
                ]
                for item in dropbox["items"]:
                    body += (
                        '<section class="connection-card"><h2>'
                        + e(item["filename"])
                        + "</h2><p>"
                        + e(item["state"].replace("_", " "))
                        + "</p>"
                    )
                    if item["error"]:
                        body += '<p class="error">' + e(item["error"]) + "</p>"
                    if item["state"] == "needs_identity":
                        body += (
                            "<p>Choose whether this export updates an existing calendar "
                            "or creates a calend"
                            "ar. Names and filenames alone cannot establish identity.</p>"
                        )
                        for i, part in enumerate(item["parts"]):
                            if part["calendar_id"]:
                                continue
                            fields = _hidden("id", item["id"]) + _hidden("part", str(i))
                            fields += "<p>Entry: " + e(part["entry"]) + "</p>"
                            fields += _select(
                                "calendar_id",
                                "Belongs to",
                                [("", "Create a separate calendar"), *calendars],
                            )
                            fields += _input("name", "Name for a new calendar", part["name"])
                            fields += _input(
                                "timezone", "Timezone for a new calendar", "America/Chicago"
                            )
                            body += _form("identify-drop", fields, "Identify calendar")
                    body += "</section>"
                if not dropbox["items"]:
                    body += '<p class="notice">No files have been received yet.</p>'
                body += (
                    "<details><summary>Existing calendar sources and import history</summ"
                    'ary><a href="/admin/context?tab=calendar">Inspect calendar records</'
                    "a></details>"
                )
            elif tab == "enrichment":
                title = "Enrichment"
                assignment = config["assignments"]["enrichment"]
                body += (
                    "<p>Make suggestions once for each new unreviewed note. Editing a not"
                    "e does not repeatedly call the model.</p>"
                )
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
                    "<fieldset><legend>What should it suggest?</legend><p>A clearer filen"
                    "ame can be suggested. Your note body stays unchanged.</p>"
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
                fields += "<details><summary>Processing limits</summary>"
                for key, label in [
                    ("timeout_s", "Request timeout (seconds)"),
                    ("max_input_chars", "Maximum note and transcript characters"),
                    ("max_output_tokens", "Maximum response tokens"),
                    ("attempts", "Maximum attempts"),
                ]:
                    fields += _input(key, label, str(assignment[key]), kind="number")
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
                    "<p>Confirmed contributions from reviewed notes. Editing a reviewed n"
                    "ote updates its contribution; names alone never merge identities.</p"
                    ">"
                )
                for person in data.get("dossiers", {}).values():
                    body += (
                        '<section class="connection-card"><h2>' + e(person["address"]) + "</h2><ul>"
                    )
                    for note in person["notes"]:
                        body += (
                            '<li><a href="/admin/review/'
                            + quote(note["id"])
                            + '">'
                            + e(note["path"])
                            + "</a> · "
                            + e(note["date"])
                            + "</li>"
                        )
                    body += "</ul></section>"
                if not data.get("dossiers"):
                    body += '<p class="notice">No confirmed attendee contributions yet.</p>'
            else:
                title = "Review notes"
                body += (
                    "<p>Read and correct the note, choose any suggestions, "
                    "then finish review once.</p>"
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
                if not config["assignments"]["enrichment"]["enabled"]:
                    body += (
                        '<p class="notice">Enrichment is off. <a href="/admin/workflow?tab=en'
                        'richment">Finish enrichment setup</a> to queue notes. You can still '
                        "review notes without suggestions.</p>"
                    )
                else:
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
            properties += _select(
                "note_type",
                "Note type",
                [(v, v.title()) for v in definition.vocabulary] or [(kind, kind)],
                kind,
            )
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
