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
    CitedNote,
    Page,
    ReconcilerNotice,
    StoreError,
)
from coppermind_admin.pages.problems import UNREACHABLE, _notices_html, store_client

DEFAULT_TIMEZONE = "America/Chicago"
NOTICES = {
    "removed": "The artifact was removed. It is gone from Obsidian and from search; "
    "the stored file is kept until the retention purge.",
    "removed_link": "The link was removed. It is no longer listed or found in search; "
    "notes that mention it keep their address.",
    "missing": "No such artifact; it may already have been removed.",
}
INTRO = (
    "<p>Files and links kept beside your notes. Each file is stored exactly as it arrived and "
    "shown in Obsidian under the artifacts folder, in the folder for the month it came in; a "
    "link is kept by its address. A note that uses one names it, and shows here under "
    "<em>Cited by</em>. Removing a file takes it out of Obsidian and search, and the stored file "
    "is kept until the retention purge removes it; removing a link only takes it out of this list "
    "and search.</p>"
)
TEXT_LABELS = {
    "extracted": "extracted",
    "no_extractable_text": "no text found",
    "unsupported": "not a text type",
    "failed": "extraction failed",
    "link": "link",
}
# Column widths that keep the table inside a 1280 pixel window with the sidebar open:
# dates, sizes and the Remove button on one line, paths wrapping only after a slash.
COLUMNS = (
    ("Title", 21),
    ("Received", 12),
    ("Size", 9),
    ("Where", 22),
    ("Cited by", 14),
    ("Text", 11),
    ("", 11),
)


class ArtifactControl(Protocol):
    """The part of the store contract this page uses."""

    async def list_artifacts(self, query: ArtifactQuery) -> Page[ArtifactSummary]: ...

    async def get_artifact(self, artifact_id: str) -> ArtifactRecord: ...

    async def remove_artifact(self, artifact_id: str, removal: str = "admin") -> None: ...

    async def list_reconciler_notices(self) -> list[ReconcilerNotice]: ...


def _client(request: Request, wiring: Wiring) -> ArtifactControl:
    return cast(ArtifactControl, store_client(request, wiring))


def _zone(request: Request) -> ZoneInfo:
    try:
        return ZoneInfo(read_settings(request.app.state.control).general.timezone)
    except Exception:  # noqa: BLE001 - a time is still worth showing in the default zone
        return ZoneInfo(DEFAULT_TIMEZONE)


def local_day(moment: datetime, zone: ZoneInfo) -> str:
    """A received date short enough to sit on one line in a table: `Nov 4, 2026`."""
    local = moment.astimezone(zone)
    return f"{local:%b} {local.day}, {local:%Y}"


def human_size(size: int) -> str:
    """`812 B`, `793 KiB`, `3.0 MiB`: one decimal only while it says something."""
    if size < 1024:
        return f"{size} B"
    for unit in ("KiB", "MiB", "GiB"):
        size_f = size / 1024
        if size_f < 1024 or unit == "GiB":
            return f"{size_f:.1f} {unit}" if size_f < 10 else f"{size_f:.0f} {unit}"
        size = int(size_f)
    return f"{size} B"


def _citing(item: ArtifactSummary) -> str:
    """The citing notes by title, the way the person knows them, not by identifier."""
    if not item.citing_notes and not item.citing_note_ids:
        return '<span class="muted">no note yet</span>'
    notes = item.citing_notes or [
        CitedNote(id=note_id, title=note_id, path="") for note_id in item.citing_note_ids
    ]
    return "<br>".join(
        f'<a href="/admin/notes/{quote(note.id, safe="")}">{escape(note.title)}</a>'
        for note in notes
    )


def _breakable(path: str) -> str:
    """A path that wraps only after a slash, never in the middle of a name."""
    return "/<wbr>".join(escape(part) for part in path.split("/"))


def _where(item: ArtifactSummary) -> str:
    if item.path:
        return f'<code class="path">{_breakable(item.path)}</code>'
    if item.url:
        return (
            f'<a class="path" href="{escape(item.url, quote=True)}" rel="noreferrer">'
            f"{escape(item.url)}</a>"
        )
    return ""


def _origin(item: ArtifactSummary) -> str:
    """Where it came from, said only when that is worth knowing; `api` is the ordinary case."""
    if item.origin == "api":
        return ""
    if item.origin.startswith("source:"):
        came = f"from a {escape(item.origin.removeprefix('source:'))} source"
    else:
        came = f"from {escape(item.origin)}"
    return f'<br><small class="muted">{came}</small>'


