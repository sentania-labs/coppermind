"""Everyday settings: time zone, password, folders, note properties, tags, search, advanced.

Each line on the Settings page shows what is set now and has one button. A
setting is changed on its own small page, which says what happened afterwards.
"""

from __future__ import annotations

import re
from typing import Any, cast
from zoneinfo import ZoneInfo, available_timezones

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import ValidationError

from coppermind.schema import KEY_NAME_PATTERN, FrontmatterSchema, KeyDefinition
from coppermind.settings import Wiring
from coppermind.statefiles import RevisionConflict, StateStore
from coppermind.store_protocol import StoreError
from coppermind_admin import model, ui
from coppermind_admin.auth import AdminRecordUnreadable
from coppermind_admin.pages.common import (
    health_badge,
    service_choices,
    service_path,
    stopped_help,
    where_text,
)
from coppermind_admin.shell import Ctx
from coppermind_admin.ui import e, plural, segment

ZONES = {
    "America/Los_Angeles": "Pacific Time (Los Angeles)",
    "America/Vancouver": "Pacific Time (Vancouver)",
    "America/Denver": "Mountain Time (Denver)",
    "America/Edmonton": "Mountain Time (Edmonton)",
    "America/Phoenix": "Mountain Time, no daylight saving (Phoenix)",
    "America/Chicago": "Central Time (Chicago)",
    "America/Winnipeg": "Central Time (Winnipeg)",
    "America/Mexico_City": "Central Time (Mexico City)",
    "America/New_York": "Eastern Time (New York)",
    "America/Toronto": "Eastern Time (Toronto)",
    "America/Halifax": "Atlantic Time (Halifax)",
    "America/Anchorage": "Alaska Time (Anchorage)",
    "Pacific/Honolulu": "Hawaii Time (Honolulu)",
    "Europe/London": "UK Time (London)",
    "Europe/Dublin": "Irish Time (Dublin)",
    "Europe/Lisbon": "Western European Time (Lisbon)",
    "Europe/Berlin": "Central European Time (Berlin)",
    "Europe/Paris": "Central European Time (Paris)",
    "Europe/Madrid": "Central European Time (Madrid)",
    "Europe/Rome": "Central European Time (Rome)",
    "Europe/Amsterdam": "Central European Time (Amsterdam)",
    "Europe/Stockholm": "Central European Time (Stockholm)",
    "Europe/Warsaw": "Central European Time (Warsaw)",
    "Europe/Helsinki": "Eastern European Time (Helsinki)",
    "Europe/Athens": "Eastern European Time (Athens)",
    "Asia/Dubai": "Gulf Time (Dubai)",
    "Asia/Kolkata": "India Time (Kolkata)",
    "Asia/Singapore": "Singapore Time (Singapore)",
    "Asia/Shanghai": "China Time (Shanghai)",
    "Asia/Tokyo": "Japan Time (Tokyo)",
    "Asia/Seoul": "Korea Time (Seoul)",
    "Australia/Perth": "Australian Western Time (Perth)",
    "Australia/Sydney": "Australian Eastern Time (Sydney)",
    "Australia/Melbourne": "Australian Eastern Time (Melbourne)",
    "Pacific/Auckland": "New Zealand Time (Auckland)",
    "UTC": "UTC, no daylight saving",
}
REGIONS = (
    "Africa",
    "America",
    "Antarctica",
    "Asia",
    "Atlantic",
    "Australia",
    "Europe",
    "Indian",
    "Pacific",
)
KINDS = {
    "string": ("Words", "Any text. Example: a project name."),
    "list": ("Several values", "More than one entry. Example: the people at a meeting, or tags."),
    "date": ("A date", "Example: Nov 10, 2026."),
    "enum": (
        "One from a fixed set",
        "You list the options and a note has exactly one. Example: low, medium or high.",
    ),
    "bool": ("Yes or no", "Example: follow-up needed."),
    "int": ("A number", "Example: an amount."),
}
# Properties Coppermind owns: the name a person reads, and which tick fills each.
BUILT_ROLES = {
    "date_key": ("Date", "date"),
    "type_key": ("Type", "type"),
    "context_key": ("Context", "context"),
    "account_key": ("Account", "account"),
    "tags_key": ("Tags", "tags"),
}
BUILT_OWNED = {
    "attendees": ("Attendees", "people"),
    "invitees": ("Invitees", "people"),
    "calendar_event": ("Meeting", "event"),
}
HIDDEN_ROLES = ("schema_version_key", "id_key", "reviewed_key", "sources_key")
HIDDEN_NAMES = {"time_unknown", "kind", "artifacts"}
BUILT_WHY = {
    "Date": "When the meeting or note happened.",
    "Type": "What kind of note it is. Decides the folder it is filed in.",
    "Context": "Whether it is internal, personal, or with someone outside such as a customer.",
    "Account": "Which customers or companies it was with.",
    "Tags": "Your own labels.",
}
SIGNIN = (("1", "1 hour"), ("12", "12 hours"), ("24", "1 day"), ("168", "1 week"))
TIMING = {
    "scan": (
        "Look for changed notes every",
        (("30", "30 seconds"), ("60", "1 minute"), ("300", "5 minutes"), ("900", "15 minutes")),
        "How often Coppermind checks the vault.",
    ),
    "quiet": (
        "Wait for a new file to settle",
        (("10", "10 seconds"), ("30", "30 seconds"), ("60", "1 minute"), ("300", "5 minutes")),
        "So a file that is still being written is not picked up half done.",
    ),
    "daily": (
        "Full check once a day at",
        (("01:30", "1:30 am"), ("03:30", "3:30 am"), ("05:30", "5:30 am"), ("23:30", "11:30 pm")),
        "A thorough check of every file.",
    ),
    "history": (
        "Record a change to history after",
        (("30", "30 seconds"), ("60", "1 minute"), ("300", "5 minutes")),
        "Of no further edits to that note.",
    ),
}
SEARCH_LEDE = (
    "How well search is working, a box to try it, and search by meaning, which finds notes by "
    "what they are about and not only the exact words."
)


def zone_name(identity: str) -> str:
    """A time zone the way a person says it: "Central Time (Chicago)"."""
    if identity in ZONES:
        return ZONES[identity]
    region, _, city = identity.partition("/")
    if not city:
        return identity
    return f"{city.rsplit('/', 1)[-1].replace('_', ' ')} ({region})"


def all_zones() -> list[tuple[str, str]]:
    listed = dict(ZONES)
    for identity in sorted(available_timezones()):
        if identity.split("/")[0] in REGIONS and identity not in listed:
            listed[identity] = zone_name(identity)
    return list(listed.items())


