"""Overview counters, the problems dashboard and the pages its links open.

Every number and every problem is computed by the store from its metadata
mirror and the rejections it recorded. Admin only reads: nothing here asks the
store to change a note, and nothing is ever written into one (operator
decision of 2026-10-01). The one thing these pages let a person do is dismiss
the notice that a hand-moved note or artifact file was returned, which changes
no file.
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
# Where a Dismiss form may send the person back; anything else lands on the overview.
DISMISS_RETURNS = frozenset({"/admin", "/admin/artifacts", "/admin/problems"})


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
    "missing": "Missing note",
    "filing": "Filing error",
    "path_notice": "Note path changed",
    "returned_note": "Hand-moved note returned",
    "folder_kept": "Old month folder kept",
    "unreadable_note": "Unreadable note",
    "artifact": "Artifact",
}


def _link(problem: ProblemInfo) -> str:
    if problem.kind == "returned_note" and problem.note_id:
        action = f"/v1/admin/notices/{quote(problem.note_id, safe='')}/dismiss"
        return (
            f'<form method="post" action="{action}">'
            '<input type="hidden" name="back" value="/admin/problems">'
            "<button>Dismiss notice</button></form>"
        )
    if problem.kind == "purge_receipt":
        return (
            '<form method="post" action="/v1/admin/purge/abandon">'
            f'<input type="hidden" name="token" value="{escape(problem.reference, quote=True)}">'
            "<button>Abandon this receipt</button></form>"
        )
    if problem.kind in {"purge", "purge_unfinished"}:
        return '<a href="/admin/purge">Retention purge</a>'
    if problem.kind == "artifact" or problem.artifact_id:
        return '<a href="/admin/artifacts">Artifacts</a>'
    if problem.kind == "dropbox":
        return '<a href="/admin/workflow?tab=intake">Dropbox receipts</a>'
    if problem.kind in {"calendar_import", "calendar_conflict"}:
        return '<a href="/admin/context?tab=calendar">Calendar import history</a>'
    if problem.kind == "damaged_source":
        return ""
    if problem.kind == "missing" and problem.note_id:
        target = f"/admin/note-history/{quote(problem.note_id, safe='')}"
        return f'<a href="{escape(target)}">Check recovery options</a>'
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


def _reason(item: ProblemInfo) -> str:
    if item.kind == "missing":
        if "vanished between filing paths" in item.reason.lower():
            return (
                "The file disappeared while filing was moving it. "
                + item.reason
                + ". Check recovery options for a saved copy."
            )
        return (
            "The file is no longer at its last known path. Check recovery options "
            "for a saved copy or restore it from a device or backup."
        )
    return item.reason


def _problem_rows(problems: list[ProblemInfo], empty_text: str = "No problems found.") -> str:
    if not problems:
        return "<p>" + escape(empty_text) + "</p>"
    rows = "".join(
        f"<tr><td>{_what(item)}<br>"
        f"<small>{escape(KIND_LABELS.get(item.kind, item.kind))}</small></td>"
        f"<td>{escape(_reason(item))}"
        + (
            f"<br>Filing attempts: {item.filing_attempts}"
            if item.filing_attempts is not None
            else ""
        )
        + f"</td><td>{_link(item)}</td></tr>"
        for item in problems
    )
    return (
        "<table><thead><tr><th>Affected note or source</th><th>What to do</th><th>Action</th></tr>"
        f"</thead><tbody>{rows}</tbody></table>"
    )


def filing_status_html(status: StatusResponse, zone: ZoneInfo | None = None) -> str:
    """One filing summary shared by Overview and Problems."""
    from coppermind_admin.pages.search_index import local_time

    counters = status.counters
    ready = counters.filing_ready_depth
    if counters.filing_stalled:
        drain = "No recent filing progress. Check Problems and store logs."
    elif ready and counters.filing_estimated_drain_s is not None:
        minutes = max(1, (counters.filing_estimated_drain_s + 59) // 60)
        drain = (
            f"about {minutes} {'minute' if minutes == 1 else 'minutes'} for notes without errors"
        )
    elif ready:
        drain = "Estimating after filing makes progress."
    else:
        drain = "No notes waiting without errors."
    health = (
        "Filing needs attention"
        if counters.filing_failures or counters.filing_stalled
        else "Filing in progress"
        if counters.filing_queue_depth
        else "Filing complete"
    )
    progress = (
        escape(local_time(counters.filing_last_progress_at, zone or ZoneInfo("America/Chicago")))
        if counters.filing_last_progress_at
        else "No successful filing recorded yet"
    )
    return f"""<section aria-label="Filing status"><h3>{health}</h3>
