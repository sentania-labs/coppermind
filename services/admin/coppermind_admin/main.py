"""Server-rendered operator entry point.

Admin is the break-glass surface: the Obsidian companion is how the owner works
day to day, and Admin is where setup happens and where something stuck gets
fixed. Every page is plain HTML with forms, so it works with scripts off, and
every form that changes something carries a token bound to the signed-in session.
"""

from __future__ import annotations

import html
import re
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import ValidationError
from starlette.middleware.base import RequestResponseEndpoint

from coppermind.health import Check, Health, Readiness
from coppermind.logging import configure_logging, get_logger
from coppermind.settings import ProductSettings, Wiring, read_settings
from coppermind.statefiles import StateStore
from coppermind.store_client import HttpStoreClient
from coppermind_admin import __version__, layout, ui
from coppermind_admin.auth import (
    AdminCredentials,
    AdminRecordUnreadable,
    AlreadyClaimed,
    ClaimCodeUnreadable,
    ClaimStateProblem,
    ClaimStateUnwritable,
    InvalidClaimCode,
    SignedSessions,
)
from coppermind_admin.shell import COOKIE, csrf_token, with_csrf

SERVICE = "coppermind-admin"
PUBLIC = {"/admin/welcome", "/admin/signin", "/healthz", "/readyz"}
# The only places a file is handed in. Each streams its body and checks the form token itself.
UPLOADS = {"/admin/incoming/add", "/admin/calendar/add", "/admin/storage/files/add"}
log = get_logger(SERVICE)

# Bound every form body so a POST cannot make Admin buffer a body of any size it likes.
# A file handed in from this computer is not a form: it is streamed, never buffered.
MAX_FORM_BYTES = 65536
CSRF_COOKIE = "coppermind_admin_csrf"
MIN_PASSWORD = 12


class SubmissionTooLarge(Exception):
    """The form body passed the ceiling before Admin finished reading it."""


def _derive_csrf(sessions: SignedSessions, session: str) -> str:
    """Derive a CSRF token bound to the signed session.

    The cookie value is never used as a CSRF token; this derivation prevents
    any service on the same host from forging a matching form field, because
    only this process holds the session secret that signs the derivation.
    """
    try:
        return sessions.csrf_token(session)
    except AdminRecordUnreadable:
        return ""


async def submitted(request: Request) -> dict[str, str]:
    """The fields of a submitted form. Admin is driven by its pages only."""
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_FORM_BYTES:
            raise SubmissionTooLarge
    parsed = parse_qs(bytes(body).decode(errors="replace"), keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items()}


def is_upload(request: Request) -> bool:
    return request.headers.get("content-type", "").startswith("multipart/form-data")


def rejection_detail(error: Exception) -> str:
    if isinstance(error, ValidationError):
        return "\n".join(
            f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in error.errors()
        )
    return str(error)


SETTINGS_REMEDY = """Whoever runs the server should correct that file. Nothing was changed by
this attempt. The rest of Coppermind reads the same file, so a bad value there stops notes being
taken in too, and correcting it restores everything together."""

ADMIN_RECORD_REMEDY = """Whoever runs the server should restore that file from a backup, or
restart Coppermind with its "new Admin setup link" option and choose a new password from the
link it shows. Notes and settings are not affected either way."""

CLAIM_RECORD_REMEDY = """Nothing was saved and your setup code is still good. Admin needs to be
able to write to its data folder, so whoever runs the server should check that it has free space
and that its ownership is intact. Then press Start setup again."""

CLAIM_CODE_REMEDY = """Nothing was saved and your setup code is still good. Admin could not read
the code it was given at install, so whoever runs the server should check the ownership of that
file. Then press Start setup again."""


def public_page(title: str, body: str) -> str:
    return with_csrf(layout.public(title, f'<section class="lat-card stack">{body}</section>'))


def problem_page(title: str, headline: str, problem: str, remedy: str) -> HTMLResponse:
    return HTMLResponse(
        public_page(
            title,
            f"<div><h1>{html.escape(title)}</h1></div>"
            + ui.banner("bad", html.escape(headline))
            + f'<p class="muted">{remedy}</p>'
            + ui.tech(problem),
        ),
        status_code=500,
    )


