"""The search index page: its state and the button that rebuilds it.

The index is derived from the notes filesystem, so rebuilding it is always
safe: the store throws every entry away and reads the files again, in the
background, while search keeps answering from the old entries until the new
ones are committed. Times are shown in the operator's timezone.
"""

from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Protocol, cast
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from coppermind.settings import Wiring, read_settings
from coppermind.store_protocol import RebuildSearchIndexResult, SearchIndexStatus, StoreError
from coppermind_admin.pages.problems import UNREACHABLE, store_client

DEFAULT_TIMEZONE = "America/Chicago"

NOTICES = {
    "started": "Rebuild started. The counts below change when it finishes.",
    "running": "A rebuild is already running.",
}


class SearchIndexControl(Protocol):
    """The part of the store contract this page uses."""

    async def get_search_index_status(self) -> SearchIndexStatus: ...

    async def rebuild_search_index(self) -> RebuildSearchIndexResult: ...


def _client(request: Request, wiring: Wiring) -> SearchIndexControl:
    return cast(SearchIndexControl, store_client(request, wiring))


def _zone(request: Request) -> ZoneInfo:
    """The operator's timezone, or the shipped default if settings cannot load."""
    try:
        return ZoneInfo(read_settings(request.app.state.control).general.timezone)
    except Exception:  # noqa: BLE001 - a time is still worth showing in the default zone
        return ZoneInfo(DEFAULT_TIMEZONE)


def local_time(moment: datetime | None, zone: ZoneInfo) -> str:
    """A time as a person reads it, for example 'October 9, 2026, 2:05 PM CDT'."""
    if moment is None:
        return "never"
    local = moment.astimezone(zone)
    hour = local.strftime("%I").lstrip("0") or "12"
    return f"{local:%B} {local.day}, {local:%Y}, {hour}:{local:%M %p %Z}"


def status_html(status: SearchIndexStatus, zone: ZoneInfo) -> str:
    rebuild = (
        f"running since {escape(local_time(status.rebuild_started_at, zone))}"
        if status.rebuild_running
        else f"last finished {escape(local_time(status.rebuild_completed_at, zone))}"
    )
    failure = (
        f'<p class="error">The last rebuild did not finish: {escape(status.rebuild_error)}</p>'
        if status.rebuild_error
        else ""
    )
    return f"""<ul>
<li>Notes indexed: {status.notes}</li>
<li>Source pages indexed: {status.sources}</li>
<li>Last updated: {escape(local_time(status.last_updated_at, zone))}</li>
<li>Rebuild: {rebuild}</li>
</ul>{failure}"""


def router(wiring: Wiring) -> APIRouter:
    routes = APIRouter()

    def page(body: str, status_code: int = 200) -> HTMLResponse:
        from coppermind_admin.main import page as render

        return HTMLResponse(render("Search index", body), status_code=status_code)

    @routes.get("/admin/search-index", response_class=HTMLResponse, include_in_schema=False)
    async def search_index_page(request: Request) -> Response:
        heading = '<h1>Search index</h1><p><a href="/admin">Overview</a></p>'
        try:
            status = await _client(request, wiring).get_search_index_status()
        except (StoreError, OSError):
            return page(f'{heading}<p class="error">{UNREACHABLE}</p>', 503)
        message = NOTICES.get(request.query_params.get("notice", ""), "")
        notice = f"<p>{escape(message)}</p>" if message else ""
        return page(
            f"""{heading}{notice}
<p>Full-text search over note bodies and titles, and over the generated source
pages. The index is rebuilt from the notes filesystem, never the other way
round, so rebuilding it loses nothing.</p>
{status_html(status, _zone(request))}
<form method="post" action="/v1/admin/search-index/rebuild"><button>Rebuild index</button></form>"""
        )

    @routes.post("/v1/admin/search-index/rebuild", include_in_schema=False)
    async def rebuild(request: Request) -> Response:
        try:
            result = await _client(request, wiring).rebuild_search_index()
        except (StoreError, OSError):
            return page(
                '<h1>Search index</h1><p><a href="/admin/search-index">Search index</a></p>'
                f'<p class="error">{UNREACHABLE} Nothing was rebuilt.</p>',
                503,
            )
        notice = "started" if result.started else "running"
        return RedirectResponse(f"/admin/search-index?notice={notice}", status_code=303)

    return routes
