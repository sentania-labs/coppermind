"""Overview counters, the problems dashboard and the pages its links open.

Every number and every problem is computed by the store from its metadata
mirror and the rejections it recorded. Admin only reads: nothing here asks the
store to change a note, and nothing is ever written into one (operator
decision of 2026-10-01). The one thing the overview lets the operator do is
dismiss the notice that a hand-moved note was returned, which changes no note.
"""

from __future__ import annotations

from collections.abc import Sequence
from html import escape
from typing import Protocol
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from coppermind.errors import to_http
from coppermind.settings import Wiring
from coppermind.store_client import HttpStoreClient
from coppermind.store_protocol import (
    NoteSourceInfo,
    ProblemInfo,
    ReconcilerNotice,
    SourceManifest,
    StatusResponse,
    StoreError,
)

UNREACHABLE = "The store could not answer. Check that the store and PostgreSQL are running."


class StatusReader(Protocol):
    """The read-only part of the store contract these pages use."""

    async def get_status(self) -> StatusResponse: ...

    async def get_problems(self) -> list[ProblemInfo]: ...

    async def list_reconciler_notices(self) -> list[ReconcilerNotice]: ...

    async def dismiss_reconciler_notice(self, note_id: str) -> None: ...

    async def get_note_sources(self, note_id: str) -> list[NoteSourceInfo]: ...

    async def get_source(self, source_id: str) -> SourceManifest: ...


def store_client(request: Request, wiring: Wiring) -> StatusReader:
    """The store client, built on first use so Admin starts without the store."""
    client = getattr(request.app.state, "store", None)
    if client is None:
        client = HttpStoreClient(
            wiring.store_url, wiring.read_internal_token(), timeout=wiring.store_timeout_s
        )
        request.app.state.store = client
    return client


KIND_LABELS = {
    "calendar_import": "Calendar import",
    "calendar_conflict": "Calendar conflict",
    "dropbox": "Dropbox",
    "purge": "Retention purge",
    "purge_unfinished": "Retention purge",
    "purge_receipt": "Retention purge receipt",
    "damaged_source": "Damaged source bundle",
}


def _link(problem: ProblemInfo) -> str:
    if problem.kind == "purge_receipt":
        return (
            '<form method="post" action="/v1/admin/purge/abandon">'
            f'<input type="hidden" name="token" value="{escape(problem.reference, quote=True)}">'
            "<button>Abandon this receipt</button></form>"
        )
    if problem.kind in {"purge", "purge_unfinished"}:
        return '<a href="/admin/purge">Retention purge</a>'
    if problem.kind == "dropbox":
        return '<a href="/admin/workflow?tab=intake">Dropbox receipts</a>'
    if problem.kind in {"calendar_import", "calendar_conflict"}:
        return '<a href="/admin/context?tab=calendar">Calendar import history</a>'
    if problem.kind == "damaged_source":
        return ""
    if problem.note_id:
        target = f"/admin/notes/{quote(problem.note_id, safe='')}"
        return f'<a href="{escape(target)}">note {escape(problem.note_id)}</a>'
    if problem.source_id:
        target = f"/admin/sources/{quote(problem.source_id, safe='')}"
        return f'<a href="{escape(target)}">source {escape(problem.source_id)}</a>'
    return ""


PURGE_KINDS = {"purge", "purge_unfinished", "purge_receipt"}


def _what(item: ProblemInfo) -> str:
    if item.kind == "damaged_source":
        return "Preserved source copy"
    return "Saved cleanup attempt" if item.kind in PURGE_KINDS else escape(item.reference)


def _problem_rows(problems: list[ProblemInfo], empty_text: str = "No problems found.") -> str:
    if not problems:
        return "<p>" + escape(empty_text) + "</p>"
    rows = "".join(
        f"<tr><td>{escape(KIND_LABELS.get(item.kind, item.kind))}</td>"
        f"<td>{_what(item)}</td>"
        f"<td>{escape(item.reason)}</td><td>{_link(item)}</td></tr>"
        for item in problems
    )
    return (
        "<table><thead><tr><th>Kind</th><th>Reference</th><th>Reason</th><th>Open</th></tr>"
        f"</thead><tbody>{rows}</tbody></table>"
    )