def welcome_body(code: str, errors: dict[str, str], *, reset: bool, banner: str = "") -> str:
    hint = (
        "Filled in from the link you opened. "
        if code
        else "It is in the link Coppermind showed when it was installed. "
    ) + ui.reveal(
        "Where does the code come from?",
        '<div class="lat-hint">Coppermind shows a link when it first starts, in the same '
        "window where it was installed. The link carries this code. If you only have the "
        "code, type it here.</div>",
    )
    lede = (
        "Two things and you are back in: the setup code from the new link, and a new "
        "password for these Admin screens."
        if reset
        else "Two things and you are in: the setup code, and a password for these Admin screens."
    )
    return f"""<div><h1>{"Choose a new password" if reset else "Welcome to Coppermind"}</h1>
<p class="lede">{lede}</p></div>{banner}
<form method="post" action="/admin/welcome" class="stack">
<input type="hidden" name="tz" id="tz" value="">
{
        ui.text_input(
            "code",
            "Setup code",
            code,
            hint=hint,
            error=errors.get("code", ""),
            autocomplete="one-time-code",
        )
    }
{
        ui.text_input(
            "pw1",
            "Choose a password",
            kind="password",
            error=errors.get("pw1", ""),
            hint=f"At least {MIN_PASSWORD} characters. You will use it to sign in to Admin.",
            autocomplete="new-password",
        )
    }
{
        ui.text_input(
            "pw2",
            "Type the password again",
            kind="password",
            error=errors.get("pw2", ""),
            autocomplete="new-password",
        )
    }
<div class="actions">{ui.submit("Save the new password" if reset else "Start setup")}</div>
</form>
<script>try{{document.getElementById("tz").value=
Intl.DateTimeFormat().resolvedOptions().timeZone||""}}catch(e){{}}</script>"""


def signin_body(error: str, notice: str = "") -> str:
    forgot = ui.reveal(
        "Forgot the password?",
        '<div class="lat-hint">For safety a forgotten password can only be reset from the '
        "Coppermind server itself, by whoever installed it. They restart Coppermind with its "
        '"new Admin setup link" option. It then shows a fresh setup link, the same kind you '
        "used the first time. Open that link and choose a new password. Your notes and "
        "settings are not affected.</div>",
    )
    return f"""<div><h1>Sign in to Coppermind Admin</h1>
<p class="lede">Enter the Admin password you chose during setup.</p></div>{notice}
<form method="post" action="/admin/signin" class="stack">
{
        ui.text_input(
            "password",
            "Password",
            kind="password",
            error=error,
            autocomplete="current-password",
            autofocus=True,
        )
    }
<div class="actions">{ui.submit("Sign in")}</div></form>
<div>{forgot}</div>"""


