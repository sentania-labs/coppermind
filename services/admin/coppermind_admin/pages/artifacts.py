"""The artifacts page: every stored artifact with its provenance, and a remove action.

Admin only reads the list; the one thing it changes is removing an artifact's
projection, through the store, after a confirmation page. The archived bytes
stay under the artifact's identity until the retention purge rules say
otherwise, so the action is recoverable by design.
"""

from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Protocol, cast
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from coppermind.errors import to_http
from coppermind.settings import Wiring, read_settings
from coppermind.store_protocol import (
    ArtifactQuery,
    ArtifactRecord,
    ArtifactSummary,
    Page,
    StoreError,
)
from coppermind_admin.pages.problems import UNREACHABLE, store_client

DEFAULT_TIMEZONE = "America/Chicago"
NOTICES = {
    "removed": "The artifact was removed: its browsable copy, record and search entry are gone. "
    "The archived bytes stay until retention purges them.",
    "missing": "No such artifact; it may already have been removed.",
}
TEXT_LABELS = {
    "extracted": "text extracted",
    "no_extractable_text": "no extractable text",
    "unsupported": "not a text type",
    "failed": "extraction failed",
    "link": "link",
}


class ArtifactControl(Protocol):
    """The part of the store contract this page uses."""

    async def list_artifacts(self, query: ArtifactQuery) -> Page[ArtifactSummary]: ...

    async def get_artifact(self, artifact_id: str) -> ArtifactRecord: ...

    async def remove_artifact(self, artifact_id: str) -> None: ...


def _client(request: Request, wiring: Wiring) -> ArtifactControl:
    return cast(ArtifactControl, store_client(request, wiring))


def _zone(request: Request) -> ZoneInfo:
    try:
        return ZoneInfo(read_settings(request.app.state.control).general.timezone)
    except Exception:  # noqa: BLE001 - a time is still worth showing in the default zone
        return ZoneInfo(DEFAULT_TIMEZONE)


def local_day(moment: datetime, zone: ZoneInfo) -> str:
    local = moment.astimezone(zone)
    return f"{local:%B} {local.day}, {local:%Y}"


def human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    for unit in ("KiB", "MiB", "GiB"):
        size_f = size / 1024
        if size_f < 1024 or unit == "GiB":
            return f"{size_f:.1f} {unit}"
        size = int(size_f)
    return f"{size} B"


def _citing(item: ArtifactSummary) -> str:
    if not item.citing_note_ids:
        return '<span class="muted">uncited</span>'
    return ", ".join(
        f'<a href="/admin/notes/{quote(note_id, safe="")}">{escape(note_id)}</a>'
        for note_id in item.citing_note_ids
    )


def _where(item: ArtifactSummary) -> str:
    if item.path:
        return f"<code>{escape(item.path)}</code>"
    if item.url:
        return f'<a href="{escape(item.url, quote=True)}" rel="noreferrer">{escape(item.url)}</a>'
    return ""


