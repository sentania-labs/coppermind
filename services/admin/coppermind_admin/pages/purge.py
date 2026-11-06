"""Preview and explicitly confirm retention cleanup using the existing Admin shell."""

from html import escape
from typing import Protocol, cast
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import ValidationError

from coppermind.errors import to_http
from coppermind.purge import AbandonPurge, ConfirmPurge, PurgeResult
from coppermind.settings import ProductSettings, Wiring, read_settings
from coppermind.statefiles import RevisionConflict
from coppermind.store_protocol import (
    NotesFilesystemUnavailable,
    ProblemInfo,
    PurgeConflict,
    SourcesFilesystemUnavailable,
    StoreError,
    ValidationFailed,
)
from coppermind_admin.pages.problems import store_client
from coppermind_admin.pages.search_index import _zone, local_time


class PurgeControl(Protocol):
    async def preview_purge(self) -> PurgeResult: ...
    async def confirm_purge(self, request: ConfirmPurge) -> PurgeResult: ...
    async def abandon_purge(self, request: AbandonPurge) -> PurgeResult: ...
    async def get_problems(self) -> list[ProblemInfo]: ...


PROGRESS = """<script>
for (const form of document.querySelectorAll('main form')) {
  form.addEventListener('submit', (event) => {
    const target = event.submitter ? event.submitter.formAction : form.action;
    document.getElementById('progress').textContent =
      target.endsWith('/confirm') ? 'Removing the confirmed items. Keep this page open.' :
      target.endsWith('/abandon') ? 'Abandoning the saved list. Nothing is being removed.' :
      target.endsWith('/preview') ?
      'Checking deleted notes and source references. Nothing is being removed.' :
      'Saving retention. No items are being removed.';
    setTimeout(() => {
      for (const button of form.querySelectorAll('button')) button.disabled = true;
    }, 0);
  });
}
</script>"""


def plural(count: int, singular: str, many: str | None = None) -> str:
    return f"{count} {singular if count == 1 else many or singular + 's'}"


def listing(value: PurgeResult, request: Request) -> str:
    groups = ""
    for label, values in [
        ("origin", value.counts.by_origin),
        ("provider", value.counts.by_provider),
    ]:
        rows = "".join(
            f"<tr><td>{escape(k)}</td><td>{n.notes}</td><td>{n.sources}</td>"
            f"<td>{n.artifacts}</td></tr>"
            for k, n in sorted(values.items())
        )
        groups += (
            f"<section><h2>By {label}</h2><table><thead><tr><th>{label.capitalize()}</th>"
            f"<th>Deleted notes</th><th>Orphaned sources</th>"
            f"<th>Removed artifacts</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></section>"
        )
    rows = ""
    for c in value.candidates:
        name = escape(c.title)
        when = (
            escape(local_time(c.deleted_at, _zone(request)))
            if c.deleted_at
            else "Opening note removed"
        )
        label = {
            "note": "Deleted note",
            "source": "Orphaned source",
            "artifact": "Removed artifact",
        }[c.kind]
        if c.leftover:
            label += " (leftover from an interrupted purge)"
        where = c.origins if c.origins == c.providers else [*c.origins, *c.providers]
        if c.reason:
            where = [*where, c.reason]
        rows += f"""<tr><td>{label}<br><strong>{name}</strong><br>{escape(c.path)}</td>
<td>{"<br>".join(escape(v) for v in where)}</td><td>{when}</td></tr>"""
    return f"""<div class="metrics"><div class="metric">Deleted notes
<strong>{value.counts.notes}</strong></div>
<div class="metric">Orphaned sources<strong>{value.counts.sources}</strong></div>
<div class="metric">Removed artifacts<strong>{value.counts.artifacts}</strong></div></div>
<div class="review-grid">{groups}</div>
<p class="muted">An item associated with several origins or providers counts once in each group.</p>
<h2>Items in this list</h2><table><thead><tr><th>Item</th><th>Origin / provider</th>
<th>Deletion recorded</th></tr></thead><tbody>{rows}</tbody></table>"""