def _dismiss_form(notice: ReconcilerNotice) -> str:
    action = f"/v1/admin/notices/{quote(notice.note_id, safe='')}/dismiss"
    return f'<form method="post" action="{action}"><button>Dismiss</button></form>'


def _notices_html(notices: Sequence[ReconcilerNotice], zone: ZoneInfo) -> str:
    from coppermind_admin.pages.search_index import local_time

    if not notices:
        return ""
    items = ""
    for notice in notices:
        where = (
            f"found at <code>{escape(notice.from_path)}</code>, returned to "
            f"<code>{escape(notice.path)}</code>, {escape(local_time(notice.at, zone))}"
            if notice.kind == "note"
            else f"<code>{escape(notice.from_path)}</code>, {escape(local_time(notice.at, zone))}"
        )
        items += (
            f"<li><strong>{escape(notice.title)}</strong> "
            f'<span class="muted">{where}</span>'
            f"<br>{escape(notice.reason)}"
            f"{_dismiss_form(notice) if notice.kind == 'note' else ''}</li>"
        )
    return f"<ul>{items}</ul>"


def counters_html(
    status: StatusResponse,
    notices: Sequence[ReconcilerNotice] = (),
    zone: ZoneInfo = ZoneInfo("America/Chicago"),
) -> str:
    counters = status.counters
    problems = (
        counters.rejected_ingests
        + counters.name_collisions
        + counters.unparseable_files
        + counters.filing_failures
        + counters.known_unreadable
        + counters.calendar_import_failures
        + counters.purge_problems
    )
    if counters.active_problems is not None:
        problems = counters.active_problems
    states = ", ".join(
        f"{escape(state)}: {count}" for state, count in sorted(counters.notes_by_state.items())
    )
    return f"""<h2>Store Status</h2>
<div class="metrics">
<div class="metric" aria-label="Notes awaiting review: {counters.notes_awaiting_review}">
Notes awaiting review<strong>{counters.notes_awaiting_review}</strong>
<span class="muted">Review in Obsidian.</span></div>
<div class="metric" aria-label="Sources: {counters.sources}">
Preserved sources<strong>{counters.sources}</strong></div>
<div class="metric">Problems<strong>{problems}</strong>
<a href="/admin/problems">Problems ({problems})</a></div></div>
<ul>
<li>Notes by state: {states or "none"}</li>
<li>Sources: {counters.sources}</li>
<li>Rejected ingests: {counters.rejected_ingests}</li>
<li>Name collisions: {counters.name_collisions}</li>
<li>Unparseable files: {counters.unparseable_files}</li>
<li>Known unreadable search entries: {counters.known_unreadable}</li>
<li>Pending filing failures: {counters.filing_failures}</li>
<li>Failed calendar imports: {counters.calendar_import_failures}</li>
<li>Retention purge items needing attention: {counters.purge_problems}</li>
<li>Excluded (skipped by exclusion rules): {counters.excluded_count}</li>
<li>Notes root: {escape(counters.notes_folder or "Notes")}/</li>
<li>Notes the next filing pass will move: {counters.notes_to_refile}</li>
<li>Hand-moved notes returned to their filed folder: {counters.notes_returned}
{_notices_html([n for n in notices if n.kind == "note"], zone)}</li>
<li>Old month folders left in place because they are not empty: {counters.folders_kept}
{_notices_html([n for n in notices if n.kind == "folder"], zone)}</li>
</ul>
<p><a href="/admin/problems">Problems ({problems})</a></p>"""


async def overview_counters(request: Request, wiring: Wiring) -> str:
    from coppermind_admin.pages.search_index import _zone

    try:
        client = store_client(request, wiring)
        status = await client.get_status()
        counters = status.counters
        notices = (
            await client.list_reconciler_notices()
            if counters.notes_returned or counters.folders_kept
            else []
        )
    except (StoreError, OSError):
        return f'<p class="error">{UNREACHABLE}</p>'
    return counters_html(status, notices, _zone(request))


