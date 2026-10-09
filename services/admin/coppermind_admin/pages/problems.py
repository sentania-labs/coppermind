"""Overview counters, the problems dashboard and the pages its links open.

Every number and every problem is computed by the store from its metadata
mirror and the rejections it recorded. Admin only reads: nothing here asks the
store to change a note, and nothing is ever written into one (operator
decision of 2026-10-01).
"""

from __future__ import annotations

from html import escape
from typing import Protocol
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from coppermind.errors import to_http
from coppermind.settings import Wiring
from coppermind.store_client import HttpStoreClient
from coppermind.store_protocol import (
    NoteSourceInfo,
    ProblemInfo,
    SourceManifest,
    StatusResponse,
    StoreError,
)

UNREACHABLE = "The store could not answer. Check that the store and PostgreSQL are running."


class StatusReader(Protocol):
    """The read-only part of the store contract these pages use."""

    async def get_status(self) -> StatusResponse: ...

    async def get_problems(self) -> list[ProblemInfo]: ...

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


def _link(problem: ProblemInfo) -> str:
    if problem.note_id:
        target = f"/admin/notes/{quote(problem.note_id, safe='')}"
        return f'<a href="{escape(target)}">note {escape(problem.note_id)}</a>'
    if problem.source_id:
        target = f"/admin/sources/{quote(problem.source_id, safe='')}"
        return f'<a href="{escape(target)}">source {escape(problem.source_id)}</a>'
    return ""


def _problem_rows(problems: list[ProblemInfo]) -> str:
    if not problems:
        return "<p>No problems found.</p>"
    rows = "".join(
        f"<tr><td>{escape(item.kind)}</td><td>{escape(item.reference)}</td>"
        f"<td>{escape(item.reason)}</td><td>{_link(item)}</td></tr>"
        for item in problems
    )
    return (
        "<table><thead><tr><th>Kind</th><th>Reference</th><th>Reason</th><th>Open</th></tr>"
        f"</thead><tbody>{rows}</tbody></table>"
    )


def counters_html(status: StatusResponse) -> str:
    counters = status.counters
    problems = counters.rejected_ingests + counters.name_collisions + counters.unparseable_files
    states = ", ".join(
        f"{escape(state)}: {count}" for state, count in sorted(counters.notes_by_state.items())
    )
    return f"""<h2>Store Status</h2>
<ul>
<li>Notes awaiting review: {counters.notes_awaiting_review}</li>
<li>Notes by state: {states or "none"}</li>
<li>Sources: {counters.sources}</li>
<li>Rejected ingests: {counters.rejected_ingests}</li>
<li>Name collisions: {counters.name_collisions}</li>
<li>Unparseable files: {counters.unparseable_files}</li>
<li>Excluded (skipped by exclusion rules): {counters.excluded_count}</li>
</ul>
<p><a href="/admin/problems">Problems ({problems})</a></p>"""


async def overview_counters(request: Request, wiring: Wiring) -> str:
    try:
        status = await store_client(request, wiring).get_status()
    except (StoreError, OSError):
        return f'<p class="error">{UNREACHABLE}</p>'
    return counters_html(status)


def router(wiring: Wiring) -> APIRouter:
    routes = APIRouter()

    def page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
        from coppermind_admin.main import page as render

        return HTMLResponse(render(title, body), status_code=status_code)

    def unreachable(title: str) -> HTMLResponse:
        return page(title, f'<h1>{escape(title)}</h1><p class="error">{UNREACHABLE}</p>', 503)

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
            "Nothing is written into a note.</p>" + _problem_rows(problems),
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