def rows_html(items: list[ArtifactSummary], zone: ZoneInfo) -> str:
    if not items:
        return (
            "<p>Nothing here yet. An artifact arrives through the companion plugin, an agent "
            "or the Dropbox folder.</p>"
        )
    rows = []
    for item in items:
        state = {
            "modified": '<br><small class="state">the copy in Obsidian was changed</small>',
            "missing": '<br><small class="state">the copy in Obsidian is missing</small>',
        }.get(item.state, "")
        size = human_size(item.size_bytes) if item.filename else "link"
        rows.append(
            "<tr>"
            f"<td>{escape(item.title)}{_origin(item)}</td>"
            f'<td class="nowrap">{escape(local_day(item.received_at, zone))}</td>'
            f'<td class="nowrap">{escape(size)}</td>'
            f"<td>{_where(item)}{state}</td>"
            f"<td>{_citing(item)}</td>"
            f"<td>{escape(TEXT_LABELS.get(item.text_status, item.text_status))}"
            f'<br><small class="muted">revision {item.revision}</small></td>'
            f'<td class="nowrap"><a class="button" '
            f'href="/admin/artifacts/{quote(item.id, safe="")}/remove">Remove</a></td>'
            "</tr>"
        )
    columns = "".join(f'<col style="width:{width}%">' for _, width in COLUMNS)
    headings = "".join(f"<th>{escape(name)}</th>" for name, _ in COLUMNS)
    return (
        f'<table class="artifacts"><colgroup>{columns}</colgroup>'
        f"<thead><tr>{headings}</tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )


def notices_html(notices: list[ReconcilerNotice], zone: ZoneInfo) -> str:
    """Artifacts moved in Obsidian and returned, the same notices the overview shows."""
    moved = [notice for notice in notices if notice.kind == "artifact"]
    if not moved:
        return ""
    return f"<h2>Moved back ({len(moved)})</h2>" + _notices_html(
        moved, zone, back="/admin/artifacts"
    )


def router(wiring: Wiring) -> APIRouter:
    routes = APIRouter()

    def page(title: str, body: str, status_code: int = 200, *, wide: bool = False) -> HTMLResponse:
        from coppermind_admin.main import page as render

        return HTMLResponse(render(title, body, wide=wide), status_code=status_code)

    @routes.get("/admin/artifacts", response_class=HTMLResponse, include_in_schema=False)
    async def artifacts_page(request: Request) -> Response:
        heading = '<h1>Artifacts</h1><p><a href="/admin">Overview</a></p>'
        cursor = request.query_params.get("cursor") or None
        try:
            client = _client(request, wiring)
            listed = await client.list_artifacts(ArtifactQuery(limit=100, cursor=cursor))
            notices = await client.list_reconciler_notices()
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
        zone = _zone(request)
        return page(
            "Artifacts",
            f"{heading}{notice}{INTRO}{notices_html(notices, zone)}"
            f"{rows_html(listed.items, zone)}{more}",
            wide=True,
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
        cited = (
            ", ".join(escape(note.title) for note in record.citing_notes)
            or ", ".join(escape(note_id) for note_id in record.citing_note_ids)
            or "no note cites it"
        )
        if record.path:
            where = f"<li>File in Obsidian: <code>{escape(record.path)}</code></li>"
            kept = f"<li>Revisions kept in the archive: {len(record.revisions)}</li>"
            effect = (
                "<p>Removing deletes the copy under the artifacts folder in Obsidian and takes "
                "the artifact out of search. Notes that cite it keep their citation and their "
                "link, which then points at nothing. The stored file itself is kept, marked "
                "removed, until the retention purge removes it after the retention window.</p>"
            )
            confirm = "I understand the file leaves Obsidian"
        else:
            where = f"<li>Link: {escape(record.url or '')}</li>"
            kept = ""
            effect = (
                "<p>This is a link, not a file, so nothing in Obsidian changes. Removing takes "
                "it off this list and out of search. Notes that cite it keep their citation and "
                "the address they already have. The record is kept, marked removed, until the "
                "retention purge removes it after the retention window.</p>"
            )
            confirm = "I understand the link will no longer be listed or found in search"
        return page(
            "Remove artifact",
            f"""<h1>Remove artifact</h1><p><a href="/admin/artifacts">Artifacts</a></p>
<ul><li>Title: {escape(record.title)}</li><li>Identifier: <code>{escape(record.id)}</code></li>
{where}<li>Cited by: {cited}</li>{kept}</ul>
{effect}
<form method="post" action="/v1/admin/artifacts/{quote(artifact_id, safe="")}/remove">
<label><input type="checkbox" name="confirm" value="yes" required> {confirm}</label>
<button>Remove {"link" if record.url and not record.path else "artifact"}</button></form>""",
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
            client = _client(request, wiring)
            record = await client.get_artifact(artifact_id)
            await client.remove_artifact(artifact_id)
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
        outcome = "removed_link" if record.url and not record.path else "removed"
        return RedirectResponse(f"/admin/artifacts?notice={outcome}", status_code=303)

    return routes
