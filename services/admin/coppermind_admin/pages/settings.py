"""Typed, independently saved sections of the revisioned settings file."""

import json
from html import escape
from typing import Any, Literal, get_args, get_origin

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from pydantic.fields import FieldInfo

from coppermind.settings import ProductSettings
from coppermind.statefiles import RevisionConflict, StateStore

router = APIRouter()

SECTIONS = {
    "general": "General",
    "notes": "Folders",
    "reconcile": "File discovery",
    "git": "Git history",
    "sync": "Sync limits",
    "limits": "Upload limits",
    "admin": "Sessions",
    "curator": "Filing (planned)",
    "indexer": "Index settings (planned)",
    "events": "Events (planned)",
    "schema_version": "Settings format",
}
UNAVAILABLE = {
    "curator": "Automatic filing is not implemented. These settings do not affect notes yet.",
    "indexer": (
        "Search works, but these controls are not connected yet. Indexing is always on, "
        "uses English plus exact words, and catches up with filesystem reconciliation."
    ),
    "events": "Event consumers and retention controls are not implemented yet.",
    "schema_version": "This format version is managed by Coppermind and cannot be edited here.",
}
LABELS = {
    "general.timezone": "Local timezone",
    "reconcile.scan_interval_s": "Check for file changes every (seconds)",
    "reconcile.quiet_period_s": "Wait for new files to settle (seconds)",
    "reconcile.full_rehash_daily_at": "Full daily scan at (local time)",
    "reconcile.excluded_folders": "Never adopt these folders",
    "reconcile.excluded_patterns": "Never adopt these filename patterns",
    "git.debounce_s": "Wait after an edit before recording history (seconds)",
    "git.poll_interval_s": "Check for missed changes every (seconds)",
    "admin.session_hours": "Sign in again after (hours)",
}


def fields(model: BaseModel, prefix: str = ""):
    """Walk leaves, including settings added after the page was built."""
    for name, field in type(model).model_fields.items():
        value = getattr(model, name)
        path = f"{prefix}.{name}" if prefix else name
        if isinstance(value, BaseModel):
            yield from fields(value, path)
        else:
            yield path, field, value


