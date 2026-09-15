"""Server-rendered operator entry point."""

from __future__ import annotations

import html
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import ValidationError
from starlette.middleware.base import RequestResponseEndpoint

from coppermind.health import Check, Health, Readiness
from coppermind.logging import configure_logging, get_logger
from coppermind.settings import ProductSettings, Wiring
from coppermind.statefiles import StateStore
from coppermind_admin import __version__
from coppermind_admin.auth import (
    AdminCredentials,
    AdminRecordUnreadable,
    AlreadyClaimed,
    ClaimStateUnwritable,
    InvalidClaimCode,
    SignedSessions,
)

SERVICE = "coppermind-admin"
COOKIE = "coppermind_admin_session"
PUBLIC = {
    "/admin/claim",
    "/admin/login",
    "/v1/admin/claim",
    "/v1/admin/login",
    "/healthz",
    "/readyz",
}
log = get_logger(SERVICE)

# What each rejection tells the operator. The page named in the redirect is
# the one that reads the notice, so a refused password is never reported as a
# bad code.
CLAIM_NOTICES = {
    "invalid_claim_code": "That claim code was not accepted.",
    "validation_error": "Enter the claim code and a password of at least 12 characters.",
}
LOGIN_NOTICES = {
    "unauthorized": "That password was not accepted.",
    "already_claimed": "Admin has already been claimed. Log in with the admin password.",
    "session_expired": "That session has ended. Log in again.",
}