def kept_line(value: PurgeResult) -> str:
    kept = value.kept
    return (
        '<p class="muted">Kept: '
        f"{plural(kept.notes_within_retention, 'deleted note')} still inside the "
        f"{value.retention_days}-day retention window and "
        f"{plural(kept.sources_in_use, 'source')} still used by live notes, and "
        f"{plural(kept.artifacts_within_retention, 'removed artifact')} still inside the window. "
        "Git history is never rewritten.</p>"
    )


def router(wiring: Wiring) -> APIRouter:
    routes = APIRouter()

    def client(request: Request) -> PurgeControl:
        return cast(PurgeControl, store_client(request, wiring))

    def page(body: str, status: int = 200) -> HTMLResponse:
        from coppermind_admin.main import page as render

        return HTMLResponse(
            render(
                "Retention purge",
                "<h1>Retention purge</h1>"
                + body
                + '<p id="progress" role="status" aria-live="polite"></p>'
                + PROGRESS,
            ),
            status_code=status,
        )

    def failure(
        error: Exception, status: int = 503, message: str = "", interrupted: bool = False
    ) -> HTMLResponse:
        if message:
            return page(
                f'<p class="error" role="alert">{escape(message)}</p>'
                '<a href="/admin/purge">Reload retention purge</a>',
                status,
            )
        if isinstance(error, SourcesFilesystemUnavailable):
            message = "The sources volume could not be read or written."
        elif isinstance(error, NotesFilesystemUnavailable):
            message = "The notes or state volume could not be read or written."
        elif isinstance(error, StoreError):
            status, body = to_http(error)
            message = body["message"][:1].upper() + body["message"][1:]
        else:
            message = "Retention purge could not read or remove files. Try again."
        if not interrupted:
            return failure(error, status, message)
        return page(
            f'<p class="error" role="alert">{escape(message)}</p>'
            "<p>An interrupted purge may have removed some items already. "
            "Open Retention purge to review its saved list, then retry it or abandon it.</p>"
            '<a href="/admin/purge">Open retention purge</a>',
            status,
        )

    async def attention(request: Request) -> str:
        try:
            problems = await client(request).get_problems()
        except (StoreError, OSError):
            return ""
        mine = [p for p in problems if p.kind in {"purge", "purge_unfinished", "purge_receipt"}]
        if not mine:
            return ""
        reasons = "".join(f"<li>{escape(p.reason)}</li>" for p in mine)
        review = ""
        if any(p.kind == "purge_unfinished" for p in mine):
            review = (
                '<form method="post" action="/v1/admin/purge/preview">'
                "<button>Review the unfinished purge</button></form>"
            )
        return (
            '<div class="error" role="alert"><h2>A purge needs attention</h2>'
            f'<ul>{reasons}</ul>{review}<p><a href="/admin/problems">See all problems</a></p></div>'
        )

    async def home(
        request: Request, notice: str = "", status: int = 200, submitted: dict | None = None
    ) -> HTMLResponse:
        state = request.app.state.control.read("settings")
        days = str(read_settings(request.app.state.control).retention.days)
        revision = str(state.revision)
        if submitted:
            days = submitted.get("days", [days])[0]
            revision = submitted.get("revision", [revision])[0]
        days, revision = escape(days, quote=True), escape(revision, quote=True)
        return page(
            f'''{notice}{await attention(request)}
<p>Remove deleted-note recovery copies, the stored files of removed artifacts, and unused
source material. First preview the list. Nothing is removed until you confirm it.</p>
<p>Git history and its removal commits stay intact.
Live notes and sources they reference are protected.
Orphaned sources are eligible as soon as their opening note is deleted or purged
and no live note cites them.
Notes still present in Trash are protected.</p>
<form method="post" action="/v1/admin/purge/preview">
<input type="hidden" name="revision" value="{revision}">
<label for="days">Keep deleted notes and removed artifacts for (days)</label>
<input id="days" name="days" type="number" min="0" max="36500" required value="{days}">
<p class="field-help">Default: 30 days from the recorded deletion or removal. Zero permits immediate
removal. The same window covers a deleted note and an artifact removed here, through the API or by
deleting its file in Obsidian.
Save a changed value before previewing. Saving does not start a purge.</p>
<button>Preview what will be removed</button>
<button class="secondary" formaction="/v1/admin/purge/settings">Save retention</button>
</form>''',
            status,
        )

    def abandoned(value: PurgeResult) -> HTMLResponse:
        if not value.candidates:
            return page(
                '<p class="notice" role="status">The unreadable receipt was moved to quarantine. '
                "Nothing was removed.</p>"
                '<p><a href="/admin/purge">Back to retention settings</a></p>'
            )
        kept = "".join(
            f"<li>{escape(c.title)}<br>{escape(c.path)}</li>"
            for c in value.candidates
            if c.id in value.remaining
        )
        return page(
            f'<p class="notice" role="status">Purge abandoned. {value.completed} listed items '
            f"were removed and {len(value.remaining)} were not. Nothing was retried. "
            "Items that were not removed are listed in Problems; preview again to finish "
            "removing them.</p>"
            + (f"<h2>Not removed</h2><ul>{kept}</ul>" if kept else "")
            + '<p><a href="/admin/purge">Back to retention settings</a></p>'
        )

    def reviewed(value: PurgeResult, request: Request) -> HTMLResponse:
        if value.state == "abandoned":
            return abandoned(value)
        if not value.candidates:
            return page(
                '<p class="success">Nothing is eligible for removal.</p>'
                "<p>Deleted notes within the retention window "
                "and sources with live references are kept.</p>"
                + kept_line(value)
                + '<a href="/admin/purge">Back to retention settings</a>'
            )
        complete = value.state == "complete"
        unfinished = value.state in {"failed", "applying"}
        heading = (
            (
                '<p class="success" role="status">Purge complete. The listed items were removed. '
                "Git history is unchanged.</p>"
            )
            if complete
            else (
                "<h2>Review before removing</h2><p>This is the saved list. "
                "Nothing has been removed by this preview.</p>"
            )
        )
        if unfinished:
            heading = (
                '<p class="error">An earlier purge did not finish. '
                "Some items may already be removed and some files may remain. "
                "Review its saved list below, then retry to finish removing only these items, "
                "or abandon it.</p>"
            )
        body = (
            heading
            + f"<p>Deleted-note retention: {value.retention_days} days.</p>"
            + listing(value, request)
        )
        if not complete:
            body += kept_line(value)
            parts = []
            if value.counts.notes:
                parts.append(
                    f"{plural(value.counts.notes, 'deleted note')} and their recovery copies"
                )
            if value.counts.sources:
                parts.append(
                    f"{plural(value.counts.sources, 'orphaned source')} with "
                    + (
                        "its original bundle and generated pages"
                        if value.counts.sources == 1
                        else "their original bundles and generated pages"
                    )
                )
            if value.counts.artifacts:
                parts.append(
                    f"{plural(value.counts.artifacts, 'removed artifact')} "
                    "with their archived bytes"
                )
            action = (
                "Retry removal of listed items" if unfinished else "Permanently remove listed items"
            )
            body += f'''<div class="notice"><h2>Confirm permanent removal</h2>
<p>This removes {" and ".join(parts)}.
Their search entries are removed too.
Admin cannot undo this. Git history remains,
but it does not contain the original source bundles.</p></div>
<form method="post" action="/v1/admin/purge/confirm">
<input type="hidden" name="token" value="{escape(value.token)}">
<label for="confirmation">Type <code>Purge listed items</code> to confirm</label>
<input id="confirmation" name="confirmation" required autocomplete="off"
pattern="Purge listed items">
<button class="danger">{action}</button>
</form>'''
            if unfinished:
                body += f'''<h2>Or abandon this purge</h2>
<p>Abandoning stops this saved list without retrying it. Nothing more is removed.
What was and was not removed is reported in Problems, and you can start a new preview.</p>
<form method="post" action="/v1/admin/purge/abandon">
<input type="hidden" name="token" value="{escape(value.token)}">
<button class="secondary">Abandon this purge</button></form>'''
        body += (
            '<p><a href="/admin/purge">'
            + (
                "Back to retention settings (nothing removed)"
                if value.state == "preview"
                else "Back to retention settings"
            )
            + "</a></p>"
        )
        return page(body)

    @routes.get("/admin/purge", include_in_schema=False)
    async def retention(request: Request) -> Response:
        try:
            return await home(request)
        except (OSError, ValueError) as error:
            return failure(
                error, 500, "Retention settings could not be read. Reload this page and try again."
            )

    @routes.post("/v1/admin/purge/settings", include_in_schema=False)
    async def settings(request: Request) -> Response:
        form = parse_qs((await request.body()).decode())
        try:
            state = request.app.state.control.read("settings")
            values = dict(state.body)
            values.pop("revision", None)
            values["retention"] = {"days": int(form.get("days", [""])[0])}
            product = ProductSettings.model_validate(values)
            request.app.state.control.write(
                "settings",
                product.model_dump(mode="json"),
                if_revision=int(form.get("revision", [""])[0]),
            )
            return await home(
                request,
                '<p class="success" role="status">Retention saved. '
                "Preview the list to continue.</p>",
            )
        except RevisionConflict:
            return await home(
                request,
                '<p class="error">Retention changed in another session. '
                '<a href="/admin/purge">Reload current settings</a> before trying again.</p>',
                409,
                form,
            )
        except ValueError:
            return await home(
                request,
                '<p class="error">Enter a whole number of days from 0 to 36500. '
                "Your choice has not been saved.</p>",
                422,
                form,
            )
        except OSError as error:
            return failure(
                error, 500, "Retention could not be saved. Reload this page and try again."
            )

    @routes.post("/v1/admin/purge/preview", include_in_schema=False)
    async def preview(request: Request) -> Response:
        form = parse_qs((await request.body()).decode())
        try:
            submitted = form.get("days", [None])[0]
            if submitted is not None and submitted.strip() != str(
                read_settings(request.app.state.control).retention.days
            ):
                return await home(
                    request,
                    '<p class="error">You changed the number of days but have not saved it. '
                    "Choose Save retention first, then preview.</p>",
                    422,
                    form,
                )
            return reviewed(await client(request).preview_purge(), request)
        except StoreError as error:
            return failure(error)

    @routes.post("/v1/admin/purge/confirm", include_in_schema=False)
    async def confirm(request: Request) -> Response:
        form = parse_qs((await request.body()).decode())
        try:
            payload = ConfirmPurge.model_validate(
                {
                    "token": form.get("token", [""])[0],
                    "confirmation": form.get("confirmation", [""])[0],
                }
            )
            return reviewed(await client(request).confirm_purge(payload), request)
        except ValidationError:
            return page(
                '<p class="error">Confirmation did not match. '
                "Preview the list and type Purge listed items exactly.</p>"
                '<a href="/admin/purge">Back</a>',
                422,
            )
        except StoreError as error:
            return failure(
                error, interrupted=not isinstance(error, PurgeConflict | ValidationFailed)
            )

    @routes.post("/v1/admin/purge/abandon", include_in_schema=False)
    async def abandon(request: Request) -> Response:
        form = parse_qs((await request.body()).decode())
        try:
            payload = AbandonPurge.model_validate({"token": form.get("token", [""])[0]})
            return reviewed(await client(request).abandon_purge(payload), request)
        except ValidationError:
            return page(
                '<p class="error">That saved list is no longer valid. Preview again.</p>'
                '<a href="/admin/purge">Back</a>',
                422,
            )
        except StoreError as error:
            return failure(error)

    return routes
