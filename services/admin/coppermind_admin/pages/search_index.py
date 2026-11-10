"""The search index page: its state and the button that rebuilds it.

The index is derived from the notes filesystem, so rebuilding it is always
safe: the store reads the files again and replaces the entries, in the
background, while search keeps answering from the old entries until the new
ones are committed. Times are shown in the operator's timezone.
"""

from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Any, Protocol, cast
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from coppermind.settings import Wiring, read_settings
from coppermind.store_protocol import (
    EmbeddingStatus,
    RebuildSearchIndexResult,
    SearchIndexStatus,
    StoreError,
)
from coppermind_admin.pages.problems import UNREACHABLE, store_client

DEFAULT_TIMEZONE = "America/Chicago"

NOTICES = {
    "started": "Rebuild started. The counts below change when it finishes.",
    "running": "A rebuild is already running.",
    "connection-saved": "Connection saved. Choose it below to use it for search by meaning.",
}


class SearchIndexControl(Protocol):
    """The part of the store contract this page uses."""

    async def get_search_index_status(self) -> SearchIndexStatus: ...

    async def rebuild_search_index(self) -> RebuildSearchIndexResult: ...

    async def context_action(self, action: str, payload: dict[str, Any]) -> dict[str, Any]: ...


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
<li>Artifacts indexed: {status.artifacts}</li>
<li>Known unreadable entries: {status.known_unreadable}</li>
<li>Last updated: {escape(local_time(status.last_updated_at, zone))}</li>
<li>Rebuild: {rebuild}</li>
</ul>{failure}"""


def embedding_status_html(status: EmbeddingStatus, zone: ZoneInfo) -> str:
    """Where embedding stands, in words: what is happening and what search does meanwhile."""
    model = escape(status.model)
    if status.state == "off":
        return "<p>Search by meaning is off. Searches use keywords only.</p>"
    if status.state == "unavailable":
        return f'<p class="error">{escape(status.error)}</p>'
    lines = ""
    if status.state == "idle":
        lines = (
            f"<p>All {status.embedded} entries are ready for meaning search "
            f"({status.passages} passages, model {model}).</p>"
        )
    else:
        percent = int(100 * status.embedded / status.documents) if status.documents else 0
        lines = (
            f'<p role="status">Embedding your notes with {model}: {status.embedded} of '
            f'{status.documents} done ({percent}%). <progress max="{status.documents}" '
            f'value="{status.embedded}">{percent}%</progress></p>'
            '<p class="muted">Searches use keywords, plus meaning for what is done. This page '
            'does not refresh by itself: <a href="/admin/search-index">Refresh progress</a>.</p>'
        )
    if status.state == "waiting":
        retry = f" Next try {escape(local_time(status.retry_at, zone))}." if status.retry_at else ""
        lines += (
            '<p class="error">The embeddings model is not answering: '
            f"{escape(status.error)}{retry} Searches use keywords only until it does.</p>"
        )
    if status.skipped:
        lines += (
            f'<p class="muted">{status.skipped} entries were refused by the model and are left '
            "out of meaning search until their text changes.</p>"
        )
    return lines


# The store's generic message for a save made against an out-of-date configuration. Here the
# draft is kept and the form already carries the current revision, so "reload" would only
# throw the draft away; this page says what actually happened and what to do.
STALE_REVISION = "configuration changed; reload before saving"
STALE_MESSAGE = (
    "Settings changed in another session. Nothing was saved. Your entries are still here; "
    "review them and save again."
)
CONNECTIONS_LINK = "/admin/context?tab=models&return=/admin/search-index"


def _wait_ok(value: object) -> bool:
    try:
        return 5 <= int(str(value)) <= 600
    except ValueError:
        return False


def saved_notice(enabled: bool, status: EmbeddingStatus) -> str:
    """What saving did, in words, from the state the page now shows."""
    if not enabled:
        return "Search by meaning is off. Searches use keywords only."
    return (
        f"Saved. Indexing by meaning has started; {status.embedded} of {status.documents} "
        "entries are ready."
    )


def embeddings_html(
    view: dict[str, Any],
    status: EmbeddingStatus,
    zone: ZoneInfo,
    attempted: dict[str, Any] | None = None,
) -> str:
    """The meaning-search section: progress first, then the settings.

    `attempted` is what was just submitted and refused; the form keeps it, so a failed test
    never silently puts the old choice back.
    """
    settings = {**view["embeddings"], **(attempted or {})}
    connections = view["connections"]
    models = view["discovery"].get(settings["connection"], [])
    choices = '<option value="">Choose a connection</option>' + "".join(
        f'<option value="{escape(item["id"], quote=True)}"'
        f"{' selected' if item['id'] == settings['connection'] else ''}>"
        f"{escape(item['name'])}</option>"
        for item in connections
    )
    datalist = "".join(f'<option value="{escape(name, quote=True)}">' for name in models)
    unsaved = ' data-unsaved="true"' if attempted is not None else ""
    wait_invalid = "" if _wait_ok(settings["timeout_s"]) else ' aria-invalid="true"'
    checked = lambda flag: " checked" if flag else ""  # noqa: E731
    where_from = (
        "<p>There is no OpenAI or OpenAI-compatible connection yet. A connection is a service "
        "address plus an API key if it needs one, and is where models come from. Add one, then "
        "come back here to choose it.</p>"
        if not connections
        else ""
    )
    return f"""<h2>Search by meaning</h2>