def rows_html(items: list[ArtifactSummary], zone: ZoneInfo) -> str:
    if not items:
        return "<p>No artifacts stored yet. Upload one with <code>POST /v1/artifacts</code>.</p>"
    rows = []
    for item in items:
        state = (
            ' <span class="notice">browsable copy changed on a device</span>'
            if item.state == "modified"
            else ""
        )
        rows.append(
            "<tr>"
            f"<td><strong>{escape(item.kind)}</strong></td>"
            f'<td>{escape(item.title)}<br><small class="muted">{escape(item.id)}</small></td>'
            f"<td>{escape(item.origin)}</td>"
            f"<td>{escape(local_day(item.received_at, zone))}</td>"
            f"<td>{escape(human_size(item.size_bytes)) if item.filename else 'link'}</td>"
            f"<td>{_where(item)}{state}</td>"
            f"<td>{_citing(item)}</td>"
            f"<td>{escape(TEXT_LABELS.get(item.text_status, item.text_status))}"
            f'<br><small class="muted">revision {item.revision}</small></td>'
            f'<td><a class="button" href="/admin/artifacts/{quote(item.id, safe="")}/remove">'
            "Remove</a></td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Kind</th><th>Title</th><th>Origin</th><th>Received</th>"
        "<th>Size</th><th>Where</th><th>Cited by</th><th>Text</th><th></th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def router(wiring: Wiring) -> APIRouter:
    routes = APIRouter()

    def page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
        from coppermind_admin.main import page as render

        return HTMLResponse(render(title, body), status_code=status_code)

    @routes.get("/admin/artifacts", response_class=HTMLResponse, include_in_schema=False)
    async def artifacts_page(request: Request) -> Response:
        heading = '<h1>Artifacts</h1><p><a href="/admin">Overview</a></p>'
        cursor = request.query_params.get("cursor") or None
        try:
            listed = await _client(request, wiring).list_artifacts(
                ArtifactQuery(limit=100, cursor=cursor)
            )
        except (StoreError, OSError):
            return page("Artifacts", f'{heading}<p class="error">{UNREACHABLE}</p>', 503)
        message = NOTICES.get(request.query_params.get("notice", ""), "")
        notice = f'<p class="success">{escape(message)}</p>' if message else ""
        more = (
            f'<p><a href="/admin/artifacts?cursor={quote(listed.next_cursor, safe="")}">'
            "Older artifacts</a></p>"
            if listed.next_cursor
            else ""
        )
        return page(
            "Artifacts",
            f"""{heading}{notice}
<p>Static files and links handed in beside notes, stored byte for byte under the artifacts folder
by received date. Every row says its kind, so a design package can never read as a meeting.
The extracted text lives in the database and the state volume, never as a page in the notes
filesystem. A note cites an artifact in its <code>artifacts</code> frontmatter.</p>
{rows_html(listed.items, _zone(request))}{more}""",
        )

    @routes.get(
        "/admin/artifacts/{artifact_id}/remove",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def confirm_remove(artifact_id: str, request: Request) -> Response:
        try:
            record = await _client(request, wiring).get_artifact(artifact_id)
        except StoreError as error:
            status_code, _ = to_http(error)
            if status_code == 404:
                return RedirectResponse("/admin/artifacts?notice=missing", status_code=303)
            return page(
                "Remove artifact",
                f'<h1>Remove artifact</h1><p class="error">{UNREACHABLE}</p>',
                503,
            )
        except OSError:
            return page(
                "Remove artifact",
                f'<h1>Remove artifact</h1><p class="error">{UNREACHABLE}</p>',
                503,
            )
        where = (
            f"<li>File: <code>{escape(record.path)}</code></li>"
            if record.path
            else f"<li>Link: {escape(record.url or '')}</li>"
        )
        cited = (
            ", ".join(escape(note_id) for note_id in record.citing_note_ids) or "no note cites it"
        )
        return page(
            "Remove artifact",
            f"""<h1>Remove artifact</h1><p><a href="/admin/artifacts">Artifacts</a></p>
<ul><li>Kind: <strong>{escape(record.kind)}</strong></li><li>Title: {escape(record.title)}</li>
<li>Origin: {escape(record.origin)}</li>{where}<li>Cited by: {cited}</li>
<li>Revisions kept in the archive: {len(record.revisions)}</li></ul>
<p>Removing takes the browsable copy, the extracted record and the search entry away on this
request. Notes that cite it keep their citation and their link, which then points at nothing.
The archived bytes stay under the artifact's identity until the retention purge rules decide.</p>
<form method="post" action="/v1/admin/artifacts/{quote(artifact_id, safe="")}/remove">
<label><input type="checkbox" name="confirm" value="yes" required> I understand the file leaves
the notes filesystem</label><button>Remove artifact</button></form>""",
        )

    @routes.post("/v1/admin/artifacts/{artifact_id}/remove", include_in_schema=False)
    async def remove(artifact_id: str, request: Request) -> Response:
        from coppermind_admin.main import submitted

        body = await submitted(request)
        if body.get("confirm") != "yes":
            return RedirectResponse(
                f"/admin/artifacts/{quote(artifact_id, safe='')}/remove", status_code=303
            )
        try:
            await _client(request, wiring).remove_artifact(artifact_id)
        except StoreError as error:
            status_code, _ = to_http(error)
            if status_code == 404:
                return RedirectResponse("/admin/artifacts?notice=missing", status_code=303)
            return page(
                "Artifacts",
                f'<h1>Artifacts</h1><p class="error">{UNREACHABLE} Nothing was removed.</p>',
                503,
            )
        except OSError:
            return page(
                "Artifacts",
                f'<h1>Artifacts</h1><p class="error">{UNREACHABLE} Nothing was removed.</p>',
                503,
            )
        return RedirectResponse("/admin/artifacts?notice=removed", status_code=303)

    return routes
