"""What every Admin page handler is given: the store, the settings, and the page frame.

A handler builds the body of its page and asks `Ctx.page` to frame it. The frame
reads the overview once per request, so the menu's counts and the page's own
numbers are the same answer. A result message is said once, on the page the
person lands on after pressing the button.
"""

from __future__ import annotations

import re
from contextvars import ContextVar
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from coppermind.settings import ProductSettings, Wiring, read_settings
from coppermind.statefiles import RevisionConflict, StateStore
from coppermind.store_client import HttpStoreClient
from coppermind.store_protocol import StoreError
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


def with_csrf(document: str) -> str:
    """Give every form that changes something the token that proves it came from Admin."""
    token = csrf_token.get()
    return POST_FORM.sub(
        lambda match: match[0] + f'<input type="hidden" name="csrf" value="{token}">', document
    )


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
        return self._form

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
        return HTMLResponse(with_csrf(document), status_code=status)

    async def unavailable(self, title: str, lede: str, active: str) -> HTMLResponse:
        """The page for when the store did not answer: say so, and change nothing."""
        self._view, self._view_failed = None, True
        return await self.page(title, ui.head(title, lede), active, status=503)

    async def refused(self, error: Exception, to: str) -> Response:
        """An action the store would not do: say why on the page it was asked from."""
        self.say("bad", ui.e(str(error)) or "That did not work. Nothing was changed.")
        return self.go(to)

    # -- settings ------------------------------------------------------------------------

    def save_settings(self, change: Any) -> ProductSettings:
        """Apply `change` to the settings document and save it, retrying a lost race once."""
        for attempt in (1, 2):
            try:
                current = self.control.read("settings")
                document, revision = dict(current.body), current.revision
            except FileNotFoundError:
                document, revision = ProductSettings().model_dump(mode="json"), None
            change(document)
            validated = ProductSettings.model_validate(
                {key: value for key, value in document.items() if key != "revision"}
            )
            try:
                self.control.write("settings", document, if_revision=revision)
                return validated
            except RevisionConflict:
                if attempt == 2:
                    raise
        raise RevisionConflict(0)