<p>Finds notes and transcripts by what they are about, not only the words they use, such as
&ldquo;that call where we talked about the licensing change&rdquo;. It works alongside the
keyword search and falls back to it whenever the model is off or not answering.</p>
{embedding_status_html(status, zone)}
<form method="post" action="/v1/admin/search-index/embeddings" id="meaning-form"{unsaved}>
<input type="hidden" name="revision" value="{int(view["revision"])}">
<fieldset><legend>Embeddings model</legend>
{where_from}
<label>Connection<select name="connection" class="lat-select">{choices}</select></label>
<p class="muted"><a href="{CONNECTIONS_LINK}" id="connections-link">Add or edit connection</a>
(you come back to this page after saving a connection; unsaved changes to the settings
below are lost when you leave, so save them first)</p>
<label>Model<input class="lat-input" name="model" list="embedding-models"
value="{escape(settings["model"], quote=True)}"
placeholder="for example text-embedding-3-small"></label>
<datalist id="embedding-models">{datalist}</datalist>
<p class="muted">The model list comes from the connection; refresh it on the Connections page.</p>
<label>Wait limit (seconds)<input class="lat-input" name="timeout_s" type="number" min="5" max="600"
value="{escape(str(settings["timeout_s"]), quote=True)}"{wait_invalid}></label>
<p class="muted">How long the service may stay silent before one request is given up on,
including the test when you save.</p>
</fieldset>
<fieldset><legend>When to run</legend>
<label><input type="checkbox" name="enabled"{checked(settings["enabled"])}>
Search by meaning</label>
<p class="muted">When this is on, the text of your notes, source pages, stored artifacts and the
people and company pages (which hold personal details) is
sent to the connection chosen above so it can be embedded, a few at a time, in the background,
newest first. It picks up where it left off after a restart. Saving asks the model for one test
embedding before turning this on. Changing the connection or the model indexes existing content
again; keyword search remains available while it does.</p>
</fieldset>
<p id="meaning-testing" class="notice" role="status" hidden></p>
<button class="lat-btn lat-btn--primary">Save search by meaning</button>
</form>
<script>
const meaningForm = document.getElementById("meaning-form");
// A form shown after a refused save already holds unsaved values, so the warning starts armed.
let meaningDirty = meaningForm.dataset.unsaved === "true";
meaningForm.addEventListener("input", () => {{ meaningDirty = true; }});
document.getElementById("connections-link").addEventListener("click", (event) => {{
  if (meaningDirty && !confirm(
    "Your changes to the search by meaning settings are not saved and will be lost if you " +
    "leave. Leave anyway?")) event.preventDefault();
}});
meaningForm.addEventListener("submit", () => {{ meaningDirty = false; }});
document.getElementById("meaning-form").addEventListener("submit", (event) => {{
  const form = event.target;
  if (!form.elements.enabled.checked) return;
  const note = document.getElementById("meaning-testing");
  note.textContent = "Testing connection... this may take up to " +
    form.elements.timeout_s.value + " seconds";
  note.hidden = false;
}});
</script>"""


def router(wiring: Wiring) -> APIRouter:
    routes = APIRouter()

    def page(body: str, status_code: int = 200) -> HTMLResponse:
        from coppermind_admin.main import page as render

        return HTMLResponse(render("Search index", body), status_code=status_code)

    async def meaning_section(
        request: Request,
        status: SearchIndexStatus,
        problem: str = "",
        attempted: dict[str, Any] | None = None,
    ) -> str:
        try:
            view = await _client(request, wiring).context_action("embeddings", {})
        except (StoreError, OSError):
            return f'<h2>Search by meaning</h2><p class="error">{UNREACHABLE}</p>'
        failure = f'<p class="error">{escape(problem)}</p>' if problem else ""
        return failure + embeddings_html(view, status.embeddings, _zone(request), attempted)

    @routes.get("/admin/search-index", response_class=HTMLResponse, include_in_schema=False)
    async def search_index_page(request: Request) -> Response:
        heading = '<h1>Search index</h1><p><a href="/admin">Home</a></p>'
        try:
            status = await _client(request, wiring).get_search_index_status()
        except (StoreError, OSError):
            return page(f'{heading}<p class="error">{UNREACHABLE}</p>', 503)
        notice_key = request.query_params.get("notice", "")
        message = NOTICES.get(notice_key, "")
        if notice_key in {"embeddings-on", "embeddings-off"}:
            message = saved_notice(notice_key == "embeddings-on", status.embeddings)
        notice = f'<p class="notice">{escape(message)}</p>' if message else ""
        meaning = await meaning_section(request, status)
        return page(
            f"""{heading}{notice}