def duration(seconds: int) -> str:
    if seconds % 3600 == 0:
        return plural(seconds // 3600, "hour")
    if seconds % 60 == 0:
        return plural(seconds // 60, "minute")
    return plural(seconds, "second")


def with_value(
    choices: tuple[tuple[str, str], ...], value: str, label: str
) -> list[tuple[str, str]]:
    listed = list(choices)
    if value not in {item[0] for item in listed}:
        listed.append((value, label))
    return listed


def read_schema(control: StateStore) -> tuple[int, FrontmatterSchema]:
    state = control.read("schema")
    body = {key: value for key, value in state.body.items() if key != "revision"}
    return state.revision, FrontmatterSchema.model_validate(body)


def write_schema(control: StateStore, schema: FrontmatterSchema, revision: int) -> None:
    problems = schema.edit_problems()
    if problems:
        raise ValueError(problems[0])
    control.write("schema", schema.model_dump(mode="json"), if_revision=revision)


def properties(schema: FrontmatterSchema) -> list[dict[str, Any]]:
    """The properties a person sees: the ones Coppermind comes with, then their own."""
    roles = {schema.roles.get(role): role for role in schema.roles}
    hidden = {schema.roles.get(role) for role in HIDDEN_ROLES} | HIDDEN_NAMES
    built, own = [], []
    for key in schema.keys:
        if key.name in hidden:
            continue
        role = roles.get(key.name)
        if role in BUILT_ROLES:
            label, fill = BUILT_ROLES[role]
            built.append(
                {
                    "key": key,
                    "label": label,
                    "fill": fill,
                    "built": True,
                    "tags": role == "tags_key",
                    "why": BUILT_WHY[label],
                }
            )
        elif key.name in BUILT_OWNED:
            label, fill = BUILT_OWNED[key.name]
            built.append(
                {
                    "key": key,
                    "label": label,
                    "fill": fill,
                    "built": True,
                    "tags": False,
                    "why": key.description,
                }
            )
        else:
            own.append(
                {
                    "key": key,
                    "label": key.name,
                    "fill": "p:" + key.name,
                    "built": False,
                    "tags": False,
                    "why": key.description or "No description yet.",
                }
            )
    order = ["Date", "Type", "Context", "Account", "Tags", "Attendees", "Invitees", "Meeting"]
    built.sort(key=lambda item: order.index(str(item["label"])))
    return built + own


def schema_problem(error: Exception) -> str:
    """Why a change to the note schema was not kept, in a plain sentence."""
    if isinstance(error, RevisionConflict):
        return (
            "Settings were changed somewhere else while you were editing. Reload the page and "
            "try again." + ui.tech(str(error))
        )
    if isinstance(error, ValidationError):
        return (
            "Coppermind could not keep that. Check the name and options and try again."
            + ui.tech(str(error))
        )
    return e(str(error))


def router(wiring: Wiring) -> APIRouter:
    routes = APIRouter()

    # -- the Settings page ---------------------------------------------------------------

    @routes.get("/admin/settings", include_in_schema=False)
    async def settings_page(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        settings = ctx.settings()
        view = await ctx.view()
        try:
            _, schema = read_schema(ctx.control)
            shown = properties(schema)
        except (OSError, ValueError):
            shown = []
        hours = settings.admin.session_hours
        search = "Search by meaning is off, so search finds exact words only."
        if view and view["search"]["state"] == "on" and view["search"]["service"]:
            search = f"Search by meaning is on, using {e(view['search']['service']['name'])}."
        finished = view is not None and model.setup_next(model.setup_steps(view)) is None
        names = ", ".join(item["label"].lower() for item in shown[:4])
        rows = ui.rows(
            ui.row(
                "Time zone",
                e(zone_name(settings.general.timezone)),
                ui.go("Change", "/admin/settings/timezone"),
            ),
            ui.row(
                "Password and signing in",
                f"Password is set. Admin asks for it again after {duration(hours * 3600)}.",
                ui.go("Change", "/admin/settings/signin"),
            ),
            ui.row(
                "Folders",
                f'Notes to review go in "{e(settings.notes.review_folder)}". Reviewed notes '
                "are filed by type, year and month.",
                ui.go("Change", "/admin/settings/folders"),
            ),
            ui.row(
                "Note properties",
                f"{plural(len(shown), 'piece')} of information a note can carry, such as {names}.",
                ui.go("Change", "/admin/settings/properties"),
            ),
            ui.row("Search", search, ui.go("Open", "/admin/settings/search")),
            ui.row(
                "Colours",
                '<select class="lat-select inline theme" aria-label="Colours">'
                '<option value="system">Match my device</option>'
                '<option value="light">Light</option><option value="dark">Dark</option></select>'
                ' <span class="lat-badge lat-badge--ok" id="theme-saved" role="status" hidden>'
                "Saved on this browser</span>",
            ),
            ui.row("Setup guide", "Finished.", ui.go("Open", "/admin/setup")) if finished else "",
        )
        body = f"""{ui.head("Settings", "Everyday settings. Each line shows what is set now.")}
<div class="stack">{ctx.flash()}{ui.card(rows)}
<p class="subtle">{ui.a("Advanced", "/admin/settings/advanced")}: how often Coppermind checks
for changes, and details for support. You never need these to use Coppermind.</p></div>"""
        return await ctx.page("Settings", body, "settings")

    # -- time zone -----------------------------------------------------------------------

    @routes.get("/admin/settings/timezone", include_in_schema=False)
    async def timezone(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        current = ctx.settings().general.timezone
        query = ctx.q("q").strip()
        browser = ctx.q("browser")
        try:
            ZoneInfo(browser)
        except (ValueError, OSError, KeyError):
            browser = ""
        matches = [
            (identity, label)
            for identity, label in all_zones()
            if query.casefold() in label.casefold() or query.casefold() in identity.casefold()
        ]
        if not query:
            matches = [(identity, label) for identity, label in matches if identity in ZONES]
        shown = matches[:5]
        buttons = (
            "".join(
                '<form method="post" action="/admin/settings/timezone">'
                f"{ui.hidden({'zone': identity, 'browser': browser})}"
                f"<button>{e(label)}"
                + (" " + ui.badge("ok", "Current") if identity == current else "")
                + "</button></form>"
                for identity, label in shown
            )
            or '<div class="empty">No match. Try the nearest big city.</div>'
        )
        here = (
            ui.row(
                "This browser is in",
                e(zone_name(browser)),
                ui.badge("ok", "Same")
                if browser == current
                else ui.btn(
                    "Use this",
                    "/admin/settings/timezone",
                    "primary",
                    {"zone": browser, "browser": browser},
                ),
            )
            if browser
            else ""
        )
        more = (
            f'<p class="lat-hint" style="margin-top:6px">Showing 5 of {len(matches)}. Type a '
            "few letters to narrow it down.</p>"
            if len(matches) > len(shown)
            else ""
        )
        detect = (
            ""
            if browser or ctx.q("nb")
            else """<script>try{const z=Intl.DateTimeFormat().resolvedOptions().timeZone;
const u=new URL(location.href);u.searchParams.set(z?"browser":"nb",z||"1");location.replace(u)}
catch(e){}</script>"""
        )
        lede = (
            "Used for the dates on your notes and for anything Coppermind does at a set time "
            "of day."
        )
        body = f"""{ui.head("Time zone", lede, ("/admin/settings", "Settings"))}
<div class="stack">{ctx.flash()}
{
            ui.card(
                ui.rows(
                    ui.row(
                        "Now set to",
                        f"<b>{e(zone_name(current))}</b>. Times shown in Admin use it too.",
                    ),
                    here,
                )
            )
        }
{
            ui.card(f'''<form method="get" action="/admin/settings/timezone">
{ui.hidden({"browser": browser})}
{
                ui.text_input(
                    "q",
                    "Or type a city or zone, then pick it",
                    query,
                    live=True,
                    autofocus=bool(query),
                    hint="For example: Denver, London, Eastern.",
                )
            }</form>
<div class="tzlist">{buttons}</div>{more}''')
        }</div>{detect}"""
        return await ctx.page("Time zone", body, "settings")

    @routes.post("/admin/settings/timezone", include_in_schema=False)
    async def timezone_save(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        zone = form.get("zone", "")
        try:
            ZoneInfo(zone)
            ctx.save_settings(lambda doc: doc.setdefault("general", {}).update(timezone=zone))
        except (ValueError, OSError, KeyError, RevisionConflict):
            ctx.say("bad", "That time zone could not be saved. Pick one from the list.")
            return ctx.go("/admin/settings/timezone")
        ctx.say(
            "ok",
            f"Time zone saved: {e(zone_name(zone))}. New notes use it from now on. Notes "
            "already filed keep their dates.",
        )
        # The page works out the browser's zone with a reload, which would use up the message.
        # Sending it back with the zone it already knew means there is no reload.
        known = form.get("browser", "")
        return ctx.go(ui.url("/admin/settings/timezone", browser=known, nb="" if known else "1"))

    # -- password and signing in ---------------------------------------------------------

    def signin_body(ctx: Ctx, errors: dict[str, str]) -> str:
        hours = str(ctx.settings().admin.session_hours)
        change = f"""<form method="post" action="/admin/settings/signin">
<h3>Change the password</h3>
{
            ui.text_input(
                "cur",
                "Current password",
                kind="password",
                error=errors.get("cur", ""),
                autocomplete="current-password",
            )
        }
{
            ui.text_input(
                "new1",
                "New password",
                kind="password",
                hint="At least 12 characters.",
                error=errors.get("new1", ""),
                autocomplete="new-password",
            )
        }
{
            ui.text_input(
                "new2",
                "New password again",
                kind="password",
                error=errors.get("new2", ""),
                autocomplete="new-password",
            )
        }
<div class="actions">{ui.submit("Change password")}</div></form>"""
        stay = ui.rows(
            ui.row(
                "Stay signed in for",
                ui.quick_select(
                    "/admin/settings/signin/stay",
                    "hours",
                    "Stay signed in for",
                    with_value(SIGNIN, hours, duration(int(hours) * 3600)),
                    hours,
                ),
            )
        )
        return f"""{
            ui.head(
                "Password and signing in",
                "The password for these Admin screens, and how long you stay signed in.",
                ("/admin/settings", "Settings"),
            )
        }
<div class="stack">{ctx.flash()}{ui.card(change)}{ui.card(stay)}</div>"""

    @routes.get("/admin/settings/signin", include_in_schema=False)
    async def signin(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        return await ctx.page("Password and signing in", signin_body(ctx, {}), "settings")

    @routes.post("/admin/settings/signin", include_in_schema=False)
    async def signin_save(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        current, first, second = form.get("cur", ""), form.get("new1", ""), form.get("new2", "")
        errors: dict[str, str] = {}
        if not current:
            errors["cur"] = "Enter your current password."
        if len(first) < 12:
            errors["new1"] = "Use at least 12 characters."
        elif not second:
            errors["new2"] = "Type the new password again."
        elif first != second:
            errors["new2"] = "The two new passwords are different."
        credentials = request.app.state.credentials
        if not errors:
            try:
                if not await credentials.change_password(current, first):
                    errors["cur"] = "That is not the current password."
            except AdminRecordUnreadable:
                errors["cur"] = "The password could not be changed just now. Try again."
        if errors:
            return await ctx.page(
                "Password and signing in", signin_body(ctx, errors), "settings", status=422
            )
        # Changing the password signs every browser out; this one is signed straight back in.
        response = request.app.state.start_session("/admin/settings/signin")
        cookie = request.app.state.session_cookie(response)
        if cookie:
            request.app.state.flashes[cookie] = {
                "kind": "ok",
                "html": "Password changed. Other browsers signed in to Admin have been signed out.",
            }
        return response

    @routes.post("/admin/settings/signin/stay", include_in_schema=False)
    async def signin_stay(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        try:
            hours = int(form.get("hours", ""))
            ctx.save_settings(lambda doc: doc.setdefault("admin", {}).update(session_hours=hours))
        except (ValueError, RevisionConflict):
            ctx.say("bad", "That could not be saved. Pick one of the listed times.")
            return ctx.go("/admin/settings/signin")
        ctx.say(
            "ok",
            f"Saved. Admin asks for the password again after {duration(hours * 3600)}. This "
            "applies from your next sign-in.",
        )
        return ctx.go("/admin/settings/signin")

    # -- folders -------------------------------------------------------------------------

    FOLDER_FIELDS = (
        ("review", "Notes waiting for review"),
        ("filed", "Reviewed notes with no type"),
        ("drop", "Where you drop files for Coppermind"),
        ("files", "Files beside notes"),
        ("journal", "Journal"),
        ("trash", "Deleted notes"),
    )
    FOLDER_WORDS = {
        "review": "Notes waiting for review now go in",
        "filed": "Reviewed notes with no type now go in",
        "drop": "Files for Coppermind are now picked up from",
        "files": "Files beside notes now go in",
        "journal": "Journal notes now go in",
        "trash": "Deleted notes now go in",
    }

    def folders_draft(ctx: Ctx, data: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
        params = ctx.request.query_params
        draft = {key: params.get(key) or data["current"][key] for key, _ in FOLDER_FIELDS}
        dated = params.getlist("dated") if "d" in params else list(data["dated"])
        target = params.get("set")
        if target in draft:
            picked = (
                params.get("new_folder", "").strip()
                if params.get("folder") == "__new__"
                else params.get("folder", "")
            )
            if picked:
                draft[target] = picked
        if params.get("undate") in dated:
            dated.remove(params.get("undate", ""))
        if params.get("date") and params.get("date") not in dated:
            dated.append(params.get("date", ""))
        return draft, dated

    def folders_page(
        ctx: Ctx, data: dict[str, Any], lede: str, error: tuple[str, str] | None = None
    ) -> tuple[str, str, str, str]:
        draft, dated = folders_draft(ctx, data)
        carry = "".join(ui.hidden({key: value}) for key, value in draft.items())
        carry += ui.hidden({"d": 1}) + "".join(ui.hidden({"dated": item}) for item in dated)
        waiting = data["waiting"]
        hints = {
            "review": "New notes land here until you tick Reviewed. If you change it, the "
            f"{plural(waiting, 'note')} waiting now {'moves' if waiting == 1 else 'move'} to "
            "the new folder too.",
            "filed": "Notes with a type go in a folder named after the type instead.",
            "drop": "Calendar files and documents put here are picked up.",
            "files": "PDFs, slides and other files handed in with a note.",
            "journal": 'Notes of type "journal" go here, not in a type folder.',
            "trash": "Kept here until cleaned up.",
        }

        def pick(key: str, label: str) -> str:
            problem = error[1] if error and error[0] == key else ""
            return (
                f'<div class="field"><span class="lat-label">{label}</span>'
                f'<div style="display:flex;gap:6px"><input class="lat-input" readonly '
                f'value="{e(draft[key])}" aria-label="{label}">'
                f'<button class="lat-btn" name="pick" value="{key}">Choose…</button></div>'
                f'<div class="lat-hint">{hints[key]}</div>{ui.field_error(problem)}</div>'
            )

        chips = "".join(
            f'<span class="chip">{e(item)}<button name="undate" value="{e(item)}" '
            f'aria-label="Remove {e(item)}">&times;</button></span>'
            for item in dated
        )
        spare = [item for item in data["types"] if item not in dated]
        adder = (
            '<select class="lat-select inline" name="date" aria-label="Add one of your note '
            'types" data-auto><option value="">Add one of your note types…</option>'
            + "".join(f"<option>{e(item)}</option>" for item in spare)
            + '</select><noscript><button class="lat-btn">Add</button></noscript>'
            if spare
            else ""
        )
        left = "".join(pick(key, label) for key, label in FOLDER_FIELDS[:3])
        right = "".join(pick(key, label) for key, label in FOLDER_FIELDS[3:])
        example = ui.banner(
            "info",
            'Example: a reviewed note of type "work" dated Nov 10, 2026 is filed in the folder '
            '<b>work › 2026 › 2026-11</b> as "2026-11-10 Budget review".',
        )
        form = f"""<form method="get" action="/admin/settings/folders">{carry}
<div class="two"><div>{left}</div><div>{right}</div></div>
<div class="field field--wide"><span class="lat-label">Note types whose file name starts with
the date</span><div class="chips">{chips}{adder}</div>
<div class="lat-hint">The list offers the note types already used in your vault.</div></div>
</form>{example}
<form method="post" action="/admin/settings/folders">{carry}
<div class="actions">{ui.submit("Save", busy="Saving…")}{ui.go("Cancel", "/admin/settings")}
</div></form>"""
        dialog = ""
        target = ctx.q("pick")
        if target in draft:
            dialog = folder_dialog(
                "Choose a folder",
                data["folders"],
                draft[target],
                action="/admin/settings/folders",
                method="get",
                extra=carry + ui.hidden({"set": target}),
                cancel="/admin/settings/folders?"
                + "&".join(f"{key}={segment(value)}" for key, value in draft.items())
                + "&d=1"
                + "".join("&dated=" + segment(item) for item in dated),
            )
        body = f"""{ui.head("Folders", lede, ("/admin/settings", "Settings"))}
<div class="stack">{ctx.flash()}{ui.card(form)}</div>"""
        return "Folders", body, "settings", dialog

    async def show_folders(
        ctx: Ctx, data: dict[str, Any], lede: str, error: Any = None, status: int = 200
    ) -> Response:
        title, body, active, dialog = folders_page(ctx, data, lede, error)
        return await ctx.page(title, body, active, dialog=dialog, status=status)

    @routes.get("/admin/settings/folders", include_in_schema=False)
    async def folders_get(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        lede = (
            "Where things go in your Obsidian vault. Changing a folder affects new notes; "
            "notes already filed stay where they are."
        )
        try:
            data = await ctx.act("folders", {})
        except StoreError:
            return await ctx.unavailable("Folders", lede, "settings")
        return await show_folders(ctx, data, lede)

    @routes.post("/admin/settings/folders", include_in_schema=False)
    async def folders_save(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        raw = (await request.body()).decode(errors="replace")
        from urllib.parse import parse_qs

        fields = parse_qs(raw, keep_blank_values=True)
        payload: dict[str, Any] = {
            key: fields.get(key, [""])[-1] for key, _ in FOLDER_FIELDS if fields.get(key)
        }
        payload["dated"] = fields.get("dated", [])
        try:
            result = await ctx.act("folders-save", payload)
        except StoreError as exc:
            return await ctx.refused(exc, "/admin/settings/folders")
        if "refused" in result:
            ctx.say("bad", e(result["refused"]["message"]))
            return ctx.go("/admin/settings/folders")
        changed, was, moved = result["changed"], result["was"], result["moved"]
        lines = []
        for key, value in changed.items():
            line = f'{FOLDER_WORDS[key]} "{e(value)}".'
            if key == "review":
                line += (
                    f" The {plural(moved, 'note')} that {'was' if moved == 1 else 'were'} "
                    f'waiting in "{e(was[key])}" {"was" if moved == 1 else "were"} moved there '
                    "too."
                )
            else:
                line += f' What is already in "{e(was[key])}" stays there.'
            lines.append(line)
        ctx.say(
            "ok",
            "Folders saved. " + " ".join(lines) if lines else "Folders saved. Nothing was changed.",
        )
        return ctx.go("/admin/settings")

    # -- note properties -----------------------------------------------------------------

    async def fill_state(ctx: Ctx) -> dict[str, Any] | None:
        try:
            return await ctx.act("suggestions", {})
        except StoreError:
            return None

    @routes.get("/admin/settings/properties", include_in_schema=False)
    async def properties_page(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        _, schema = read_schema(ctx.control)
        sugg = await fill_state(ctx)
        on = bool(sugg and sugg["state"] != "none")
        rows = ""
        shown = properties(schema)
        for item in shown:
            key: KeyDefinition = item["key"]
            detail = e(item["why"])
            if key.kind == "enum" and key.vocabulary:
                detail += f" Options: {e(', '.join(key.vocabulary))}."
            if item["tags"]:
                detail += (
                    f" Any tag is allowed; {len(schema.tags)} have a meaning written down."
                    if schema.tag_mode == "open"
                    else f" Only the {plural(len(schema.tags), 'tag')} on your list are allowed."
                )
            elif on and sugg:
                detail += (
                    " Suggestions fill it in."
                    if sugg["fill"].get(item["fill"])
                    else " Suggestions do not fill it in."
                )
            here = "/admin/settings/properties/edit/" + segment(key.name)
            actions = (
                ui.go("Manage tags", "/admin/settings/tags")
                if item["tags"]
                else ui.go("Edit", here)
            )
            if not item["built"]:
                actions += ui.go("Remove", ui.url("/admin/settings/properties", ask=key.name))
            rows += ui.item(
                f"{e(item['label'])} {ui.badge('', KINDS[key.kind][0])}", detail, actions
            )
        dialog = ""
        asked = ctx.q("ask")
        if asked and any(not i["built"] and i["key"].name == asked for i in shown):
            dialog = ui.dialog(
                f'Remove the property "{e(asked)}"?',
                "New notes stop carrying it. Notes that already have it keep what is written "
                "there.",
                "/admin/settings/properties/remove",
                "Remove",
                "/admin/settings/properties",
                danger=True,
                fields={"name": asked},
            )
        built = sum(1 for item in shown if item["built"])
        body = f"""{
            ui.head(
                "Note properties",
                "The pieces of information every note can carry, shown at the top "
                "of a note in Obsidian. Suggestions can fill them in.",
                ("/admin/settings", "Settings"),
            )
        }
<div class="stack">{ctx.flash()}
<div class="actions actions--top">
{ui.go("Add a property", "/admin/settings/properties/edit/new", "primary")}</div>
{ui.card(rows)}
<p class="lat-hint">The first {built} come with Coppermind and cannot be removed, but you can
edit their descriptions and options.</p></div>"""
        return await ctx.page("Note properties", body, "settings", dialog=dialog)

    @routes.post("/admin/settings/properties/remove", include_in_schema=False)
    async def property_remove(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        name = form.get("name", "")
        revision, schema = read_schema(ctx.control)
        target = next(
            (i for i in properties(schema) if not i["built"] and i["key"].name == name), None
        )
        if target is None:
            return ctx.go("/admin/settings/properties")
        try:
            # The tick goes first: suggestions cannot be left filling a property that is gone.
            await ctx.act("suggestions-tick", {"key": target["fill"], "on": False})
            revision, schema = read_schema(ctx.control)
            schema.keys = [key for key in schema.keys if key.name != name]
            write_schema(ctx.control, schema, revision)
        except (StoreError, ValueError, RevisionConflict) as exc:
            return await ctx.refused(exc, "/admin/settings/properties")
        ctx.say("ok", f'Removed "{e(name)}". Existing notes were not changed.')
        return ctx.go("/admin/settings/properties")

    async def edit_page(
        ctx: Ctx, name: str, values: dict[str, str], errors: dict[str, str], status: int = 200
    ) -> Response:
        _, schema = read_schema(ctx.control)
        new = name == "new"
        item = next((i for i in properties(schema) if i["key"].name == name), None)
        if not new and item is None:
            return ctx.go("/admin/settings/properties")
        key = item["key"] if item else KeyDefinition(name="", kind="string")
        built = bool(item and item["built"])
        label = item["label"] if item else ""
        kind = values.get("kind", key.kind)
        sugg = await fill_state(ctx)
        on = bool(sugg and sugg["state"] != "none")
        if built:
            identity = ui.rows(
                ui.row("Name", e(label)),
                ui.row("Kind", f"{KINDS[key.kind][0]}. {KINDS[key.kind][1]}"),
            )
        else:
            kinds = "".join(
                ui.choice("kind", value, title, example, value == kind)
                for value, (title, example) in KINDS.items()
            )
            identity = ui.text_input(
                "name",
                "Name",
                values.get("name", key.name),
                hint="For example: Project",
                error=errors.get("name", ""),
            ) + (
                '<div class="field field--wide"><span class="lat-label">What kind of '
                f"information is it?</span>{kinds}</div>"
            )
        options = ui.text_input(
            "options",
            "The options, separated by commas",
            values.get("options", ", ".join(key.vocabulary)),
            hint="For example: low, medium, high",
            error=errors.get("options", ""),
        )
        options = (
            options
            if built and key.kind == "enum"
            else f'<div data-if="kind=enum">{options}</div>'
            if not built
            else ""
        )
        why = ui.text_input(
            "why",
            "What it is for",
            values.get(
                "why",
                key.description
                if not built or label not in BUILT_WHY
                else key.description or BUILT_WHY[label],
            ),
            hint="Shown to you here, and given to the AI model so it fills the property in "
            "sensibly.",
        )
        if on and sugg:
            both = (
                "Attendees and invitees are filled in together, so this tick covers both. "
                if label in {"Attendees", "Invitees"}
                else ""
            )
            ticked = True if new else bool(sugg["fill"].get(item["fill"] if item else ""))
            fill = ui.check(
                "fill",
                "Let suggestions fill this in",
                both + "The same tick as on the Suggestions page; change it in either place.",
                checked=("fill" in values) if values else ticked,
            ) + ui.hidden({"fill_shown": 1})
        else:
            fill = (
                '<p class="muted">Suggestions are off, so nothing fills this in for you. '
                + ui.a("Turn on suggestions", "/admin/suggestions")
                + "</p>"
            )
        title = "Add a property" if new else f'Edit "{e(label)}"'
        lede = (
            "One more piece of information your notes can carry."
            if new
            else "Change what this property is for"
            + (". It comes with Coppermind, so its name and kind are fixed." if built else ".")
        )
        body = f"""{ui.head(title, lede, ("/admin/settings/properties", "Note properties"))}
<div class="stack">{ui.banner("bad", errors["save"]) if errors.get("save") else ""}{
            ui.card(f'''<form method="post"
action="/admin/settings/properties/edit/{segment(name)}">{identity}{options}{why}{fill}
<div class="actions">{ui.submit("Add property" if new else "Save")}
{ui.go("Cancel", "/admin/settings/properties")}</div></form>''')
        }</div>"""
        return await ctx.page(
            "Add a property" if new else "Edit a property", body, "settings", status=status
        )

    @routes.get("/admin/settings/properties/edit/{name}", include_in_schema=False)
    async def property_edit(name: str, request: Request) -> Response:
        return await edit_page(Ctx(request, wiring), name, {}, {})

    @routes.post("/admin/settings/properties/edit/{name}", include_in_schema=False)
    async def property_save(name: str, request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        revision, schema = read_schema(ctx.control)
        new = name == "new"
        item = next((i for i in properties(schema) if i["key"].name == name), None)
        if not new and item is None:
            return ctx.go("/admin/settings/properties")
        built = bool(item and item["built"])
        errors: dict[str, str] = {}
        if built and item:
            key = item["key"]
            wanted, kind = key.name, key.kind
        else:
            wanted = re.sub(r"\s+", "_", form.get("name", "").strip())
            kind = form.get("kind", "string")
            if not wanted:
                errors["name"] = "Give the property a name."
            elif not KEY_NAME_PATTERN.fullmatch(wanted):
                errors["name"] = (
                    "Use letters, numbers, dashes and underscores, starting with a letter."
                )
            elif wanted != name and any(
                k.name.casefold() == wanted.casefold() for k in schema.keys
            ):
                errors["name"] = f'There is already a property called "{e(wanted)}".'
            if kind not in KINDS:
                kind = "string"
        choices = [part.strip() for part in form.get("options", "").split(",") if part.strip()]
        if (
            kind == "enum"
            and len(choices) < 2
            and not (built and item and item["key"].vocabulary and "options" not in form)
        ):
            errors["options"] = "List at least two options, separated by commas."
        if errors:
            return await edit_page(ctx, name, form, errors, 422)
        description = form.get("why", "").strip()
        if new or item is None:
            definition = KeyDefinition(name=wanted, kind=cast(Any, kind), description=description)
            schema.keys.append(definition)
        else:
            definition = item["key"]
            definition.name, definition.kind = wanted, cast(Any, kind)
            definition.description = description
        if kind == "enum" and choices:
            definition.vocabulary = choices
            definition.vocabulary_meanings = {
                k: v for k, v in definition.vocabulary_meanings.items() if k in choices
            }
            if definition.default is not None and definition.default not in choices:
                definition.default = choices[0]
        elif kind != "string":
            definition.vocabulary, definition.vocabulary_meanings = [], {}
        try:
            write_schema(ctx.control, schema, revision)
        except (ValueError, RevisionConflict) as exc:
            errors["save"] = schema_problem(exc)
            return await edit_page(ctx, name, form, errors, 422)
        said = (
            f'Added "{e(wanted)}". New notes carry it from now on.'
            if new
            else f'Saved "{e(item["label"] if item else wanted)}".'
        )
        if form.get("fill_shown"):
            fill_key = item["fill"] if built and item else "p:" + wanted
            try:
                await ctx.act("suggestions-tick", {"key": fill_key, "on": "fill" in form})
                said += (
                    " Suggestions will fill it in."
                    if "fill" in form and new
                    else " Suggestions will not fill it in."
                    if new
                    else " Suggestions fill it in."
                    if "fill" in form
                    else " Suggestions do not fill it in."
                )
            except StoreError:
                said += " The suggestions tick could not be saved; set it on the Suggestions page."
        ctx.say("ok", said)
        return ctx.go("/admin/settings/properties")

    # -- tags ----------------------------------------------------------------------------

    @routes.get("/admin/settings/tags", include_in_schema=False)
    async def tags(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        _, schema = read_schema(ctx.control)
        sugg = await fill_state(ctx)
        on = bool(sugg and sugg["state"] != "none")
        editing = ctx.q("edit")
        mode = ui.card(
            '<form method="post" action="/admin/settings/tags/mode">'
            + ui.choice(
                "mode",
                "open",
                "Allow any tag",
                "New tags can be made up on the fly, by you or by suggestions.",
                schema.tag_mode == "open",
            )
            + ui.choice(
                "mode",
                "closed",
                "Only tags from my list",
                "A tag that is not on the list below is refused. Tags a note already "
                "has are left alone.",
                schema.tag_mode == "closed",
            )
            + f'<div class="actions">{ui.submit("Save", "")}</div></form>'
        )
        tick = (
            '<form class="act" method="post" action="/admin/settings/tags/fill">'
            + ui.check(
                "fill",
                "Let suggestions add tags",
                "The same tick as on the Suggestions page."
                if on
                else "Suggestions are off, so nothing adds tags for you.",
                checked=bool(sugg and sugg["fill"].get("tags")),
                disabled=not on,
            ).replace("<input ", "<input data-auto ", 1)
            + '<noscript><button class="lat-btn">Save</button></noscript></form>'
        )
        blank = '<span class="subtle">Not written down</span>'
        nothing = '<span class="subtle">Nothing</span>'
        rows = [
            [
                e(tag),
                e(meaning) or blank,
                e(", ".join(schema.aliases_of(tag))) or nothing,
                ui.go("Edit", ui.url("/admin/settings/tags", edit=tag))
                + " "
                + ui.btn("Remove", "/admin/settings/tags/remove", "", {"tag": tag}),
            ]
            for tag, meaning in schema.tags.items()
        ]
        current = editing if editing in schema.tags else ""
        form = f"""<h3 style="margin-top:12px">{
            f'Edit "{e(current)}"' if current else "Add a tag"
        }</h3>
<form method="post" action="/admin/settings/tags/save">{ui.hidden({"was": current})}
<div class="two"><div>{ui.text_input("tag", "Tag", current)}
{
            ui.text_input(
                "also",
                "Also written as (optional)",
                ", ".join(schema.aliases_of(current)) if current else "",
                hint="Other spellings, separated by commas. They are turned into this tag.",
            )
        }
</div><div>{
            ui.text_input(
                "why", "What it means (optional)", schema.tags.get(current, "") if current else ""
            )
        }</div></div>
<div class="actions">{ui.submit("Save tag" if current else "Add tag", "primary" if current else "")}
{ui.go("Cancel", "/admin/settings/tags") if current else ""}
{ui.go("Back to Note properties", "/admin/settings/properties")}</div></form>"""
        listing = (
            ui.table(["Tag", "What it means", "Also written as", ""], rows)
            if rows
            else (ui.empty("No tags on your list yet", "Add one below."))
        )
        body = f"""{
            ui.head(
                "Tags",
                "Tags are your own labels on notes. Here you say which ones are "
                "allowed and what each one means.",
                ("/admin/settings/properties", "Note properties"),
            )
        }
<div class="stack">{ctx.flash()}{mode}{ui.card(tick + listing + form, "Your tags")}</div>"""
        return await ctx.page("Tags", body, "settings")

    def tag_change(ctx: Ctx, change: Any) -> str:
        revision, schema = read_schema(ctx.control)
        try:
            change(schema)
            checked = FrontmatterSchema.model_validate(schema.model_dump())
            write_schema(ctx.control, checked, revision)
        except (ValueError, RevisionConflict) as exc:
            return schema_problem(exc)
        return ""

    @routes.post("/admin/settings/tags/mode", include_in_schema=False)
    async def tags_mode(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        mode = "closed" if form.get("mode") == "closed" else "open"
        problem = tag_change(ctx, lambda schema: setattr(schema, "tag_mode", mode))
        _, schema = read_schema(ctx.control)
        if problem:
            ctx.say("bad", "That could not be saved. " + problem)
        else:
            ctx.say(
                "ok",
                "Tags saved. Any tag is allowed."
                if mode == "open"
                else f"Tags saved. Only the {plural(len(schema.tags), 'tag')} on your list are "
                "allowed from now on.",
            )
        return ctx.go("/admin/settings/tags")

    @routes.post("/admin/settings/tags/fill", include_in_schema=False)
    async def tags_fill(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        try:
            await ctx.act("suggestions-tick", {"key": "tags", "on": "fill" in form})
        except StoreError as exc:
            return await ctx.refused(exc, "/admin/settings/tags")
        ctx.say(
            "ok",
            "Saved. Suggestions may add tags."
            if "fill" in form
            else "Saved. Suggestions no longer add tags.",
        )
        return ctx.go("/admin/settings/tags")

    @routes.post("/admin/settings/tags/save", include_in_schema=False)
    async def tags_save(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        tag, was = form.get("tag", "").strip().lstrip("#"), form.get("was", "")
        if not tag:
            ctx.say("bad", "Type the tag first.")
            return ctx.go(ui.url("/admin/settings/tags", edit=was))
        aliases = [part.strip().lstrip("#") for part in form.get("also", "").split(",")]
        aliases = [alias for alias in aliases if alias and alias != tag]

        def change(schema: FrontmatterSchema) -> None:
            if tag != was and tag in schema.tags:
                raise ValueError(
                    f'The tag "{tag}" is already on your list. Edit that one to change it.'
                )
            if was and was in schema.tags:
                tags = {}
                for name, meaning in schema.tags.items():
                    tags[tag if name == was else name] = (
                        form.get("why", "").strip() if name == was else meaning
                    )
                schema.tags = tags
            else:
                schema.tags[tag] = form.get("why", "").strip()
            schema.tag_aliases = {
                alias: canonical
                for alias, canonical in schema.tag_aliases.items()
                if canonical not in {was, tag} and alias != tag
            }
            for alias in aliases:
                schema.tag_aliases[alias] = tag

        problem = tag_change(ctx, change)
        if problem:
            ctx.say("bad", "That tag could not be saved. " + problem)
            return ctx.go(ui.url("/admin/settings/tags", edit=was))
        ctx.say("ok", f'Saved the tag "{e(tag)}".' if was else f'Added the tag "{e(tag)}".')
        return ctx.go("/admin/settings/tags")

    @routes.post("/admin/settings/tags/remove", include_in_schema=False)
    async def tags_remove(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        tag = form.get("tag", "")

        def change(schema: FrontmatterSchema) -> None:
            schema.tags.pop(tag, None)
            schema.tag_aliases = {a: c for a, c in schema.tag_aliases.items() if c != tag}

        problem = tag_change(ctx, change)
        if problem:
            ctx.say("bad", "That tag could not be removed. " + problem)
        else:
            ctx.say(
                "ok",
                f'Removed "{e(tag)}" from your list. Notes that already carry it keep it.',
            )
        return ctx.go("/admin/settings/tags")

    # -- search --------------------------------------------------------------------------

    @routes.get("/admin/settings/search", include_in_schema=False)
    async def search(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        try:
            data = await ctx.act("search", {})
            listed = (await ctx.act("services", {}))["services"]
            query = ctx.q("q").strip()
            hits = (await ctx.act("search-try", {"q": query}))["hits"] if query else []
        except StoreError:
            return await ctx.unavailable("Search", SEARCH_LEDE, "settings")
        use = ctx.q("use")
        index = data["index"]
        embeddings = index["embeddings"]
        entries = index["notes"] + index["sources"] + index["artifacts"]
        if index["rebuild_running"]:
            health = "Being rebuilt now. Search keeps working while it runs."
        elif index.get("rebuild_error"):
            health = "The last rebuild did not finish. Press Rebuild search to run it again."
        elif entries:
            health = (
                f"Up to date: {plural(index['notes'], 'note')} and "
                f"{plural(index['artifacts'], 'file')} can be searched."
            )
        else:
            health = "Nothing to search yet."
        found = ""
        if query:
            found = "".join(
                ui.item(
                    e(hit["title"]),
                    "Found by meaning: about the same subject, though the words differ."
                    if hit["match"] == "meaning"
                    else "Matches the words you typed.",
                )
                for hit in hits
            ) or ui.empty(
                "Nothing found",
                ""
                if data["state"] == "on"
                else "Search by meaning is off, so only exact words are matched.",
            )
        top = ui.card(
            ui.rows(
                ui.row(
                    "Search",
                    health,
                    ui.btn("Rebuild search", "/admin/settings/search/rebuild", busy="Rebuilding…"),
                )
            )
            + f"""<form method="get" action="/admin/settings/search">
{
                ui.text_input(
                    "q",
                    "Try a search",
                    query,
                    live=True,
                    autofocus=bool(query),
                    hint="Searches your notes, files and people the way your assistants and "
                    "Obsidian do.",
                )
            }</form>{found}""",
            "Search",
        )
        service = data["service"]
        if data["state"] == "on" and service:
            here = {"back": "/admin/settings/search"}
            test = ui.btn(
                "Test now",
                f"/admin/services/{segment(service['id'])}/test",
                "" if service["answering"] else "primary",
                here,
                busy="Testing…",
            )
            edit = ui.go("Edit this service", service_path(service, **{"for": "search"}))
            off = ui.btn("Turn off", "/admin/settings/search/off")
            if not service["answering"]:
                status = ui.status(
                    "bad",
                    "Search by meaning has stopped",
                    f"{e(service['name'])} is not answering. Search still works, using exact "
                    "words." + stopped_help(service, 0, ""),
                    test + edit + off,
                )
            elif embeddings["state"] == "unavailable":
                status = ui.status(
                    "warn",
                    "Search by meaning cannot run on this server",
                    "The database this server uses cannot store what search by meaning needs. "
                    "Search still works, using exact words. Whoever runs the server can see "
                    "why under Details for support." + ui.tech(embeddings.get("error", "")),
                    off,
                )
            else:
                total, done = embeddings["documents"], embeddings["embedded"]
                progress = (
                    f"Coppermind has read all {plural(total, 'note and file', 'notes and files')}."
                    if total and done >= total
                    else f"Coppermind has read {done:,} of {total:,} so far. It carries on in "
                    "the background."
                    if total
                    else "Nothing to read yet."
                )
                status = ui.status("ok", "Search by meaning is on", progress, off)
            meaning = status + ui.card(
                ui.rows(
                    ui.row(
                        "AI service",
                        f"{e(service['name'])} {health_badge(service)} {where_text(service)}",
                        ""
                        if not service["answering"]
                        else test
                        + edit
                        + ui.go("Use a different service", "/admin/settings/search?change=1"),
                    )
                )
            )
        if data["state"] != "on" or not service or ctx.q("change") or use:
            current = service["id"] if service else ""
            chooser = f"""<form method="post" action="/admin/settings/search/on">
<h3>Which AI service should read your notes for this?</h3>
{service_choices("svc", "embed", "search", listed, ctx.zone, current=current, chosen=use)}
<p class="muted" style="margin-top:8px">When on, the text of your notes, files and people pages
is sent to this service a little at a time, in the background.</p>
<div class="actions">
{ui.submit("Turn on", busy="Turning on…") if any(s["embed"]["ok"] for s in listed) else ""}
{ui.go("Cancel", "/admin/settings/search") if ctx.q("change") else ""}</div></form>"""
            if data["state"] != "on" or not service:
                meaning = ui.status(
                    "off", "Search by meaning is off", "Search still works, using exact words."
                ) + ui.card(chooser)
            else:
                meaning = ui.banner(
                    "info",
                    "Choose the service to use instead, then press Turn on. Until then search "
                    f"by meaning keeps using {e(service['name'])}.",
                ) + ui.card(chooser)
        body = f"""{ui.head("Search", SEARCH_LEDE, ("/admin/settings", "Settings"))}
<div class="stack">{ctx.flash()}{top}{meaning}</div>"""
        return await ctx.page("Search", body, "settings")

    @routes.post("/admin/settings/search/on", include_in_schema=False)
    async def search_on(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        try:
            result = await ctx.act("search-on", {"connection": form.get("svc", "")})
        except StoreError as exc:
            return await ctx.refused(exc, "/admin/settings/search")
        if "refused" in result:
            ctx.say("bad", e(result["refused"]["message"]))
            return ctx.go("/admin/settings/search")
        ctx.say(
            "ok",
            f"Search by meaning is on, using {e(result['service'])}. Coppermind is now reading "
            "your notes in the background. Results get better as it goes.",
        )
        return ctx.go("/admin/settings/search")

    @routes.post("/admin/settings/search/off", include_in_schema=False)
    async def search_off(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        await ctx.form()
        try:
            await ctx.act("search-off", {})
        except StoreError as exc:
            return await ctx.refused(exc, "/admin/settings/search")
        ctx.say(
            "ok",
            "Search by meaning is off. Nothing more is sent to the AI service. Search uses "
            "exact words.",
        )
        return ctx.go("/admin/settings/search")

    @routes.post("/admin/settings/search/rebuild", include_in_schema=False)
    async def search_rebuild(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        await ctx.form()
        try:
            result = await ctx.act("search-rebuild", {})
        except StoreError as exc:
            return await ctx.refused(exc, "/admin/settings/search")
        ctx.say(
            "ok",
            "Rebuilding search now. It runs in the background and search keeps working "
            "meanwhile. No note is changed."
            if result["started"]
            else "A rebuild is already running. It finishes by itself.",
        )
        return ctx.go("/admin/settings/search")

    # -- advanced ------------------------------------------------------------------------

    @routes.get("/admin/settings/advanced", include_in_schema=False)
    async def advanced(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        tab = "about" if ctx.q("tab") == "about" else "timing"
        settings = ctx.settings()
        if tab == "timing":
            current = {
                "scan": (
                    str(settings.reconcile.scan_interval_s),
                    duration(settings.reconcile.scan_interval_s),
                ),
                "quiet": (
                    str(settings.reconcile.quiet_period_s),
                    duration(settings.reconcile.quiet_period_s),
                ),
                "daily": (
                    settings.reconcile.full_rehash_daily_at,
                    settings.reconcile.full_rehash_daily_at,
                ),
                "history": (str(settings.git.debounce_s), duration(settings.git.debounce_s)),
            }
            rows = "".join(
                ui.row(
                    label,
                    f'<select class="lat-select inline" name="{name}" aria-label="{label}">'
                    + ui.options(with_value(choices, *current[name]), current[name][0])
                    + f'</select> <span class="lat-hint">{hint}</span>',
                )
                for name, (label, choices, hint) in TIMING.items()
            )
            inner = f"""<form method="post" action="/admin/settings/advanced">
<div class="rows">{rows}</div><div class="actions">{ui.submit("Save")}</div></form>"""
        else:
            try:
                support = await ctx.act("support", {})
            except StoreError:
                return await ctx.unavailable(
                    "Advanced", "Details for support need Coppermind to be answering.", "settings"
                )
            history = support["history"]
            if history["available"]:
                record = (
                    "Working. Coppermind keeps a record of every change to your notes, which is "
                    "what lets a deleted note be brought back."
                    + (
                        f" Last recorded {ui.ago(history['last_commit_at'], ctx.zone)}."
                        if history.get("last_commit_at")
                        else " Nothing has been recorded yet."
                    )
                )
            else:
                record = (
                    "Not working right now, so a note deleted from now on may not be "
                    "recoverable. " + e(history["reason"])
                )
            import json

            details = json.dumps(
                {"version": request.app.state.version, **support}, indent=2, sort_keys=True
            )
            inner = ui.rows(
                ui.row("Version", f"Coppermind {e(request.app.state.version)}"),
                ui.row("Record of changes", record),
                ui.row(
                    "For support",
                    "A summary of this install with no note content and no keys."
                    ' <span class="lat-badge lat-badge--ok" id="support-copied" role="status" '
                    "hidden>Copied. Paste it into your message to support.</span>"
                    + ui.reveal(
                        "Show the details", f'<pre class="lat-code" id="support">{e(details)}</pre>'
                    ),
                    '<button type="button" class="lat-btn" data-copy="support">Copy details'
                    "</button>",
                ),
            )
        body = f"""{
            ui.head(
                "Advanced",
                "You never need this page to use Coppermind. It is here for fine "
                "tuning and for when support asks.",
                ("/admin/settings", "Settings"),
            )
        }
<div class="stack">{ctx.flash()}
{ui.seg("/admin/settings/advanced", [("timing", "Timing"), ("about", "About")], tab)}
{ui.card(inner)}</div>"""
        return await ctx.page("Advanced", body, "settings")

    @routes.post("/admin/settings/advanced", include_in_schema=False)
    async def advanced_save(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()

        def change(document: dict[str, Any]) -> None:
            reconcile = document.setdefault("reconcile", {})
            reconcile["scan_interval_s"] = int(form["scan"])
            reconcile["quiet_period_s"] = int(form["quiet"])
            reconcile["full_rehash_daily_at"] = form["daily"]
            document.setdefault("git", {})["debounce_s"] = int(form["history"])

        try:
            ctx.save_settings(change)
        except (ValueError, KeyError, RevisionConflict):
            ctx.say("bad", "Those values could not be saved. Pick from the lists and try again.")
            return ctx.go("/admin/settings/advanced")
        ctx.say("ok", "Saved. The new values are in use within a minute.")
        return ctx.go("/admin/settings/advanced")

    return routes


def folder_dialog(
    title: str,
    folders: list[str],
    current: str,
    *,
    action: str,
    cancel: str,
    method: str = "post",
    extra: str = "",
    error: str = "",
    field: str = "folder",
) -> str:
    """The folder picker: the folders in the vault, or a new one. It opens on the one in use."""
    known = current in folders
    choices = "".join(
        ui.choice(
            field,
            name,
            e(name) + (" (in use now)" if name == current else ""),
            "",
            name == current,
        )
        for name in folders
    ) + ui.choice(field, "__new__", "A new folder", "", bool(current) and not known)
    using = f'In use now: "{e(current)}". ' if current else ""
    return (
        f'<div class="overlay"><div class="dialog" role="dialog" aria-modal="true" '
        f'aria-label="{e(title)}"><form method="{method}" action="{e(action)}">{extra}'
        f'<h2>{title}</h2><p class="muted">{using}Folders in your vault:</p>'
        f'<div class="pickgrid">{choices}</div>'
        f'<div data-if="{field}=__new__">'
        + ui.text_input("new_folder", "Name of the new folder", "" if known else current)
        + "</div>"
        + ui.field_error(error)
        + '<div class="actions actions--end">'
        f'<a class="lat-btn" href="{e(cancel)}" data-cancel>Cancel</a>'
        '<button class="lat-btn lat-btn--primary">Use this folder</button></div>'
        "</form></div></div>"
    )
