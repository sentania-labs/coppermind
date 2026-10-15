"""Missing and deleted notes, with a previewed, exclusive restore action."""

from __future__ import annotations

from html import escape
from typing import Literal, Protocol, cast
from urllib.parse import parse_qs, quote, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from coppermind.errors import to_http
from coppermind.settings import Wiring
from coppermind.store_protocol import (
    HistoryStatus,
    NoteQuery,
    NoteRecovery,
    NoteSummary,
    Page,
    RestoreNote,
    StoreError,
)
from coppermind_admin.pages.problems import store_client
from coppermind_admin.pages.search_index import _zone, local_time


class HistoryControl(Protocol):
    async def get_note_history_status(self) -> HistoryStatus: ...
    async def list_notes(self, query: NoteQuery) -> Page[NoteSummary]: ...
    async def preview_note_restore(self, note_id: str) -> NoteRecovery: ...
    async def restore_note(self, note_id: str, request: RestoreNote) -> NoteRecovery: ...


def router(wiring: Wiring) -> APIRouter:
    routes = APIRouter()

    def client(request: Request) -> HistoryControl:
        return cast(HistoryControl, store_client(request, wiring))

    def page(body: str, status: int = 200) -> HTMLResponse:
        from coppermind_admin.main import page as render

        return HTMLResponse(render("Note recovery", body), status_code=status)

    def failure(error: StoreError) -> HTMLResponse:
        status, _ = to_http(error)
        return page(
            '<h1>Note recovery</h1><p class="error">'
            + escape(str(error))
            + '</p><a href="/admin/note-history">Back</a>',
            status,
        )

    @routes.get("/admin/note-history", include_in_schema=False)
    async def history(request: Request) -> Response:
        state: Literal["missing", "deleted"] = (
            "missing" if request.query_params.get("state") == "missing" else "deleted"
        )
        try:
            status = await client(request).get_note_history_status()
            notes = await client(request).list_notes(
                NoteQuery(state=state, cursor=request.query_params.get("cursor"), limit=50)
            )
        except StoreError as error:
            return failure(error)
        rows = []
        for note in notes.items:
            action = (
                f'<a href="/admin/note-history/{quote(note.id, safe="")}">Preview restore</a>'
                if state == "deleted"
                else "Waiting for matching committed removal"
            )
            rows.append(
                f"<tr><td>{escape(note.path)}<br><small>{escape(note.id)}</small></td>"
                f"<td>{escape(note.state_reason or note.state)}</td><td>{action}</td></tr>"
            )
        next_link = ""
        if notes.next_cursor:
            target = urlencode({"state": state, "cursor": notes.next_cursor})
            next_link = f'<a href="/admin/note-history?{escape(target)}">Next page</a>'
        notice = (
            '<p class="success">Restored. Obsidian Sync can now deliver this note to devices.</p>'
            if request.query_params.get("restored")
            else ""
        )
        return page(f"""<h1>Note recovery</h1>{notice}
<p>{escape(status.reason)}</p>
<p>Git helper: {escape(status.helper_status)}. Pending files: {status.dirty_files}.<br>
Last commit: {escape(local_time(status.last_commit_at, _zone(request)))}</p>
<p>Git records removals; it cannot establish intent. Deleted notes return automatically
when their ID reappears. Missing notes await a recoverable, matching Git version.
No source bundles are removed. Git history is local history, not an off-device backup.</p>
<p><a href="/admin/note-history?state=deleted">Deleted</a> ·
<a href="/admin/note-history?state=missing">Missing</a> ·
<a href="/admin/settings?section=git">Git settings</a></p>
<h2>{state.capitalize()}</h2><table><thead><tr><th>Note</th><th>Reason</th><th>Action</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table>{next_link}""")

    @routes.get("/admin/note-history/{note_id}", include_in_schema=False)
    async def preview(note_id: str, request: Request) -> Response:
        try:
            result = await client(request).preview_note_restore(note_id)
        except StoreError as error:
            return failure(error)
        return page(f'''<h1>Restore deleted note</h1>
<p>Destination: {escape(result.path)}</p>
<p>Removal commit: <code>{escape(result.removal_commit)}</code><br>
Recovery copy retained: {escape(local_time(result.recorded_at, _zone(request)))}</p>
<p class="notice">Restoring makes this note available to Obsidian Sync again.
The original ID and exact saved contents are preserved.
An occupied path will not be overwritten.</p>
<pre>{escape(result.content)}</pre>
<form method="post" action="/v1/admin/note-history/{quote(note_id, safe="")}/restore">
<input type="hidden" name="token" value="{escape(result.token)}">
<label>Type Restore {escape(note_id)} to confirm
<input name="confirmation" autocomplete="off" required></label>
<button>Restore note</button></form>''')

    @routes.post("/v1/admin/note-history/{note_id}/restore", include_in_schema=False)
    async def restore(note_id: str, request: Request) -> Response:
        form = parse_qs((await request.body()).decode())
        try:
            payload = RestoreNote(
                token=form.get("token", [""])[0], confirmation=form.get("confirmation", [""])[0]
            )
            await client(request).restore_note(note_id, payload)
        except ValidationError:
            return page("<h1>Note recovery</h1><p>Invalid restore confirmation.</p>", 422)
        except StoreError as error:
            return failure(error)
        return RedirectResponse("/admin/note-history?restored=1", status_code=303)

    return routes