<p>Full-text search over note bodies and titles, over the generated source
pages, and over the text extracted from artifacts. The index is rebuilt from the
notes filesystem and the artifact records, never the other way round, so
rebuilding it loses nothing.</p>
{status_html(status, _zone(request))}
<form method="post" action="/v1/admin/search-index/rebuild">
<button class="lat-btn">Rebuild index</button></form>
{meaning}"""
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

    @routes.post("/v1/admin/search-index/embeddings", include_in_schema=False)
    async def save_embeddings(request: Request) -> Response:
        from coppermind_admin.main import submitted

        form = await submitted(request)
        try:
            revision = int(form.get("revision", "0"))
        except ValueError:
            revision = -1
        try:
            wait = int(form.get("timeout_s", "60"))
        except ValueError:
            wait = -1
        assignment = {
            "connection": form.get("connection", ""),
            "model": form.get("model", ""),
            "enabled": "enabled" in form,
            "timeout_s": wait,
        }
        client = _client(request, wiring)
        try:
            if not 5 <= wait <= 600:
                raise ValueError(
                    "The wait limit must be between 5 and 600 seconds. Nothing was saved."
                )
            await client.context_action(
                "save-embeddings", {"revision": revision, "assignment": assignment}
            )
        except (StoreError, OSError, ValueError) as exc:
            heading = '<h1>Search index</h1><p><a href="/admin">Home</a></p>'
            try:
                status = await client.get_search_index_status()
            except (StoreError, OSError):
                return page(f'{heading}<p class="error">{UNREACHABLE}</p>', 503)
            problem = (
                str(exc) if isinstance(exc, StoreError | ValueError) else UNREACHABLE
            ) or "That could not be saved."
            if problem == STALE_REVISION:
                problem = STALE_MESSAGE
            # What was typed stays as typed, even a wait limit that was refused.
            shown = {**assignment, "timeout_s": form.get("timeout_s", "60")}
            return page(heading + await meaning_section(request, status, problem, shown), 422)
        notice = "embeddings-on" if assignment["enabled"] else "embeddings-off"
        return RedirectResponse(f"/admin/search-index?notice={notice}", status_code=303)

    return routes