def router(wiring: Wiring) -> APIRouter:
    routes = APIRouter()

    def page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
        from coppermind_admin.main import page as render

        return HTMLResponse(render(title, body), status_code=status_code)

    def unreachable(title: str) -> HTMLResponse:
        return page(title, f'<h1>{escape(title)}</h1><p class="error">{UNREACHABLE}</p>', 503)

    @routes.post("/v1/admin/notices/{note_id}/dismiss", include_in_schema=False)
    async def dismiss_notice(note_id: str, request: Request) -> Response:
        try:
            await store_client(request, wiring).dismiss_reconciler_notice(note_id)
        except (StoreError, OSError):
            return unreachable("Overview")
        return RedirectResponse("/admin", status_code=303)

    @routes.get("/admin/problems", response_class=HTMLResponse, include_in_schema=False)
    async def problems_page(request: Request) -> Response:
        try:
            problems = await store_client(request, wiring).get_problems()
        except (StoreError, OSError):
            return unreachable("Problems")
        return page(
            "Problems",
            '<h1>Problems</h1><p><a href="/admin">Overview</a></p>'
            "<p>Computed from the metadata mirror and recorded rejections. "
            "Nothing is written into a note.</p>"
            "<p>Known unreadable search entries: "
            f"{sum(p.kind == 'search_unreadable' for p in problems)}</p>"
            f"<p>Files skipped by exclusion: {sum(p.kind == 'excluded' for p in problems)}</p>"
            + _problem_rows([item for item in problems if item.kind != "excluded"])
            + "<h2>Intentional exclusions</h2><p>These files were left untouched. "
            "An exclusion is not a failure. "
            '<a href="/admin/settings?section=reconcile">Manage exclusion rules</a>.</p>'
            + _problem_rows(
                [item for item in problems if item.kind == "excluded"], "No excluded files."
            ),
        )

    @routes.get("/admin/notes/{note_id}", response_class=HTMLResponse, include_in_schema=False)
    async def note_page(note_id: str, request: Request) -> Response:
        try:
            client = store_client(request, wiring)
            problems = [item for item in await client.get_problems() if item.note_id == note_id]
            sources = await client.get_note_sources(note_id)
        except StoreError as error:
            status_code, _ = to_http(error)
            if status_code == 404:
                return page("Note", "<h1>Note</h1><p>No such note.</p>", 404)
            return unreachable("Note")
        except OSError:
            return unreachable("Note")
        listed = "".join(
            f'<li><a href="/admin/sources/{quote(item.id, safe="")}">{escape(item.id)}</a>'
            f" {escape(item.projection_path or '')}</li>"
            for item in sources
        )
        return page(
            "Note",
            f'<h1>Note {escape(note_id)}</h1><p><a href="/admin/problems">Problems</a></p>'
            f"<h2>Problems</h2>{_problem_rows(problems)}"
            f"<h2>Sources</h2>{f'<ul>{listed}</ul>' if listed else '<p>None.</p>'}",
        )

    @routes.get("/admin/sources/{source_id}", response_class=HTMLResponse, include_in_schema=False)
    async def source_page(source_id: str, request: Request) -> Response:
        try:
            manifest = await store_client(request, wiring).get_source(source_id)
        except StoreError as error:
            status_code, _ = to_http(error)
            if status_code == 404:
                return page("Source", "<h1>Source</h1><p>No such source.</p>", 404)
            return unreachable("Source")
        except OSError:
            return unreachable("Source")
        return page(
            "Source",
            f"""<h1>Source {escape(manifest.source_id)}</h1>
<p><a href="/admin/problems">Problems</a></p>
<ul>
<li>Provider: {escape(manifest.provider)}</li>
<li>External ID: {escape(manifest.external_source_id)}</li>
<li>Type: {escape(manifest.source_type)}</li>
<li>Current revision: {manifest.current_revision}</li>
<li>Projection: {escape(manifest.projection_path or "")}</li>
</ul>""",
        )

    @routes.get("/v1/admin/status", include_in_schema=False)
    async def status_json(request: Request) -> Response:
        try:
            status = await store_client(request, wiring).get_status()
        except StoreError as error:
            status_code, body = to_http(error)
            return JSONResponse(status_code=status_code, content=body)
        return JSONResponse(status.model_dump(mode="json"))

    @routes.get("/v1/admin/notes/problems", include_in_schema=False)
    async def problems_json(request: Request) -> Response:
        try:
            problems = await store_client(request, wiring).get_problems()
        except StoreError as error:
            status_code, body = to_http(error)
            return JSONResponse(status_code=status_code, content=body)
        return JSONResponse([item.model_dump(mode="json") for item in problems])

    return routes
