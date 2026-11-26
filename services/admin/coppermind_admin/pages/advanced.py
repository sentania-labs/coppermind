"""Advanced: one list of everything that is set once, or that support may ask about.

Each row opens the page it always had. Nothing on this page is needed to use Coppermind.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from coppermind.settings import Wiring
from coppermind.store_protocol import StoreError
from coppermind_admin import ui
from coppermind_admin.pages.settings import duration, zone_name
from coppermind_admin.shell import Ctx
from coppermind_admin.ui import e

LEDE = "Things you set once, or that support may ask about. You never need these to use Coppermind."


def router(wiring: Wiring) -> APIRouter:
    routes = APIRouter()

    @routes.get("/admin/advanced", include_in_schema=False)
    async def advanced(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        settings = ctx.settings()
        auto = settings.sync.companion_auto_update
        try:
            state = (await ctx.act("transcription", {}))["state"]
        except StoreError:
            state = ""
        again = duration(settings.admin.session_hours * 3600)
        rows = [
            ui.row(
                "Password and signing in",
                f"Admin asks for the password again after {again}.",
                ui.go("Change", "/admin/settings/signin"),
            ),
            ui.row(
                "Time zone",
                e(zone_name(settings.general.timezone)),
                ui.go("Change", "/admin/settings/timezone"),
            ),
            ui.row(
                "Folders",
                f'Waiting notes go in "{e(settings.notes.review_folder)}". Reviewed notes are '
                "filed by type, year and month.",
                ui.go("Change", "/admin/settings/folders"),
            ),
            ui.row(
                "Files left alone",
                "Folders and files in your vault that Coppermind does not touch.",
                ui.go("Change", "/admin/notes/leave-alone"),
            ),
            ui.row(
                "Note properties",
                "The information a note carries, and the tags you use.",
                ui.go("Change", "/admin/settings/properties"),
            ),
            ui.row(
                "How summaries are written",
                "Your directions and the names and terms to get right.",
                ui.go("Change", "/admin/transcription/writeup"),
            ),
            ui.row(
                "Suggestions",
                "Tags, account, context and calendar match for each note.",
                ui.go("Open", "/admin/suggestions"),
            ),
            ui.row(
                "Pause transcription",
                "Transcription is <b>paused</b>. New recordings wait until you resume it."
                if state == "paused"
                else "Hold new recordings without losing them, for example while the server is "
                "being worked on.",
                ui.btn("Resume", "/admin/ai/resume", "primary", {"back": "/admin/advanced"})
                if state == "paused"
                else ui.btn("Pause", "/admin/ai/pause", "", {"back": "/admin/advanced"})
                if state in {"on", "failing"}
                else "",
            ),
            ui.row(
                "AI servers",
                "Add, test, edit or remove the servers and accounts Coppermind can use.",
                ui.go("Open", "/admin/services"),
            ),
            ui.row(
                "Search",
                "Search by meaning, and a box to try it.",
                ui.go("Open", "/admin/settings/search"),
            ),
            ui.row(
                "What Obsidian Sync carries",
                "Which kinds of file travel between Coppermind and your devices.",
                ui.go("Change", "/admin/sync/what"),
            ),
            ui.row(
                "Update the companion by itself",
                "Coppermind replaces the companion in your vault when it starts on a new "
                f"version. It is <b>{'on' if auto else 'off'}</b>.",
                ui.btn(
                    "Turn off" if auto else "Turn on",
                    "/admin/sync/companion-auto",
                    "",
                    {"on": "" if auto else "1", "back": "/admin/advanced"},
                    busy="Saving…",
                ),
            ),
            ui.row(
                "People and voices",
                "The people Coppermind knows and the voices it has saved.",
                ui.go("Open", "/admin/people"),
            ),
            ui.row(
                "Recordings and imports",
                "What was handed in, adding a file, and the largest file accepted.",
                ui.go("Open", "/admin/incoming"),
            ),
            ui.row(
                "Calendar files",
                "Files added so far, and how many meetings are kept.",
                ui.go("Open", "/admin/calendar"),
            ),
            ui.row(
                "Connected apps",
                "The keys that let another app hand things in to Coppermind.",
                ui.go("Open", "/admin/apps"),
            ),
            ui.row(
                "Space used",
                "What recordings, files and notes take up, and the files kept beside your notes.",
                ui.go("Open", "/admin/storage"),
            ),
            ui.row(
                "Limits and timing",
                "How often Coppermind looks for changes, and how long it waits for the AI.",
                ui.go("Change", "/admin/settings/advanced"),
            ),
            ui.row(
                "Details for support",
                f"Version {e(request.app.state.version)}.",
                ui.go("Open", "/admin/settings/advanced?tab=about"),
            ),
        ]
        body = f"""{ui.head("Advanced", LEDE, ("/admin", "Coppermind"))}
<div class="stack">{ctx.flash()}{ui.card(ui.rows(*rows))}</div>"""
        return await ctx.page("Advanced", body, "advanced")

    return routes
