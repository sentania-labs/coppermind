"""Model-derived controls for every product setting."""

import json
from html import escape
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from coppermind.settings import ProductSettings
from coppermind.statefiles import RevisionConflict, StateStore

router = APIRouter()


def fields(model: BaseModel, prefix: str = ""):
    """Walk model leaves, including fields introduced after this page shipped."""
    for name, field in type(model).model_fields.items():
        value = getattr(model, name)
        path = f"{prefix}.{name}" if prefix else name
        if isinstance(value, BaseModel):
            yield from fields(value, path)
        else:
            yield path, field, value


def displayed(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def render(
    store: StateStore, message: str = "", status: int = 200, submitted: dict[str, str] | None = None
) -> HTMLResponse:
    from coppermind_admin import main

    state = store.read("settings")
    current = ProductSettings.model_validate(
        {key: value for key, value in state.body.items() if key != "revision"}
    )
    defaults = {path: value for path, _, value in fields(ProductSettings())}
    revision = submitted.get("revision", "") if submitted else str(state.revision)
    body = f'''<h1>Settings</h1><p><a href="/admin">Overview</a></p>{message}
<p>Revision: {state.revision}</p><p>Lists use JSON arrays. Use null for an unset override.</p>
<form method="post" action="/v1/admin/settings">
<input type="hidden" name="revision" value="{escape(revision, quote=True)}">'''
    section = None
    for path, field, value in fields(current):
        group = path.split(".")[0]
        if group != section:
            if section is not None:
                body += "</fieldset>"
            body += f"<fieldset><legend>{escape(group)}</legend>"
            section = group
        text = submitted.get(path, displayed(value)) if submitted else displayed(value)
        body += f'''<label for="{path}">{path}</label>
<input id="{path}" name="{path}" value="{escape(text, quote=True)}">
<p class="muted">Default: <code>{escape(displayed(defaults[path]))}</code>.
{escape(field.description or field.title or path)}</p>'''
    body += "</fieldset><button>Save settings</button></form>"
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
        return render(request.app.state.control)
    except (OSError, ValueError) as exc:
        return failure(exc)


@router.post("/v1/admin/settings", include_in_schema=False)
async def save(request: Request) -> Response:
    from coppermind_admin import main

    body = await main.submitted(request)
    store = request.app.state.control
    try:
        data: dict[str, Any] = {}
        for path, _, default in fields(ProductSettings()):
            if path not in body:
                raise ValueError(f"{path}: field is missing")
            value: Any = body[path]
            if not isinstance(default, str):
                try:
                    value = json.loads(value)
                except ValueError as exc:
                    raise ValueError(f"{path}: enter a JSON number, boolean, list or null") from exc
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
        return render(store, f"<p>Saved revision {saved.revision}.</p>")
    except RevisionConflict as exc:
        return failure(exc, 409)
    except ValueError as exc:
        try:
            return render(
                store, f'<p class="error">{escape(main.rejection_detail(exc))}</p>', 422, body
            )
        except (OSError, ValueError) as read_error:
            return failure(read_error)
    except OSError as exc:
        return failure(exc)
