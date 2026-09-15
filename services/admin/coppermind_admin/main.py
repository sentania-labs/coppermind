"""Server-rendered operator entry point."""

from __future__ import annotations

import html
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.datastructures import URL
from starlette.middleware.base import RequestResponseEndpoint

from coppermind.db.session import make_engine, make_session_factory
from coppermind.health import Check, Health, Readiness
from coppermind.logging import configure_logging, get_logger
from coppermind.settings import ProductSettings, Wiring
from coppermind.statefiles import StateStore
from coppermind_admin import __version__
from coppermind_admin.auth import (
    AdminCredentials,
    AlreadyClaimed,
    InvalidClaimCode,
    PostgresSessions,
    Sessions,
    SessionsUnavailable,
)

SERVICE = "coppermind-admin"
COOKIE = "coppermind_admin_session"
# Carried once by the redirect a successful login sends the browser to, and
# stripped as soon as a session validates. Arriving here with the marker and
# without the session cookie is the browser having dropped it.
SIGNED_IN = "signed_in"
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
    "cookie_not_kept": (
        "That password was accepted, but your browser did not keep the session cookie, so "
        "Admin could not sign you in. A browser drops it when Admin is reached over plain "
        "HTTP while secure cookies are on: reach Admin over HTTPS, or set admin.cookie_secure "
        "to false in /data/state/settings.yaml to run it deliberately in the clear."
    ),
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


def without_marker(url: URL) -> str:
    cleaned = url.remove_query_params(SIGNED_IN)
    return f"{cleaned.path}?{cleaned.query}" if cleaned.query else cleaned.path


def unavailable() -> HTMLResponse:
    return HTMLResponse(
        page(
            "Unavailable",
            """<h1>Admin is unavailable</h1>
<p class="error">The session database could not be reached, so Admin cannot check or create a
sign-in right now.</p>
<p class="muted">Nothing was lost. Admin works again as soon as PostgreSQL returns.</p>""",
        ),
        status_code=503,
    )


def create_app(wiring: Wiring | None = None, sessions: Sessions | None = None) -> FastAPI:
    settings = wiring or Wiring()
    configure_logging(SERVICE, settings.log_level)
    version = settings.running_version(__version__)
    credentials = AdminCredentials(settings.state_dir)

    # Mirrors ControlState.settings in the store service, which Admin's image
    # does not carry: it installs the shared package and its own service only.
    def product_settings() -> ProductSettings:
        body = dict(StateStore(settings.state_dir).read("settings").body)
        body.pop("revision", None)
        return ProductSettings.model_validate(body)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = None
        if sessions is None:
            engine = make_engine(settings.database_url_for("asyncpg"))
            app.state.sessions = PostgresSessions(make_session_factory(engine))
        else:
            app.state.sessions = sessions
        app.state.credentials = credentials
        app.state.settings = settings
        log.info("admin started")
        try:
            yield
        finally:
            if engine is not None:
                await engine.dispose()

    app = FastAPI(title="Coppermind Admin", version=version, lifespan=lifespan)

    @app.middleware("http")
    async def require_session(request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path.rstrip("/") or "/"
        if path in PUBLIC or path == "/":
            return await call_next(request)
        token = request.cookies.get(COOKIE, "")
        if token:
            try:
                signed_in = await request.app.state.sessions.valid(token)
            except SessionsUnavailable:
                return unavailable()
            if signed_in:
                if SIGNED_IN in request.query_params:
                    return RedirectResponse(without_marker(request.url), status_code=303)
                request.state.admin_session = token
                return await call_next(request)
        if not credentials.is_claimed():
            return RedirectResponse("/admin/claim", status_code=303)
        if token:
            return error_response("login", "session_expired")
        if SIGNED_IN in request.query_params:
            return error_response("login", "cookie_not_kept")
        return RedirectResponse("/admin/login", status_code=303)

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/admin/login" if credentials.is_claimed() else "/admin/claim")

    @app.get("/admin/claim", response_class=HTMLResponse, include_in_schema=False)
    async def claim_page(request: Request) -> Response:
        if credentials.is_claimed():
            return RedirectResponse("/admin/login", status_code=303)
        refusal = notice(CLAIM_NOTICES, request.query_params.get("error"))
        return HTMLResponse(
            page(
                "Claim Admin",
                f"""<h1>Claim Coppermind</h1>{refusal}
<p>Enter the one-time code from the bootstrap claim-code file, then choose the admin password.</p>
<form method="post" action="/v1/admin/claim">
<label>Claim code<input name="code" autocomplete="one-time-code" required></label>
<label>Admin password<input type="password" name="password" autocomplete="new-password"
minlength="12" required></label><button>Claim Admin</button></form>""",
            )
        )

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
        return RedirectResponse("/admin/login", status_code=303)

    @app.post("/v1/admin/login", include_in_schema=False)
    async def login(request: Request) -> Response:
        password = (await submitted(request)).get("password", "")
        if not password or not await credentials.verify_password(password):
            return error_response("login", "unauthorized")
        product = product_settings()
        lifetime = timedelta(hours=product.admin.session_hours)
        try:
            token = await request.app.state.sessions.create(lifetime)
        except SessionsUnavailable:
            return unavailable()
        response: Response = RedirectResponse(f"/admin?{SIGNED_IN}=1", status_code=303)
        response.set_cookie(
            COOKIE,
            token,
            httponly=True,
            samesite="strict",
            secure=product.admin.cookie_secure,
            path="/",
        )
        return response

    @app.post("/v1/admin/logout", include_in_schema=False)
    async def logout(request: Request) -> Response:
        try:
            await request.app.state.sessions.delete(request.state.admin_session)
        except SessionsUnavailable:
            return unavailable()
        response = RedirectResponse("/admin/login", status_code=303)
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.get("/healthz", response_model=Health, tags=["operations"])
    async def healthz() -> Health:
        return Health(service=SERVICE, version=version)

    @app.get("/readyz", tags=["operations"])
    async def readyz(request: Request) -> JSONResponse:
        database_ready = await request.app.state.sessions.ready()
        readiness = Readiness.of([Check(name="postgres", ok=database_ready)])
        return JSONResponse(
            status_code=200 if readiness.ready else 503,
            content=readiness.model_dump(mode="json"),
        )

    return app


app = create_app()
