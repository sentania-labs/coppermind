"Purpose-oriented, independently saved properties; existing advanced rules survive edits."

from __future__ import annotations

from html import escape as e

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from coppermind.schema import FrontmatterSchema, KeyDefinition
from coppermind.statefiles import RevisionConflict
from coppermind_admin.pages.context import _check, _hidden, _input, _select
from coppermind_admin.pages.fields import read_schema

router = APIRouter()
TYPES = [
    ("string", "Text"),
    ("list", "List"),
    ("date", "Date"),
    ("enum", "Choice"),
    ("bool", "Checkbox"),
    ("int", "Number"),
]
PRESETS = {
    "attendees": ("list", "Email addresses of people confirmed as attending"),
    "invitees": ("list", "Email addresses invited to the meeting"),
    "calendar_event": ("string", "Reference to the matched calendar event"),
    "contacts": ("list", "References to known people"),
}


def render(body: str, status: int = 200) -> HTMLResponse:
    from coppermind_admin.main import page

    return HTMLResponse(page("Note properties", body), status_code=status)


@router.get("/admin/properties", include_in_schema=False)
async def properties(request: Request) -> Response:
    state, schema = read_schema(request.app.state.control)
    managed = {
        schema.role(r) for r in ("id_key", "schema_version_key", "reviewed_key", "sources_key")
    }
    body = "<h1>Note properties</h1><p>Define the information your notes can carry.</p>"
    if request.query_params.get("saved"):
        body += '<p class="success">Property saved. Existing notes were not rewritten.</p>'
    body += '<p class="section-links"><a href="/admin/properties/new">+ Add property</a>'
    body += '<a href="/admin/fields?advanced=1">Tags and advanced rules</a></p>'
    body += '<div class="property-list">'
    for key in schema.keys:
        if key.name in managed:
            continue
        body += (
            '<a class="property-row" href="/admin/properties/new?edit='
            + e(key.name, quote=True)
            + '">'
        )
        body += f"<strong>{e(key.name)}</strong><span>{dict(TYPES)[key.kind]}</span>"
        body += f'<small>{e(key.description)}</small><span aria-hidden="true">›</span></a>'
    body += "</div><details><summary>Managed by Coppermind</summary><p>"
    body += e(", ".join(sorted(managed))) + "</p></details>"
    body += (
        '<p class="muted">Properties remain editable in Obsidian. Enrichment '
        "separately controls which values may be suggested.</p>"
    )
    return render(body)


def editor(state, schema, values: dict, error: str = "") -> str:
    name = values.get("name", "")
    existing = next((k for k in schema.keys if k.name == values.get("original")), None)
    body = "<h1>" + ("Edit property" if existing else "Add property") + "</h1>"
    body += '<p><a href="/admin/properties">Back to properties</a></p>'
    if error:
        body += '<p class="error">' + e(error) + "</p>"
    if not existing:
        body += (
            '<p class="section-links">'
            + "".join(
                f'<a href="/admin/properties/new?preset={key}">'
                f"{e(key.replace('_', ' ').title())}</a>"
                for key in PRESETS
            )
            + "</p>"
        )
    body += '<form method="post" action="/v1/admin/property">'
    body += _hidden("revision", str(state.revision)) + _hidden(
        "original", values.get("original", "")
    )
    if existing:
        body += _hidden("name", name) + "<h2>" + e(name) + "</h2>"
        body += '<p class="muted">Stored names are stable. Renaming a key requires a migration.</p>'
    else:
        body += _input("name", "Property name", name)
    body += _select("kind", "Type", TYPES, values.get("kind", "string"))
    body += _input("description", "Purpose (optional)", values.get("description", ""))
    body += '<label id="property-choices">Choices (one per line)<textarea name="choices">'
    body += e(values.get("choices", "")) + "</textarea></label>"
    body += "<details><summary>Validation</summary>"
    body += _check("required", "Required on every note", bool(values.get("required")))
    body += (
        '<p class="muted">Existing defaults and conditional rules are preserv'
        "ed. Manage them under advanced rules.</p></details>"
    )
    body += "<button>Save property</button></form>"
    return body


@router.get("/admin/properties/new", include_in_schema=False)
async def new_property(request: Request) -> Response:
    state, schema = read_schema(request.app.state.control)
    values = {}
    key = next((k for k in schema.keys if k.name == request.query_params.get("edit")), None)
    if key:
        values = {
            "name": key.name,
            "original": key.name,
            "kind": key.kind,
            "description": key.description,
            "required": key.required,
            "choices": "\n".join(key.vocabulary),
        }
    elif request.query_params.get("preset") in PRESETS:
        name = request.query_params["preset"]
        kind, description = PRESETS[name]
        values = {"name": name, "kind": kind, "description": description}
    return render(editor(state, schema, values))


@router.post("/v1/admin/property", include_in_schema=False)
async def save_property(request: Request) -> Response:
    from coppermind_admin.main import submitted

    form = await submitted(request)
    state, schema = read_schema(request.app.state.control)
    try:
        if int(form["revision"]) != state.revision:
            raise ValueError(
                "Properties changed in another session. Your entries are retained; re"
                "view and save again."
            )
        name = form["name"].strip()
        existing = next((k for k in schema.keys if k.name == name), None)
        managed = {
            schema.role(r) for r in ("id_key", "schema_version_key", "reviewed_key", "sources_key")
        }
        if name in managed:
            raise ValueError("Coppermind manages this property")
        if existing and form.get("original") != name:
            raise ValueError("That property already exists. Edit it instead of creating another.")
        if form.get("original") and not existing:
            raise ValueError("The property no longer exists; reload properties")
        definition = existing.model_dump() if existing else {"name": name}
        definition.update(
            kind=form["kind"],
            description=form.get("description", ""),
            required="required" in form,
            vocabulary=[v.strip() for v in form.get("choices", "").splitlines() if v.strip()],
        )
        key = KeyDefinition.model_validate(definition)
        schema.keys = [key if k.name == name else k for k in schema.keys]
        if not existing:
            schema.keys.append(key)
        schema = FrontmatterSchema.model_validate(schema.model_dump())
        request.app.state.control.write(
            "schema", schema.model_dump(mode="json"), if_revision=state.revision
        )
    except (ValueError, KeyError, RevisionConflict) as exc:
        return render(editor(state, schema, dict(form), str(exc)), 422)
    return RedirectResponse("/admin/properties?saved=1", 303)
