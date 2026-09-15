"""Server-rendered operator entry point."""

from __future__ import annotations

import html
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.middleware.base import RequestResponseEndpoint

from coppermind.db.session import make_engine, make_session_factory
from coppermind.errors import envelope
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


def wants_form(request: Request) -> bool:
    return request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded")


async def fields(request: Request) -> dict[str, Any]:
    if wants_form(request):
        parsed = parse_qs((await request.body()).decode(), keep_blank_values=True)
        return {key: values[-1] for key, values in parsed.items()}
    try:
        loaded = await request.json()
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def error_response(request: Request, status: int, code: str, message: str) -> Response:
    if wants_form(request):
        destination = "claim" if request.url.path.endswith("claim") else "login"
        return RedirectResponse(f"/admin/{destination}?error={code}", status_code=303)
    return JSONResponse(status_code=status, content=envelope(code, message))


def create_app(wiring: Wiring | None = None, sessions: Sessions | None = None) -> FastAPI:
    settings = wiring or Wiring()
    configure_logging(SERVICE, settings.log_level)
    version = settings.running_version(__version__)
    credentials = AdminCredentials(settings.state_dir)

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
        if token and await request.app.state.sessions.valid(token):
            request.state.admin_session = token
            return await call_next(request)
        if path.startswith("/admin"):
            target = "/admin/login" if credentials.is_claimed() else "/admin/claim"
            return RedirectResponse(target, status_code=303)
        return JSONResponse(
            status_code=401,
            content=envelope("unauthorized", "an admin session is required"),
        )

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/admin/login" if credentials.is_claimed() else "/admin/claim")

    @app.get("/admin/claim", response_class=HTMLResponse, include_in_schema=False)
    async def claim_page(request: Request) -> Response:
        if credentials.is_claimed():
            return RedirectResponse("/admin/login", status_code=303)
        error = request.query_params.get("error")
        notice = '<p class="error">That claim code was not accepted.</p>' if error else ""
        return HTMLResponse(
            page(
                "Claim Admin",
                f"""<h1>Claim Coppermind</h1>{notice}
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
        error = request.query_params.get("error")
        notice = '<p class="error">That password was not accepted.</p>' if error else ""
        return HTMLResponse(
            page(
                "Login",
                f"""<h1>Admin login</h1>{notice}<form method="post" action="/v1/admin/login">
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

    @app.post("/v1/admin/claim", status_code=201)
    async def claim(request: Request) -> Response:
        body = await fields(request)
        code, password = body.get("code"), body.get("password")
        if not isinstance(code, str) or not isinstance(password, str) or len(password) < 12:
            return error_response(
                request,
                422,
                "validation_error",
                "code and a 12-character password are required",
            )
        try:
            await credentials.claim(code, password)
        except AlreadyClaimed:
            return error_response(request, 409, "already_claimed", "Admin is already claimed")
        except InvalidClaimCode:
            return error_response(request, 403, "invalid_claim_code", "the claim code is not valid")
        if wants_form(request):
            return RedirectResponse("/admin/login", status_code=303)
        return JSONResponse(status_code=201, content={"claimed": True})

    @app.post("/v1/admin/login")
    async def login(request: Request) -> Response:
        body = await fields(request)
        password = body.get("password")
        if not isinstance(password, str) or not await credentials.verify_password(password):
            return error_response(request, 401, "unauthorized", "the admin password is not valid")
        product_body = dict(StateStore(settings.state_dir).read("settings").body)
        product_body.pop("revision", None)
        product = ProductSettings.model_validate(product_body)
        lifetime = timedelta(hours=product.admin.session_hours)
        token = await request.app.state.sessions.create(lifetime)
        response: Response
        if wants_form(request):
            response = RedirectResponse("/admin", status_code=303)
        else:
            response = JSONResponse({"authenticated": True})
        response.set_cookie(
            COOKIE,
            token,
            max_age=int(lifetime.total_seconds()),
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            path="/",
        )
        return response

    @app.post("/v1/admin/logout", status_code=204)
    async def logout(request: Request) -> Response:
        await request.app.state.sessions.delete(request.state.admin_session)
        response = (
            RedirectResponse("/admin/login", status_code=303)
            if wants_form(request)
            else Response(status_code=204)
        )
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