def displayed(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def input_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "\n".join(str(item) for item in value)
    return displayed(value)


def control(path: str, field: FieldInfo, default: Any, text: str, disabled: bool) -> str:
    attrs = f'id="{path}" name="{path}"' + (" disabled" if disabled else "")
    choices = get_args(field.annotation) if get_origin(field.annotation) is Literal else ()
    if isinstance(default, bool):
        choices = ("true", "false")
    if choices:
        options = ""
        # An invalid submitted value is kept visible while the person corrects it.
        if text not in choices:
            options += (
                f'<option selected value="{escape(text, quote=True)}">{escape(text)}</option>'
            )
        for choice in choices:
            label = {"true": "Enabled", "false": "Disabled"}.get(choice, str(choice).title())
            selected = " selected" if text == choice else ""
            options += f'<option value="{choice}"{selected}>{label}</option>'
        return f"<select {attrs}>{options}</select>"
    if isinstance(default, list):
        return f'<textarea {attrs} rows="5">{escape(text)}</textarea>'
    kind = "number" if isinstance(default, int) or default is None else "text"
    extra = ' placeholder="Use the default limit"' if default is None else ""
    if kind == "number" and text and text.lstrip("-").isdigit() is False:
        kind = "text"
    if kind == "number":
        for constraint in field.metadata:
            minimum = getattr(constraint, "ge", None)
            if minimum is None and getattr(constraint, "gt", None) is not None:
                minimum = constraint.gt + 1
            maximum = getattr(constraint, "le", None)
            if minimum is not None:
                extra += f' min="{minimum}"'
            if maximum is not None:
                extra += f' max="{maximum}"'
    if default is not None:
        extra += " required"
    return f'<input {attrs} type="{kind}" value="{escape(text, quote=True)}"{extra}>'


def render(
    store: StateStore,
    message: str = "",
    status: int = 200,
    submitted: dict[str, str] | None = None,
    section: str = "general",
) -> HTMLResponse:
    from coppermind_admin import main

    state = store.read("settings")
    current = ProductSettings.model_validate(
        {key: value for key, value in state.body.items() if key != "revision"}
    )
    defaults = {path: value for path, _, value in fields(ProductSettings())}
    revision = submitted.get("revision", "") if submitted else str(state.revision)
    if section not in SECTIONS:
        section = "general"
    links = "".join(
        f'<a href="/admin/settings?section={name}"'
        + (' aria-current="page"' if name == section else "")
        + f">{label}</a>"
        for name, label in SECTIONS.items()
    )
    body = f"""<h1>Settings</h1><p>Save one section at a time. Other settings stay unchanged.</p>
{message}<div class="section-links" aria-label="Settings sections">{links}</div>"""
    for group, label in SECTIONS.items():
        hidden = " hidden" if group != section else ""
        body += f'''<form method="post" action="/v1/admin/settings?section={group}"{hidden}>
<input type="hidden" name="revision" value="{escape(revision, quote=True)}">
<h2>{label}</h2>'''
        unavailable = UNAVAILABLE.get(group)
        if unavailable:
            body += f'<p class="notice">{escape(unavailable)}</p>'
        if group == "notes":
            body += '<p class="notice">Changing a folder setting does not move existing files.</p>'
        body += '<div class="settings-grid">'
        for path, field, value in fields(current):
            if path.split(".")[0] != group:
                continue
            text = submitted.get(path, input_text(value)) if submitted else input_text(value)
            title = LABELS.get(path, path.split(".")[-1].replace("_", " ").capitalize())
            disabled = bool(unavailable) or path == "limits.attachment_max_bytes"
            help_text = ""
            if isinstance(defaults[path], list):
                help_text = "One entry per line. "
            elif defaults[path] is None:
                help_text = "Leave blank to use the default limit. "
            if path == "limits.attachment_max_bytes":
                help_text += "The attachment upload route is not implemented yet. "
            body += f'''<div><label for="{path}">{title}</label>
{control(path, field, defaults[path], text, disabled)}
<p class="setting-help">{escape(help_text)}{escape(field.description or field.title or path)}<br>
Default: <code>{escape(displayed(defaults[path]))}</code>.
<span class="muted">Setting: <code>{path}</code></span></p></div>'''
        body += '</div><div class="form-footer">'
        if not unavailable:
            body += "<button>Save this section</button>"
        body += f'<p class="muted">Revision: {state.revision}. Concurrent changes are refused.</p>'
        body += "</div></form>"
    initial_dirty = "true" if submitted else "false"
    body += f"""<script type="module">
let dirty = {initial_dirty};
for (const form of document.querySelectorAll('main form')) {{
  form.addEventListener('input', () => {{ dirty = true; }});
  form.addEventListener('submit', () => {{ dirty = false; }});
}}
window.addEventListener('beforeunload', (event) => {{
  if (dirty) {{ event.preventDefault(); event.returnValue = ''; }}
}});
</script>"""
    return HTMLResponse(main.page("Settings", body), status_code=status)


def failure(error: Exception, status: int = 500) -> HTMLResponse:
    from coppermind_admin import main

    return HTMLResponse(
        main.page(
            "Settings",
            f'<h1>Settings</h1><p class="error">{escape(main.rejection_detail(error))}</p>'
            '<a href="/admin/settings">Reload settings</a>',
        ),
        status_code=status,
    )


@router.get("/admin/settings", include_in_schema=False)
async def settings_page(request: Request) -> Response:
    try:
        return render(
            request.app.state.control, section=request.query_params.get("section", "general")
        )
    except (OSError, ValueError) as exc:
        return failure(exc)


def parsed_value(path: str, default: Any, text: str, *, line_lists: bool = False) -> Any:
    if isinstance(default, str):
        return text
    if isinstance(default, list) and (line_lists or not text.lstrip().startswith("[")):
        return [item.strip() for item in text.splitlines() if item.strip()]
    if default is None and not text.strip():
        return None
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{path}: enter a valid number, selection, or list") from exc


@router.post("/v1/admin/settings", include_in_schema=False)
async def save(request: Request) -> Response:
    from coppermind_admin import main

    body = await main.submitted(request)
    store = request.app.state.control
    section = request.query_params.get("section")
    try:
        if section is not None and (section not in SECTIONS or section in UNAVAILABLE):
            raise ValueError("This settings section is not editable.")
        # No section preserves the original complete-form contract for existing callers.
        data: dict[str, Any] = store.read("settings").body.copy() if section else {}
        data.pop("revision", None)
        for path, _, default in fields(ProductSettings()):
            if section is not None and path.split(".")[0] != section:
                continue
            if section and path == "limits.attachment_max_bytes":
                continue
            if path not in body:
                raise ValueError(f"{path}: field is missing")
            value = parsed_value(path, default, body[path], line_lists=section is not None)
            target = data
            parts = path.split(".")
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = value
        settings = ProductSettings.model_validate(data)
        try:
            revision = int(body.get("revision", ""))
        except ValueError as exc:
            raise ValueError("revision: a revision is required") from exc
        saved = store.write("settings", settings.model_dump(mode="json"), if_revision=revision)
        return render(
            store,
            f'<p class="success" role="status">Saved revision {saved.revision}.</p>',
            section=section or "general",
        )
    except RevisionConflict as exc:
        return render(
            store,
            f'<p class="error">{escape(str(exc))}. Your submitted values remain below. '
            f'<a href="/admin/settings?section={escape(section or "general", quote=True)}">'
            "Reload current settings</a> before reapplying your changes.</p>",
            409,
            body,
            section or "general",
        )
    except ValueError as exc:
        try:
            return render(
                store,
                f'<p class="error">{escape(main.rejection_detail(exc))}</p>',
                422,
                body,
                section or "general",
            )
        except (OSError, ValueError) as read_error:
            return failure(read_error)
    except OSError as exc:
        return failure(exc)
