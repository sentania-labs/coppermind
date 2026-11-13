"""What every Admin page handler is given: the store, the settings, and the page frame.

A handler builds the body of its page and asks `Ctx.page` to frame it. The frame
reads the overview once per request, so the menu's counts and the page's own
numbers are the same answer. A result message is said once, on the page the
person lands on after pressing the button.
"""

from __future__ import annotations

import re
import secrets
from contextvars import ContextVar
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from coppermind.settings import ProductSettings, Wiring, read_settings
from coppermind.statefiles import RevisionConflict, StateStore
from coppermind.store_client import HttpStoreClient
from coppermind.store_protocol import StoreError, ValidationFailed
from coppermind_admin import layout, model, ui

COOKIE = "coppermind_admin_session"
csrf_token: ContextVar[str] = ContextVar("csrf_token", default="")
POST_FORM = re.compile(r'(<form\b[^>]*\bmethod="post"[^>]*>)')
UNREACHABLE = (
    "<b>Coppermind is not answering.</b> The part of Coppermind that holds your notes did "
    "not reply, so this page cannot show what is true right now. Your notes are safe. "
    "Wait a minute and reload. If it stays like this, whoever runs the server should check "
    "that Coppermind is running."
)


STALE = "This changed since you opened it. Reload and try again."


class StaleForm(RevisionConflict):
    """A save from a form drawn before the thing it edits last changed."""


def not_saved(error: Exception, plain: str) -> str:
    """Why a settings change was not kept: the stale-form sentence, else `plain`."""
    return STALE if isinstance(error, RevisionConflict) else plain


def explain(error: Exception, lead: str, after: str = "") -> str:
    """What to say when the store could not do something: a plain sentence, never the raw error.

    A refusal the store worded for a person is shown as it is. Anything else is said as
    "did not finish, try again", with the raw text behind Details for support.
    """
    raw = str(error).strip()
    if isinstance(error, RevisionConflict):
        return " ".join(part for part in (lead, STALE, after) if part)
    if isinstance(error, ValidationFailed | ValueError) and raw:
        return " ".join(part for part in (lead, ui.e(raw), after) if part)
    said = " ".join(
        part
        for part in (
            lead,
            "Coppermind did not finish it. Wait a minute, then try again.",
            after,
        )
        if part
    )
    return said + (ui.tech(raw) if raw else "")


def with_csrf(document: str, revisions: str = "") -> str:
    """Give every form that changes something the token that proves it came from Admin, and
    the revisions of the settings it was drawn from, so a stale form is refused when saved."""
    token = csrf_token.get()
    seen = f'<input type="hidden" name="rev" value="{revisions}">' if revisions else ""
    return POST_FORM.sub(
        lambda match: match[0] + f'<input type="hidden" name="csrf" value="{token}">' + seen,
        document,
    )


def read_pins(text: str) -> dict[str, int]:
    pins: dict[str, int] = {}
    for part in text.split(";"):
        name, _, number = part.partition("=")
        if name in {"settings", "schema"} and number.isascii() and number.isdigit():
            pins[name] = int(number)
    return pins


def store_client(request: Request, wiring: Wiring) -> HttpStoreClient:
    """The store client, built on first use so Admin starts without the store."""
    client = getattr(request.app.state, "store", None)
    if client is None:
        client = HttpStoreClient(
            wiring.store_url, wiring.read_internal_token(), timeout=wiring.store_timeout_s
        )
        request.app.state.store = client
    return client


