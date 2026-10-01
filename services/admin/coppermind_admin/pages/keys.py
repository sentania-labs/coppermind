"""API key creation, audit listing and confirmed revocation."""

from html import escape

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from coppermind.api_keys import API_SCOPES, add_key, load_keys, revoke_key
from coppermind.statefiles import RevisionConflict, StateStore

router = APIRouter()


def render(store: StateStore, message: str = "", status: int = 200) -> HTMLResponse:
    from coppermind_admin import main

    keys = load_keys(store)
    body = '<h1>API Keys</h1><p><a href="/admin">Overview</a></p>' + message
    body += "<p>Changes reach the API within five minutes.</p>"
    for key in keys.keys:
        body += f"""<section><h2>{escape(key.name)}</h2>
<p>ID: {key.key_id}</p><p>Scopes: {escape(", ".join(key.scopes)) or "None"}</p>
<p>Created: {key.created_at.isoformat()}</p>
<p>Revoked: {key.revoked_at.isoformat() if key.revoked_at else "No"}</p>"""
        if key.revoked_at is None:
            body += f"""<form method="post" action="/v1/admin/keys/{key.key_id}/confirm">
<input type="hidden" name="revision" value="{keys.revision}">
<button>Revoke {escape(key.name)}</button></form>"""
        body += "</section>"
    body += f"""<h2>Create a key</h2><form method="post" action="/v1/admin/keys/create">
<input type="hidden" name="revision" value="{keys.revision}">
<label>Name<input name="name" required></label><fieldset><legend>Scopes</legend>"""
    for scope in API_SCOPES:
        body += f'<label><input type="checkbox" name="scope:{scope}" value="on">{scope}</label>'
    body += "</fieldset><button>Create key</button></form>"
    return HTMLResponse(main.page("API Keys", body), status_code=status)


def refused(error: Exception, status: int) -> HTMLResponse:
    from coppermind_admin import main

    return HTMLResponse(
        main.page(
            "API Keys",
            f'<h1>API Keys</h1><p class="error">{escape(main.rejection_detail(error))}</p>'
            '<a href="/admin/keys">Reload keys</a>',
        ),
        status_code=status,
    )


@router.get("/admin/keys", include_in_schema=False)
async def keys_page(request: Request) -> Response:
    try:
        return render(request.app.state.control)
    except (OSError, ValueError) as exc:
        return refused(exc, 500)


@router.post("/v1/admin/keys/create", include_in_schema=False)
async def create(request: Request) -> Response:
    from coppermind_admin import main

    body = await main.submitted(request)
    try:
        revision = int(body.get("revision", ""))
        scopes = [
            name.removeprefix("scope:")
            for name, value in body.items()
            if name.startswith("scope:") and value == "on"
        ]
        credential = add_key(
            request.app.state.control, body.get("name", ""), scopes, if_revision=revision
        )
        return HTMLResponse(
            main.page(
                "Key created",
                "<h1>Key created</h1><p>Copy this credential now. It will never be shown again.</p>"
                f'<pre id="credential">{escape(credential)}</pre>'
                '<a href="/admin/keys">Back to API Keys</a>',
            ),
            status_code=201,
        )
    except RevisionConflict as exc:
        return refused(exc, 409)
    except ValueError as exc:
        return refused(exc, 422)
    except OSError as exc:
        return refused(exc, 500)


@router.post("/v1/admin/keys/{key_id}/confirm", include_in_schema=False)
async def confirm(request: Request, key_id: str) -> Response:
    from coppermind_admin import main

    body = await main.submitted(request)
    try:
        keys = load_keys(request.app.state.control)
        if int(body.get("revision", "")) != keys.revision:
            raise RevisionConflict(keys.revision)
        key = next((key for key in keys.keys if key.key_id == key_id), None)
        if key is None:
            raise ValueError("Unknown key id")
        return HTMLResponse(
            main.page(
                "Confirm revocation",
                f'''<h1>Revoke {escape(key.name)}?</h1>
<p>Key {key.key_id} will stop authenticating within five minutes.</p>
<form method="post" action="/v1/admin/keys/{key.key_id}/revoke">
<input type="hidden" name="revision" value="{keys.revision}">
<input type="hidden" name="confirmed" value="yes"><button>Confirm revocation</button></form>
<a href="/admin/keys">Cancel</a>''',
            )
        )
    except RevisionConflict as exc:
        return refused(exc, 409)
    except ValueError as exc:
        return refused(exc, 422)
    except OSError as exc:
        return refused(exc, 500)


@router.post("/v1/admin/keys/{key_id}/revoke", include_in_schema=False)
async def revoke(request: Request, key_id: str) -> Response:
    from coppermind_admin import main

    body = await main.submitted(request)
    try:
        if body.get("confirmed") != "yes":
            raise ValueError("Confirm revocation first")
        revoke_key(request.app.state.control, key_id, if_revision=int(body.get("revision", "")))
        return render(request.app.state.control, "<p>Key revoked.</p>")
    except RevisionConflict as exc:
        return refused(exc, 409)
    except ValueError as exc:
        return refused(exc, 422)
    except OSError as exc:
        return refused(exc, 500)
