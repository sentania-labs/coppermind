"""Coppermind: connect Obsidian Sync, the AI server and the calendar once, and see they work.

Each connection is one card with one plain line that says whether it is working and, when it
is not, the one thing to press. Everything that is set once and left alone is on Advanced.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from coppermind.settings import Wiring
from coppermind.store_protocol import StoreError
from coppermind_admin import model, ui
from coppermind_admin.pages import ai
from coppermind_admin.pages.companion import companion_files, companion_needs_update, updated_text
from coppermind_admin.pages.incoming import file_dialog
from coppermind_admin.shell import Ctx
from coppermind_admin.ui import e, plural

LEDE = "Connect each thing once. This page then says whether it is working."
EXPORT_HELP = (
    "Google Calendar: Settings, then Import and export, then Export. Outlook: File, then Save "
    "Calendar. Apple Calendar: File, then Export."
)


def sync_row(view: dict[str, Any], ctx: Ctx) -> str:
    sync, state = view["sync"], model.sync_state(view["sync"])
    vault = e(sync.get("vault_name") or "your vault")
    change = sync.get("last_activity_at") or (sync.get("last_change") or {}).get("at")
    if state == "none":
        kind, said = (
            "off",
            ("Not connected. Connect Obsidian Sync so your notes reach your phone and computer."),
        )
        action = ui.go("Connect Obsidian Sync", "/admin/sync", "primary")
    elif state == "signin":
        kind, said = "bad", "Sync has stopped. Obsidian needs you to sign in again."
        action = ui.go("Sign in again", "/admin/sync", "primary")
    elif state == "paused":
        kind, said = "warn", "Paused. Notes are not travelling until you resume it."
        action = ui.btn("Resume", "/admin/sync/resume", "primary", {"back": "/admin"})
    else:
        kind = "ok"
        said = f'Syncing "{vault}".' + (
            f" Last change {ui.ago(change, ctx.zone)}." if change else ""
        )
        action = ui.go("Change", "/admin/sync")
    return ui.row("Obsidian Sync", f"{ui.dot(kind)} {said}", action)


def companion_row(view: dict[str, Any]) -> str:
    """The companion's line, with its update and the note that it was updated folded in."""
    if model.sync_state(view["sync"]) == "none":
        return ""
    companion = view["companion"]
    paired = companion["paired"]
    if not companion["placed"]:
        kind, said = "off", "Not in your vault yet."
        action = ui.go("Add the companion", "/admin/setup/companion", "primary")
    elif not paired:
        kind, said = "warn", "In your vault, and not paired with a device yet."
        action = ui.go("Pair a device", "/admin/setup/companion", "primary")
    else:
        devices = sorted({app["device"] for app in paired})
        kind, said = "ok", "On " + ui.sentence_list([e(name) for name in devices]) + "."
        action = ui.go("Add a device", "/admin/apps/pair?from=coppermind")
    if companion_needs_update(view):
        version = json.loads(companion_files()["manifest.json"])["version"]
        kind = "warn" if kind == "ok" else kind
        said += f" An update to version {e(version)} is ready."
        action += ui.btn(
            "Update the companion",
            "/admin/setup/companion/place",
            "primary",
            {"again": "1", "back": "/admin"},
            busy="Updating…",
        )
    for notice in view["notices"]:
        if notice["kind"] == "companion_updated":
            said += " " + updated_text(notice)
            action += ui.btn("Got it", "/admin/notices/dismiss", "", {"id": notice["id"]})
    return ui.row("Companion", f"{ui.dot(kind)} {said}", action)


def calendar_row(view: dict[str, Any]) -> str:
    calendar = view["calendar"]
    add = ui.go("Add a calendar file", "/admin?ask=calendar")
    if calendar.get("unavailable"):
        kind, said, action = "warn", "Not read just now. Reload in a moment.", ""
    elif calendar["bad_files"]:
        kind, said = "warn", "A calendar file could not be read."
        action = ui.go("Look", "/admin/calendar", "primary") + add
    elif calendar["conflicts"]:
        kind, said = "warn", "Two calendar files disagree about a meeting."
        action = ui.go("Choose", "/admin/calendar", "primary") + add
    elif not calendar["files"]:
        kind, said = (
            "off",
            ("No calendar yet. Add a calendar file so a recording is matched to its meeting."),
        )
        action = ui.go("Add a calendar file", "/admin?ask=calendar", "primary")
    else:
        kind = "ok"
        said = f"Up to date, with {plural(calendar['events'], 'meeting')}."
        action = add
    return ui.row("Calendar", f"{ui.dot(kind)} {said}", action)


def router(wiring: Wiring) -> APIRouter:
    routes = APIRouter()

    @routes.get("/admin", include_in_schema=False)
    async def coppermind(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        view = await ctx.view()
        if view is None:
            return await ctx.unavailable("Coppermind", LEDE, "home")
        artificial = await ai.read_ai(ctx, view)
        obsidian = ui.card(ui.rows(sync_row(view, ctx), companion_row(view)), "Obsidian")
        calendar = ui.card(ui.rows(calendar_row(view)), "Calendar")
        arrive = ui.card(
            ui.rows(
                ui.row(
                    "Drop folder",
                    f'Put a recording, calendar file or document in the "{e(view["drop_folder"])}" '
                    "folder in your vault.",
                )
            ),
            "Recordings arrive",
        )
        dialog = ""
        if ctx.q("ask") == "calendar":
            dialog = file_dialog(
                "Add a calendar file",
                "Choose the calendar file you exported from your calendar app (its name usually "
                f"ends in .ics). {EXPORT_HELP}",
                "/admin/calendar/add",
                "/admin",
                {"back": "/admin"},
                accept=".ics,.zip,text/calendar",
            )
        body = f"""{ui.head("Coppermind", LEDE)}<div class="stack">{ctx.flash()}
{obsidian}{ai.card(artificial)}{calendar}{arrive}
<p class="subtle">Password, time zone, folders, note properties, people and voices, connected
apps and limits are under {ui.a("Advanced", "/admin/advanced")}.</p></div>"""
        return await ctx.page("Coppermind", body, "home", dialog=dialog)

    @routes.post("/admin/notices/dismiss", include_in_schema=False)
    async def dismiss(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        try:
            await ctx.act("notice-dismiss", {"id": form.get("id", "")})
        except StoreError as exc:
            return await ctx.refused(exc, "/admin")
        return ctx.go(ctx.back("/admin"))

    return routes