class Ctx:
    def __init__(self, request: Request, wiring: Wiring) -> None:
        self.request = request
        self.wiring = wiring
        self.control: StateStore = request.app.state.control
        self._view: dict[str, Any] | None = None
        self._view_failed = False
        self._form: dict[str, str] | None = None
        self._pins: dict[str, int] = {}

    # -- what the page reads -------------------------------------------------------------

    @property
    def store(self) -> Any:
        return store_client(self.request, self.wiring)

    def settings(self) -> ProductSettings:
        try:
            return read_settings(self.control)
        except FileNotFoundError:
            return ProductSettings()

    @property
    def zone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.settings().general.timezone)
        except (OSError, ValueError):
            return ZoneInfo("America/Chicago")

    async def view(self) -> dict[str, Any] | None:
        """The overview, read once per request. None when the store did not answer."""
        if self._view is None and not self._view_failed:
            try:
                self._view = await self.store.admin_action("overview", {})
            except (StoreError, OSError):
                self._view_failed = True
        return self._view

    async def act(self, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.store.admin_action(action, payload or {})

    def q(self, name: str, default: str = "") -> str:
        return self.request.query_params.get(name, default)

    async def form(self) -> dict[str, str]:
        if self._form is None:
            from coppermind_admin.main import submitted

            self._form = await submitted(self.request)
            self._pins = read_pins(self._form.get("rev", ""))
        return self._form

    def revisions(self) -> str:
        """The revisions of the settings and the note schema a page is drawn from."""
        seen = []
        for name in ("settings", "schema"):
            try:
                seen.append(f"{name}={self.control.read(name).revision}")
            except FileNotFoundError:
                seen.append(f"{name}=0")
        return ";".join(seen)

    def pin(self, name: str) -> int | None:
        """The revision of `name` the form being saved was drawn from, spent once."""
        return self._pins.pop(name, None)

    def once(self) -> str:
        """A single-use token for a form: the first submit passes, a repeat is refused."""
        state = self.request.app.state
        bag: dict[str, list[str]] = getattr(state, "once", None) or {}
        state.once = bag
        tokens = bag.setdefault(self._session, [])
        token = secrets.token_hex(16)
        tokens.append(token)
        del tokens[:-100]
        return token

    def repeated(self, form: dict[str, str]) -> bool:
        """True when this form's single-use field was already spent by an earlier submit."""
        token = form.get("once")
        if token is None:
            return False
        tokens = getattr(self.request.app.state, "once", {}).get(self._session, [])
        if token in tokens:
            tokens.remove(token)
            return False
        return True

    # -- what the page says back ---------------------------------------------------------

    @property
    def _session(self) -> str:
        return self.request.cookies.get(COOKIE, "")

    def say(self, kind: str, html: str) -> None:
        """Leave one result message for the next page this person sees."""
        self.request.app.state.flashes[self._session] = {"kind": kind, "html": html}

    def flash(self) -> str:
        message = self.request.app.state.flashes.pop(self._session, None)
        return ui.banner(message["kind"], message["html"], fresh=True) if message else ""

    def go(self, to: str) -> RedirectResponse:
        return RedirectResponse(to, status_code=303)

    def back(self, default: str) -> str:
        """Where a button's result should land: the page that had the button."""
        target = (self._form or {}).get("back", "")
        return target if target.startswith("/admin") and "//" not in target else default

    async def page(
        self,
        title: str,
        body: str,
        active: str,
        *,
        dialog: str = "",
        refresh: int = 0,
        status: int = 200,
    ) -> HTMLResponse:
        view = await self.view()
        warning = "" if view is not None else ui.banner("bad", UNREACHABLE)
        document = layout.render(
            title,
            warning + body,
            active,
            model.menu(view),
            dialog=dialog,
            refresh=refresh,
        )
        return HTMLResponse(with_csrf(document, self.revisions()), status_code=status)

    async def unavailable(self, title: str, lede: str, active: str) -> HTMLResponse:
        """The page for when the store did not answer: say so, and change nothing."""
        self._view, self._view_failed = None, True
        return await self.page(title, ui.head(title, lede), active, status=503)

    async def refused(
        self, error: Exception, to: str, lead: str = "That did not work. Nothing was changed."
    ) -> Response:
        """An action the store would not do: say why on the page it was asked from."""
        self.say("bad", explain(error, lead))
        return self.go(to)

    # -- settings ------------------------------------------------------------------------

    def save_settings(self, change: Any) -> ProductSettings:
        """Apply `change` to the settings document and save it. A form drawn before the
        settings last changed is refused; a lost race is otherwise retried once."""
        pinned = self.pin("settings")
        for attempt in (1, 2):
            try:
                current = self.control.read("settings")
                document, revision = dict(current.body), current.revision
            except FileNotFoundError:
                document, revision = ProductSettings().model_dump(mode="json"), None
            if pinned is not None and (revision or 0) != pinned:
                raise StaleForm(revision or 0)
            change(document)
            validated = ProductSettings.model_validate(
                {key: value for key, value in document.items() if key != "revision"}
            )
            try:
                self.control.write("settings", document, if_revision=revision)
                return validated
            except RevisionConflict:
                if attempt == 2 or pinned is not None:
                    raise
        raise RevisionConflict(0)