def page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{html.escape(title)} | Coppermind</title><style>
:root {{
  color-scheme:light; font-family:system-ui,sans-serif;
  background:#f4f1e8; color:#19231d
}}
body {{ margin:0; min-height:100vh; display:grid; place-items:center }}
main {{
  width:min(28rem,calc(100% - 2rem)); background:#fff; padding:2rem;
  border:1px solid #d3cdbd; border-radius:1rem; box-shadow:0 1rem 3rem #283a2a18
}}
h1 {{ margin-top:0; font-family:Georgia,serif; font-size:2rem }} p {{ line-height:1.5 }}
label {{ display:block; font-weight:650; margin-top:1rem }}
input {{
  box-sizing:border-box; width:100%; margin-top:.4rem; padding:.75rem;
  border:1px solid #8a918b; border-radius:.4rem; font:inherit
}}
button {{
  margin-top:1.25rem; border:0; border-radius:.4rem; padding:.75rem 1rem;
  background:#245c3b; color:#fff; font:inherit; font-weight:700; cursor:pointer
}}
.error {{ padding:.75rem; background:#fde8e4; border-left:.25rem solid #a93624 }}
pre {{ white-space:pre-wrap; overflow-wrap:anywhere; font-size:.85rem }}
.muted {{ color:#59655d }}
</style></head><body><main>{body}</main></body></html>"""


def notice(notices: dict[str, str], error: str | None) -> str:
    if not error:
        return ""
    message = notices.get(error, "That did not work. Try again.")
    return f'<p class="error">{html.escape(message)}</p>'


async def submitted(request: Request) -> dict[str, str]:
    """The fields of a submitted form. Admin is driven by its pages only."""
    parsed = parse_qs((await request.body()).decode(errors="replace"), keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items()}


def error_response(destination: str, code: str) -> RedirectResponse:
    return RedirectResponse(f"/admin/{destination}?error={code}", status_code=303)


def rejection_detail(error: Exception) -> str:
    if isinstance(error, ValidationError):
        return "\n".join(
            f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in error.errors()
        )
    return str(error)


SETTINGS_REMEDY = """Correct that file and log in again. Nothing was changed by this attempt.
The rest of Coppermind reads the same file fresh, so a bad value stops note creation and
reconciliation too, and correcting it restores all of them together."""

ADMIN_RECORD_REMEDY = """Restore that file from a backup, or follow the password recovery steps in
the README: remove the admin record from the state directory, bring the stack up, and claim Admin
again with the code bootstrap issues. The notes filesystem and its database records stay
untouched, though claiming again signs every open Admin session out, and rebuilding the data
volume is neither needed nor appropriate."""

CLAIM_STATE_REMEDY = """Nothing was claimed and your claim code is still good. Admin needs the state
directory on the data volume to be writable by the user it runs as, so check that the volume has
free space and that its ownership and permissions are intact, then submit this form again."""


def claim_form(refusal: str) -> str:
    return page(
        "Claim Admin",
        f"""<h1>Claim Coppermind</h1>{refusal}
<p>Enter the one-time code from the bootstrap claim-code file, then choose the admin password.</p>
<form method="post" action="/v1/admin/claim">
<label>Claim code<input name="code" autocomplete="one-time-code" required></label>
<label>Admin password<input type="password" name="password" autocomplete="new-password"
minlength="12" required></label><button>Claim Admin</button></form>""",
    )


def unwritable_state_refusal(error: ClaimStateUnwritable) -> str:
    return f"""<p class="error">Admin could not write {html.escape(str(error.path))}, so the claim
did not happen.</p>
<p>What the filesystem reported:</p><pre>{html.escape(error.problem)}</pre>
<p class="muted">{CLAIM_STATE_REMEDY}</p>"""


def unreadable_state_file(path: Path, problem: str, remedy: str) -> HTMLResponse:
    return HTMLResponse(
        page(
            "Unreadable control state",
            f"""<h1>Admin cannot read {html.escape(path.name)}</h1>
<p class="error">{html.escape(str(path))} could not be read, so Admin cannot start a session.</p>
<p>What it rejected:</p><pre>{html.escape(problem)}</pre>
<p class="muted">{remedy}</p>""",
        ),
        status_code=500,
    )


def create_app(wiring: Wiring | None = None, sessions: SignedSessions | None = None) -> FastAPI:
    settings = wiring or Wiring()
    configure_logging(SERVICE, settings.log_level)
    version = settings.running_version(__version__)
    credentials = AdminCredentials(settings.state_dir)

    state = StateStore(settings.state_dir)

    # Mirrors ControlState.settings in the store service, which Admin's image
    # does not carry: it installs the shared package and its own service only.
    def product_settings() -> ProductSettings:
        body = dict(state.read("settings").body)
        body.pop("revision", None)
        return ProductSettings.model_validate(body)

    app = FastAPI(title="Coppermind Admin", version=version)
    app.state.sessions = sessions or SignedSessions(credentials)
    log.info("admin started")

    @app.middleware("http")
    async def require_session(request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path.rstrip("/") or "/"
        if path in PUBLIC or path == "/":
            return await call_next(request)
        if not credentials.is_claimed():
            return RedirectResponse("/admin/claim", status_code=303)
        token = request.cookies.get(COOKIE, "")
        if token:
            try:
                signed_in = request.app.state.sessions.valid(token)
            except AdminRecordUnreadable as exc:
                return unreadable_state_file(credentials.path, str(exc), ADMIN_RECORD_REMEDY)
            if signed_in:
                return await call_next(request)
        if token:
            return error_response("login", "session_expired")
        return RedirectResponse("/admin/login", status_code=303)

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/admin/login" if credentials.is_claimed() else "/admin/claim")

    @app.get("/admin/claim", response_class=HTMLResponse, include_in_schema=False)
    async def claim_page(request: Request) -> Response:
        if credentials.is_claimed():
            return RedirectResponse("/admin/login", status_code=303)
        refusal = notice(CLAIM_NOTICES, request.query_params.get("error"))
        return HTMLResponse(claim_form(refusal))

    @app.get("/admin/login", response_class=HTMLResponse, include_in_schema=False)
    async def login_page(request: Request) -> Response:
        if not credentials.is_claimed():
            return RedirectResponse("/admin/claim", status_code=303)
        refusal = notice(LOGIN_NOTICES, request.query_params.get("error"))
        return HTMLResponse(
            page(
                "Login",
                f"""<h1>Admin login</h1>{refusal}<form method="post" action="/v1/admin/login">
<label>Password<input type="password" name="password" autocomplete="current-password"
required></label><button>Log in</button></form>""",
            )
        )

    @app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
    async def overview() -> HTMLResponse:
        return HTMLResponse(
            page(
                "Overview",
                """<h1>Coppermind Admin</h1><p>You are signed in.</p>
<p class="muted">Configuration and status arrive in later increments.</p>
<form method="post" action="/v1/admin/logout"><button>Log out</button></form>""",
            )
        )

    @app.post("/v1/admin/claim", include_in_schema=False)
    async def claim(request: Request) -> Response:
        body = await submitted(request)
        code, password = body.get("code", ""), body.get("password", "")
        if not code or len(password) < 12:
            return error_response("claim", "validation_error")
        try:
            await credentials.claim(code, password)
        except AlreadyClaimed:
            return error_response("login", "already_claimed")
        except InvalidClaimCode:
            return error_response("claim", "invalid_claim_code")
        except ClaimStateUnwritable as exc:
            return HTMLResponse(claim_form(unwritable_state_refusal(exc)), status_code=500)
        return RedirectResponse("/admin/login", status_code=303)

    @app.post("/v1/admin/login", include_in_schema=False)
    async def login(request: Request) -> Response:
        if not credentials.is_claimed():
            return RedirectResponse("/admin/claim", status_code=303)
        password = (await submitted(request)).get("password", "")
        if not password:
            return error_response("login", "unauthorized")
        try:
            verified = await credentials.verify_password(password)
        except AdminRecordUnreadable as exc:
            return unreadable_state_file(credentials.path, str(exc), ADMIN_RECORD_REMEDY)
        if not verified:
            return error_response("login", "unauthorized")
        try:
            product = product_settings()
        except (OSError, ValueError) as exc:
            return unreadable_state_file(
                state.path_for("settings"), rejection_detail(exc), SETTINGS_REMEDY
            )
        lifetime = timedelta(hours=product.admin.session_hours)
        try:
            token = request.app.state.sessions.create(lifetime)
        except AdminRecordUnreadable as exc:
            return unreadable_state_file(credentials.path, str(exc), ADMIN_RECORD_REMEDY)
        response: Response = RedirectResponse("/admin", status_code=303)
        response.set_cookie(
            COOKIE,
            token,
            httponly=True,
            samesite="strict",
            secure=True,
            path="/",
        )
        return response

    @app.post("/v1/admin/logout", include_in_schema=False)
    async def logout() -> Response:
        response = RedirectResponse("/admin/login", status_code=303)
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.get("/healthz", response_model=Health, tags=["operations"])
    async def healthz() -> Health:
        return Health(service=SERVICE, version=version)

    @app.get("/readyz", tags=["operations"])
    async def readyz() -> JSONResponse:
        state_ready = settings.state_dir.is_dir()
        readiness = Readiness.of([Check(name="state", ok=state_ready)])
        return JSONResponse(
            status_code=200 if readiness.ready else 503,
            content=readiness.model_dump(mode="json"),
        )

    return app


app = create_app()