<p>Ready to file: {ready}. Notes with filing errors: {counters.filing_failures}.
{drain}</p><p>Last filing progress: {progress}</p>
<p><a href="/admin/problems">Check filing problems and recovery options</a></p></section>"""


def _dismiss_form(notice: ReconcilerNotice, back: str = "/admin") -> str:
    action = f"/v1/admin/notices/{quote(notice.note_id, safe='')}/dismiss"
    return (
        f'<form method="post" action="{action}">'
        f'<input type="hidden" name="back" value="{escape(back, quote=True)}">'
        "<button>Dismiss notice</button></form>"
    )


def _notices_html(notices: Sequence[ReconcilerNotice], zone: ZoneInfo, back: str = "/admin") -> str:
    """What moved, from where, where it went back and why, with a Dismiss that only hides it."""
    from coppermind_admin.pages.search_index import local_time

    if not notices:
        return ""
    items = ""
    for notice in notices:
        where = (
            f"found at <code>{escape(notice.from_path)}</code>, returned to "
            f"<code>{escape(notice.path)}</code>, {escape(local_time(notice.at, zone))}"
            if notice.kind != "folder"
            else f"<code>{escape(notice.from_path)}</code>, {escape(local_time(notice.at, zone))}"
        )
        items += (
            f"<li><strong>{escape(notice.title)}</strong> "
            f'<span class="muted">{where}</span>'
            f"<br>{escape(notice.reason)}"
            f"{_dismiss_form(notice, back) if notice.kind != 'folder' else ''}</li>"
        )
    guidance = ""
    if any(notice.kind == "artifact" for notice in notices):
        guidance = (
            '<p class="muted">Nothing needs doing. An artifact is filed by the month it arrived '
            "and stays there; to take one out of your notes, delete the file in Obsidian or "
            "remove it here. Dismiss notice only hides the notice.</p>"
        )
    return f"{guidance}<ul>{items}</ul>"


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
    root = escape(counters.notes_folder or "Notes")
    if counters.notes_to_refile:
        upgrade = (
            f"<strong>{counters.notes_to_refile} notes still to move.</strong> "
            "The next filing pass, which runs after the next check for file changes, "
            "moves them."
        )
    else:
        upgrade = (
            "<strong>Upgrade complete.</strong> Every reviewed note is filed by year and month."
        )
    return f"""<h2>Store Status</h2>
{filing_status_html(status, zone)}
<h3>Notes filing</h3>
<p>Notes are organized by year and month under <code>{root}/</code>, for example
<code>{root}/2026/2026-04/</code>. {upgrade}</p>
<div class="metrics">
<div class="metric" aria-label="Notes awaiting review: {counters.notes_awaiting_review}">
Notes awaiting review<strong>{counters.notes_awaiting_review}</strong>
<span class="muted">Review in Obsidian.</span></div>
<div class="metric" aria-label="Sources: {counters.sources}">
Preserved sources<strong>{counters.sources}</strong></div>
<div class="metric" aria-label="Artifacts: {counters.artifacts}">
Artifacts<strong>{counters.artifacts}</strong>
<a href="/admin/artifacts">Artifacts</a></div>
<div class="metric">Problems<strong>{problems}</strong>
<a href="/admin/problems">Problems ({problems})</a></div></div>
<ul>
<li>Notes by state: {states or "none"}</li>
<li>Sources: {counters.sources}</li>
<li>Artifacts: {counters.artifacts}</li>
<li>Rejected ingests: {counters.rejected_ingests}</li>
<li>Name collisions: {counters.name_collisions}</li>
<li>Unparseable files: {counters.unparseable_files}</li>
<li>Known unreadable search entries: {counters.known_unreadable}</li>
<li>Notes waiting to file (including errors): {counters.filing_queue_depth}</li>
<li>Notes with filing errors: {counters.filing_failures}</li>
<li>Failed calendar imports: {counters.calendar_import_failures}</li>
<li>Retention purge items needing attention: {counters.purge_problems}</li>
<li>Excluded (skipped by exclusion rules): {counters.excluded_count}</li>
<li>Hand-moved notes returned to their filed folder: {counters.notes_returned}
(listed with their reasons under Problems)</li>
<li>Old month folders left in place because they are not empty: {counters.folders_kept}
(listed with their reasons under Problems)</li>
<li>Artifacts moved in Obsidian and returned to their folder: {counters.artifacts_returned}
{_notices_html([n for n in notices if n.kind == "artifact"], zone)}</li>
</ul>
<p><a href="/admin/problems">Problems ({problems})</a></p>"""


async def overview_counters(request: Request, wiring: Wiring) -> str:
    from coppermind_admin.pages.search_index import _zone

    try:
        client = store_client(request, wiring)
        status = await client.get_status()
        notices = (
            await client.list_reconciler_notices() if status.counters.artifacts_returned else []
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
        from coppermind_admin.main import submitted

        back = str((await submitted(request)).get("back", "/admin"))
        try:
            await store_client(request, wiring).dismiss_reconciler_notice(note_id)
        except (StoreError, OSError):
            return unreachable("Overview")
        return RedirectResponse(back if back in DISMISS_RETURNS else "/admin", status_code=303)

    @routes.get("/admin/problems", response_class=HTMLResponse, include_in_schema=False)
    async def problems_page(request: Request) -> Response:
        try:
            client = store_client(request, wiring)
            problems = await client.get_problems()
            status = await client.get_status()
        except (StoreError, OSError):
            return unreachable("Problems")
        from coppermind_admin.pages.search_index import _zone

        return page(
            "Problems",
            '<h1>Problems</h1><p><a href="/admin">Overview</a></p>'
            + filing_status_html(status, _zone(request))
            + "<p>Start with the affected note and the action below. "
            "Filing retries automatically after the cause is fixed.</p>"
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
