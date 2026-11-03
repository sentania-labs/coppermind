"""Preview and explicitly confirm retention cleanup using the existing Admin shell."""

from html import escape
from typing import Protocol, cast
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import ValidationError

from coppermind.errors import to_http
from coppermind.purge import ConfirmPurge, PurgeResult
from coppermind.settings import ProductSettings, Wiring, read_settings
from coppermind.statefiles import RevisionConflict
from coppermind.store_protocol import StoreError
from coppermind_admin.pages.problems import store_client
from coppermind_admin.pages.search_index import _zone, local_time


class PurgeControl(Protocol):
    async def preview_purge(self) -> PurgeResult: ...
    async def confirm_purge(self, request: ConfirmPurge) -> PurgeResult: ...


# This extension deliberately uses the existing Admin shell/components. There
# is no lattice.css in this application; a global theme migration is separate.
DARK = """<style>
@media(prefers-color-scheme:dark) {
:root {color-scheme:dark;background:#171c19;color:#edf0eb}
.sidebar {background:#202820;border-color:#465346}
.brand,a,.breadcrumb {color:#b1d7b9}
.sidebar button {color:#b1d7b9;border-color:#8b9f8b}
main {background:#202820;border-color:#465346}
input {background:#171c19;color:#edf0eb;border-color:#8b9f8b}
.metric {background:#293329;border-color:#465346}
th,td {border-color:#465346}th,.muted,.field-help {color:#c2cdbf}
.notice {background:#493c22;color:#fff1d6}
.error {background:#4a2924;color:#fde8e4}
.success {background:#293e2e;color:#e9f0e9}
}
</style>"""

PROGRESS = """<script>
for (const form of document.querySelectorAll('main form')) {
  form.addEventListener('submit', () => {
    for (const button of form.querySelectorAll('button')) button.disabled = true;
    document.getElementById('progress').textContent =
      form.action.endsWith('/confirm') ? 'Removing the confirmed items. Keep this page open.' :
      form.action.endsWith('/preview') ?
      'Checking deleted notes and source references. Nothing is being removed.' :
      'Saving retention. No items are being removed.';
  });
}
</script>"""


