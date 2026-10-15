"""Readiness and recovery for declared frontmatter filters."""

from __future__ import annotations

from datetime import datetime
from html import escape
from typing import cast

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from coppermind.settings import Wiring
from coppermind.store_client import HttpStoreClient
from coppermind.store_protocol import StoreError
from coppermind_admin.pages.problems import UNREACHABLE, store_client
from coppermind_admin.pages.search_index import _zone, local_time


def router(wiring: Wiring) -> APIRouter:
    routes = APIRouter()

    def client(request: Request) -> HttpStoreClient:
        return cast(HttpStoreClient, store_client(request, wiring))

    def page(body: str, status: int = 200) -> HTMLResponse:
        from coppermind_admin.main import page as render

        return HTMLResponse(
            render("Field index", "<h1>Field index</h1>" + body), status_code=status
        )

    @routes.get("/admin/field-index", include_in_schema=False)
    async def status(request: Request, offset: int = Query(0, ge=0)) -> Response:
        try:
            snapshot = await client(request).field_index_action("status", offset)
        except (StoreError, OSError):
            return page(f'<p class="error">{UNREACHABLE}</p>', 503)
        job = snapshot["job"]
        fields = "".join(
            "<tr><td>"
            + escape(key["name"])
            + "</td><td>"
            + escape(key["kind"])
            + "</td><td>"
            + ("Ready" if key["ready"] else "Awaiting rebuild")
            + "</td></tr>"
            for key in snapshot["fields"]
        )
        invalid = "".join(
            "<tr><td>"
            + escape(row["path"])
            + "</td><td>"
            + escape(row["field"])
            + "</td><td>"
            + escape(row["invalid_reason"])
            + "</td></tr>"
            for row in snapshot["invalid"]
        )
        finished = job.get("completed_at")
        when = local_time(datetime.fromisoformat(finished), _zone(request)) if finished else "never"
        error = '<p class="error">' + escape(job["error"]) + "</p>" if job.get("error") else ""
        next_offset = snapshot["next_offset"]
        more = f'<a href="?offset={next_offset}">More invalid values</a>' if next_offset else ""
        return page(f"""<p>Filter notes and search by declared frontmatter fields.
New or changed fields become available after their existing values are indexed.</p>
<p>Rebuild: {escape(job["state"])}. Notes processed: {job["processed"]}.
Last completed: {escape(when)}.</p>{error}
<p>A rebuild reads the metadata mirror without changing note files. It may pause
metadata writes for up to 30 seconds. Existing ready filters remain available.</p>
<form method="post" action="/v1/admin/field-index/rebuild">
<button>Rebuild field index</button></form>
<table><thead><tr><th>Field</th><th>Type</th><th>Status</th></tr></thead><tbody>{fields}</tbody></table>
<h2>Invalid stored values ({snapshot["invalid_count"]})</h2>
<p>These values do not match filters. Correct the frontmatter in Obsidian;
reconciliation updates the index. Missing and null values are also excluded.</p>
<table><thead><tr><th>Note path</th><th>Field</th><th>Reason</th></tr></thead>
<tbody>{invalid}</tbody></table>{more}""")

    @routes.post("/v1/admin/field-index/rebuild", include_in_schema=False)
    async def rebuild(request: Request) -> Response:
        try:
            await client(request).field_index_action("rebuild")
        except (StoreError, OSError):
            return page(f'<p class="error">{UNREACHABLE}</p>', 503)
        return RedirectResponse("/admin/field-index", status_code=303)

    return routes