def create_app(wiring: Wiring | None = None, sessions: SignedSessions | None = None) -> FastAPI:
    settings = wiring or Wiring()
    configure_logging(SERVICE, settings.log_level)
    version = settings.running_version(__version__)
    credentials = AdminCredentials(settings.state_dir)
    state = StateStore(settings.state_dir)

    def product_settings() -> ProductSettings:
        return read_settings(state)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            client = getattr(app.state, "store", None)
            if isinstance(client, HttpStoreClient):
                await client.aclose()

    app = FastAPI(title="Coppermind Admin", version=version, lifespan=lifespan)
    app.state.sessions = sessions or SignedSessions(credentials)
    app.state.credentials = credentials
    app.state.control = state
    app.state.wiring = settings
    app.state.version = version
    # One result message per signed-in browser, said once on the next page it opens.
    app.state.flashes = {}

    def refuse(status: int, message: str) -> HTMLResponse:
        return HTMLResponse(
            public_page(
                "Not done",
                "<div><h1>That was not done</h1></div>"
                + ui.banner("bad", message)
                + f'<div class="actions">{ui.go("Back to Admin", "/admin", "primary")}</div>',
            ),
            status,
        )

    @app.middleware("http")
    async def protect_forms(request: Request, call_next: RequestResponseEndpoint) -> Response:
        cookie = request.cookies.get(CSRF_COOKIE, "")
        session = request.cookies.get(COOKIE, "")
        path = request.url.path.rstrip("/") or "/"
        if path in PUBLIC:
            token = cookie if re.fullmatch(r"[a-f0-9]{64}", cookie) else secrets.token_hex(32)
        elif session:
            token = _derive_csrf(request.app.state.sessions, session)
        else:
            token = secrets.token_hex(32)
        context = csrf_token.set(token)
        try:
            if request.method == "POST" and is_upload(request):
                if path not in UPLOADS:
                    # Only the pages that take a file read a body of this kind, and only
                    # they check the token inside it. Anywhere else it would go unchecked.
                    return refuse(
                        403,
                        "Admin could not be sure that request came from you, so nothing "
                        "was changed. Go back, reload the page and try again.",
                    )
                # A file is streamed by its own handler, which checks the same token
                # before it keeps a single byte of the file.
                request.state.csrf_expected = token
            elif request.method == "POST":
                try:
                    # Cache the bounded body so route handlers use the same form parser.
                    body = bytearray()
                    async for chunk in request.stream():
                        body += chunk
                        if len(body) > MAX_FORM_BYTES:
                            raise SubmissionTooLarge
                    request._body = bytes(body)
                    fields = await submitted(request)
                except SubmissionTooLarge:
                    return refuse(413, "That was too much to send in one go. Nothing was changed.")
                supplied = fields.get("csrf", "")
                if not supplied or not secrets.compare_digest(supplied.encode(), token.encode()):
                    return refuse(
                        403,
                        "This page was open too long, so Admin could not be sure the request "
                        "came from you. Nothing was changed. Go back, reload the page and "
                        "try again.",
                    )
            response = await call_next(request)
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
            if token != cookie:
                response.set_cookie(
                    CSRF_COOKIE, token, secure=True, httponly=True, samesite="strict"
                )
            return response
        finally:
            csrf_token.reset(context)

    @app.exception_handler(Exception)
    async def fault(request: Request, exc: Exception) -> HTMLResponse:
        log.error("admin page failed: %s %s", request.method, request.url.path, exc_info=exc)
        return HTMLResponse(
            public_page(
                "Something went wrong",
                "<div><h1>Something went wrong in Admin</h1></div>"
                + ui.banner(
                    "bad",
                    "Admin hit a fault of its own while doing that, so it may not have been "
                    "done. Your notes are not affected. Go back and look at the page before "
                    "trying again.",
                )
                + f'<div class="actions">{ui.go("Back to Admin", "/admin", "primary")}</div>'
                + ui.tech(f"{request.method} {request.url.path}: {type(exc).__name__}"),
            ),
            status_code=500,
        )

    log.info("admin started")

    def unreadable(path: Path, problem: str, remedy: str) -> HTMLResponse:
        return problem_page(
            "Admin cannot start",
            f"Admin could not read {path.name}, so nobody can sign in right now.",
            f"{path}: {problem}",
            remedy,
        )

    @app.middleware("http")
    async def require_session(request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path.rstrip("/") or "/"
        if path in PUBLIC or path == "/":
            return await call_next(request)
        if not credentials.is_claimed():
            return RedirectResponse("/admin/welcome", status_code=303)
        token = request.cookies.get(COOKIE, "")
        if token:
            try:
                signed_in = request.app.state.sessions.valid(token)
            except AdminRecordUnreadable as exc:
                return unreadable(credentials.path, str(exc), ADMIN_RECORD_REMEDY)
            if signed_in:
                return await call_next(request)
            ended = RedirectResponse("/admin/signin?ended=1", status_code=303)
            ended.delete_cookie(COOKIE, path="/")
            return ended
        return RedirectResponse("/admin/signin", status_code=303)

    def start_session(target: str) -> Response:
        try:
            product = product_settings()
        except FileNotFoundError:
            product = ProductSettings()
        except (OSError, ValueError) as exc:
            return unreadable(state.path_for("settings"), rejection_detail(exc), SETTINGS_REMEDY)
        try:
            token = app.state.sessions.create(timedelta(hours=product.admin.session_hours))
        except AdminRecordUnreadable as exc:
            return unreadable(credentials.path, str(exc), ADMIN_RECORD_REMEDY)
        response: Response = RedirectResponse(target, status_code=303)
        response.set_cookie(COOKIE, token, httponly=True, samesite="strict", secure=True, path="/")
        return response

    app.state.start_session = start_session

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        # Admin itself sends anyone who is not signed in on to the sign-in page.
        return RedirectResponse("/admin" if credentials.is_claimed() else "/admin/welcome")

    @app.get("/admin/welcome", response_class=HTMLResponse, include_in_schema=False)
    async def welcome(request: Request) -> Response:
        reset = credentials.reset_offered()
        if credentials.is_claimed() and not reset:
            return RedirectResponse("/admin/signin", status_code=303)
        code = request.query_params.get("code", "")
        title = "Choose a new password" if reset else "Welcome"
        return HTMLResponse(public_page(title, welcome_body(code, {}, reset=reset)))

    @app.post("/admin/welcome", include_in_schema=False)
    async def claim(request: Request) -> Response:
        reset = credentials.reset_offered()
        if credentials.is_claimed() and not reset:
            return RedirectResponse("/admin/signin?claimed=1", status_code=303)
        body = await submitted(request)
        code, first, second = body.get("code", ""), body.get("pw1", ""), body.get("pw2", "")
        errors: dict[str, str] = {}
        if not code.strip():
            errors["code"] = "Enter the setup code."
        if len(first) < MIN_PASSWORD:
            errors["pw1"] = f"Use at least {MIN_PASSWORD} characters. You typed {len(first)}."
        elif first != second:
            errors["pw2"] = "The two passwords are different. Type the same one twice."
        title = "Choose a new password" if reset else "Welcome"

        def again(status: int = 422, banner: str = "") -> HTMLResponse:
            return HTMLResponse(
                public_page(title, welcome_body(code, errors, reset=reset, banner=banner)), status
            )

        if errors:
            return again()
        try:
            if reset:
                await credentials.reset(code, first)
            else:
                await credentials.claim(code, first)
        except AlreadyClaimed:
            return RedirectResponse("/admin/signin?claimed=1", status_code=303)
        except InvalidClaimCode:
            errors["code"] = (
                "That setup code was not accepted. Check it against the link Coppermind "
                "showed, or open that link again."
            )
            return again()
        except (ClaimCodeUnreadable, ClaimStateUnwritable) as exc:
            return _claim_refused(exc)
        except AdminRecordUnreadable as exc:
            return unreadable(credentials.path, str(exc), ADMIN_RECORD_REMEDY)
        zone = body.get("tz", "")
        if not reset and zone:
            # The browser's time zone is a better first guess than a fixed default.
            with suppress(OSError, ValueError, KeyError):
                ZoneInfo(zone)
                current = state.read("settings")
                document = dict(current.body)
                document.setdefault("general", {})["timezone"] = zone
                ProductSettings.model_validate(
                    {k: v for k, v in document.items() if k != "revision"}
                )
                state.write("settings", document, if_revision=current.revision)
        response = start_session("/admin" if reset else "/admin/setup")
        cookie = _session_cookie(response)
        if cookie:
            app.state.flashes[cookie] = {
                "kind": "ok",
                "html": "New password saved. You are signed in. Other browsers were signed out."
                if reset
                else "Password saved. You are signed in.",
            }
        return response

    def _session_cookie(response: Response) -> str:
        for name, value in response.raw_headers:
            if name == b"set-cookie" and value.startswith(COOKIE.encode() + b"="):
                return value.decode().split(";", 1)[0].split("=", 1)[1]
        return ""

    app.state.session_cookie = _session_cookie

    def _claim_refused(error: ClaimStateProblem) -> HTMLResponse:
        if isinstance(error, ClaimCodeUnreadable):
            return problem_page(
                "Setup could not finish",
                "Admin could not read its setup code, so setup did not happen.",
                f"{error.path}: {error.problem}",
                CLAIM_CODE_REMEDY,
            )
        return problem_page(
            "Setup could not finish",
            "Admin could not save your password, so setup did not happen.",
            f"{error.path}: {error.problem}",
            CLAIM_RECORD_REMEDY,
        )

    @app.get("/admin/signin", response_class=HTMLResponse, include_in_schema=False)
    async def signin_page(request: Request) -> Response:
        if not credentials.is_claimed():
            return RedirectResponse("/admin/welcome", status_code=303)
        token = request.cookies.get(COOKIE, "")
        with suppress(AdminRecordUnreadable):
            if token and request.app.state.sessions.valid(token):
                # Already signed in: asking again for the password would only be confusing.
                return RedirectResponse("/admin", status_code=303)
        notice = ""
        if request.query_params.get("ended"):
            notice = ui.banner("info", "You were signed out because the sign-in ran out.")
        elif request.query_params.get("claimed"):
            notice = ui.banner(
                "info", "Setup was already done on this server. Sign in with the Admin password."
            )
        elif request.query_params.get("out"):
            notice = ui.banner("ok", "You are signed out.")
        return HTMLResponse(public_page("Sign in", signin_body("", notice)))

    @app.post("/admin/signin", include_in_schema=False)
    async def signin(request: Request) -> Response:
        if not credentials.is_claimed():
            return RedirectResponse("/admin/welcome", status_code=303)
        password = (await submitted(request)).get("password", "")
        if not password:
            return HTMLResponse(
                public_page("Sign in", signin_body("Enter your password.")), status_code=422
            )
        try:
            verified = await credentials.verify_password(password)
        except AdminRecordUnreadable as exc:
            return unreadable(credentials.path, str(exc), ADMIN_RECORD_REMEDY)
        if not verified:
            return HTMLResponse(
                public_page("Sign in", signin_body("That password was not accepted. Try again.")),
                status_code=401,
            )
        return start_session("/admin")

    @app.post("/admin/signout", include_in_schema=False)
    async def signout(request: Request) -> Response:
        app.state.flashes.pop(request.cookies.get(COOKIE, ""), None)
        response = RedirectResponse("/admin/signin?out=1", status_code=303)
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

    from coppermind_admin.pages import register

    register(app, settings)
    return app


app = create_app()