def listing(value: PurgeResult, request: Request) -> str:
    groups = ""
    for label, values in [
        ("Origin", value.counts.by_origin),
        ("Provider", value.counts.by_provider),
    ]:
        rows = "".join(
            f"<tr><td>{escape(k)}</td><td>{n}</td></tr>" for k, n in sorted(values.items())
        )
        groups += (
            f"<section><h2>By {label.lower()}</h2><table><thead>"
            f"<tr><th>{label}</th><th>Items</th></tr></thead>"
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
        label = "Deleted note" if c.kind == "note" else "Orphaned source"
        rows += f"""<tr><td>{label}<br><strong>{name}</strong><br>{escape(c.path)}</td>
<td>{escape(", ".join(c.origins))}<br>{escape(", ".join(c.providers))}</td><td>{when}</td></tr>"""
    return f"""<div class="metrics"><div class="metric">Deleted notes
<strong>{value.counts.notes}</strong></div>
<div class="metric">Orphaned sources<strong>{value.counts.sources}</strong></div></div>
<div class="review-grid">{groups}</div>
<p class="muted">An item associated with several origins or providers counts once in each group.</p>
<h2>Items in this list</h2><table><thead><tr><th>Item</th><th>Origin / provider</th>
<th>Deletion recorded</th></tr></thead><tbody>{rows}</tbody></table>"""


def router(wiring: Wiring) -> APIRouter:
    routes = APIRouter()

    def client(request: Request) -> PurgeControl:
        return cast(PurgeControl, store_client(request, wiring))

    def page(body: str, status: int = 200) -> HTMLResponse:
        from coppermind_admin.main import page as render

        return HTMLResponse(
            render(
                "Retention purge",
                DARK
                + "<h1>Retention purge</h1>"
                + body
                + '<p id="progress" role="status" aria-live="polite"></p>'
                + PROGRESS,
            ),
            status_code=status,
        )

    def failure(error: Exception, status: int = 503) -> HTMLResponse:
        if isinstance(error, StoreError):
            status, body = to_http(error)
            message = body["message"]
        else:
            message = "The setting could not be saved. Reload this page and try again."
        return page(
            f'<p class="error" role="alert">{escape(message)}</p>'
            "<p>Review the saved list again before retrying. "
            "An interrupted purge may have removed some files already.</p>"
            '<a href="/admin/purge">Back to retention purge</a>',
            status,
        )

    def home(
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
            f'''{notice}<p>Remove deleted-note recovery copies and unused source material.
First preview the list. Nothing is removed until you confirm it.</p>
<p>Git history and its removal commits stay intact.
Live notes and sources they reference are protected.
Orphaned sources are eligible as soon as their opening note is deleted or purged
and no live note cites them.
Notes still present in Trash are protected.</p>
<form method="post" action="/v1/admin/purge/settings">
<input type="hidden" name="revision" value="{revision}">
<label for="days">Keep deleted notes for (days)</label>
<input id="days" name="days" type="number" min="0" max="36500" required value="{days}">
<p class="field-help">Default: 30 days from the recorded deletion. Zero permits immediate removal.
Saving takes effect on the next preview; it does not start a purge.</p>
<button>Save retention</button></form>
<form method="post" action="/v1/admin/purge/preview">
<button>Preview what will be removed</button></form>''',
            status,
        )

    def reviewed(value: PurgeResult, request: Request) -> HTMLResponse:
        if not value.candidates:
            return page(
                '<p class="success">Nothing is eligible for removal.</p>'
                "<p>Deleted notes within the retention window "
                "and sources with live references are kept.</p>"
                '<a href="/admin/purge">Back to retention settings</a>'
            )
        complete = value.state == "complete"
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
        if value.state in {"failed", "applying"}:
            heading = (
                '<p class="error">An earlier purge did not finish. '
                "Some files may already be gone. Review its saved list below, "
                "then retry to finish removing only these items.</p>"
            )
        body = (
            heading
            + f"<p>Deleted-note retention: {value.retention_days} days.</p>"
            + listing(value, request)
        )
        if not complete:
            notes_label = f"{value.counts.notes} deleted note" + (
                "s" if value.counts.notes != 1 else ""
            )
            sources_label = f"{value.counts.sources} orphaned source" + (
                "s" if value.counts.sources != 1 else ""
            )
            action = (
                "Retry removal of listed items"
                if value.state in {"failed", "applying"}
                else "Permanently remove listed items"
            )
            body += f'''<div class="notice"><h2>Confirm permanent removal</h2>
<p>This removes {notes_label} and their recovery copies,
and {sources_label} with their original bundles and generated pages.
Their search entries are removed too.
Admin cannot undo this. Git history remains,
but it does not contain the original source bundles.</p></div>
<form method="post" action="/v1/admin/purge/confirm">
<input type="hidden" name="token" value="{escape(value.token)}">
<label for="confirmation">Type Purge listed items to confirm</label>
<input id="confirmation" name="confirmation" required autocomplete="off"
pattern="Purge listed items">
<button class="danger">{action}</button>
</form>'''
        body += (
            '<p><a href="/admin/purge">'
            + ("Back to retention settings" if complete else "Cancel and keep remaining items")
            + "</a></p>"
        )
        return page(body)

    @routes.get("/admin/purge", include_in_schema=False)
    async def retention(request: Request) -> Response:
        try:
            return home(request)
        except (OSError, ValueError) as error:
            return failure(error)

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
            return home(
                request,
                '<p class="success" role="status">Retention saved. '
                "Preview the list to continue.</p>",
            )
        except RevisionConflict:
            return home(
                request,
                '<p class="error">Retention changed in another session. '
                '<a href="/admin/purge">Reload current settings</a> before trying again.</p>',
                409,
                form,
            )
        except ValueError:
            return home(
                request,
                '<p class="error">Enter a whole number of days from 0 to 36500. '
                "Your choice has not been saved.</p>",
                422,
                form,
            )
        except OSError as error:
            return failure(error)

    @routes.post("/v1/admin/purge/preview", include_in_schema=False)
    async def preview(request: Request) -> Response:
        try:
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
            return failure(error)

    return routes
