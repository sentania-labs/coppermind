"""Storage: how much space Coppermind uses, and the files kept beside notes."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from coppermind.settings import Wiring
from coppermind.store_protocol import StoreError
from coppermind_admin import ui
from coppermind_admin.pages.incoming import file_dialog, take_in, too_large, upload_limit
from coppermind_admin.shell import Ctx, explain
from coppermind_admin.ui import e, plural, segment
from coppermind_admin.uploads import UploadRefused, receive

LEDE = "How much space Coppermind uses, and the files kept beside your notes."
# Recordings, files beside notes, notes: the parts the store reports, in that order.
COLOURS = ("use-a", "use-b", "use-d")


def router(wiring: Wiring) -> APIRouter:
    routes = APIRouter()

    @routes.get("/admin/storage", include_in_schema=False)
    async def storage(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        try:
            data = await ctx.act("storage", {})
        except StoreError:
            return await ctx.unavailable("Storage", LEDE, "storage")
        usage = data["usage"]
        total = usage["total"]
        if total > 50_000:
            bars = "".join(
                f'<i class="{COLOURS[index % len(COLOURS)]}" style="width:'
                f'{max(1, round(part["bytes"] / total * 100))}%"></i>'
                for index, part in enumerate(usage["parts"])
                if part["bytes"]
            )
            legend = "".join(
                f'<span><i class="{COLOURS[index % len(COLOURS)]}"></i>{part["name"]}: '
                f"{ui.size(part['bytes'])}</span>"
                for index, part in enumerate(usage["parts"])
            )
            used = (
                f'<div class="status-t">{ui.size(total)} used</div><div class="bar">{bars}</div>'
                f'<div class="legend">{legend}</div>'
            )
        else:
            used = (
                '<div class="status-t">Almost nothing stored yet</div><div class="muted">This '
                "fills in once notes and recordings arrive.</div>"
            )
        files = data["files"]
        rows = ui.rows(
            ui.row(
                "Files beside your notes",
                f"{plural(files, 'file or link', 'files and links')}, such as PDFs and slides "
                "handed in with a note."
                if files
                else "None yet.",
                ui.go("See files", "/admin/storage/files"),
            ),
        )
        body = f"""{ui.head("Storage", LEDE)}<div class="stack">{ctx.flash()}{ui.card(used)}
{ui.card(rows)}</div>"""
        return await ctx.page("Storage", body, "storage")

    # -- files beside your notes ---------------------------------------------------------

    @routes.get("/admin/storage/files", include_in_schema=False)
    async def files(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        view = await ctx.view()
        lede = (
            "Documents and links handed in with a note. Each one is kept exactly as it "
            f'arrived and shows in the "{e(ctx.settings().notes.artifacts_folder)}" folder in '
            "Obsidian."
        )
        query = ctx.q("q").strip()
        shown = int(ctx.q("shown")) if ctx.q("shown").isascii() and ctx.q("shown").isdigit() else 6
        try:
            data = await ctx.act("files", {"q": query, "shown": shown})
        except StoreError:
            return await ctx.unavailable("Files beside your notes", lede, "storage")
        here = ui.url("/admin/storage/files", q=query, shown=shown if shown != 6 else "")
        items, total = data["items"], data["total"]
        rows = [
            [
                e(item["name"]),
                ui.date_only(item["when"], ctx.zone),
                "Link" if item["link"] else ui.size(item["bytes"]),
                e(", ".join(item["used_by"])) or "Not used by any note",
                ui.go("Remove", ui.url(here, ask=item["id"])),
            ]
            for item in items
        ]
        dialog = ""
        asked = ctx.q("ask")
        if asked == "add":
            dialog = file_dialog(
                "Add a file",
                "Choose a document from this computer. It is kept beside your notes and its "
                "text can be searched.",
                "/admin/storage/files/add",
                here,
            )
        elif asked:
            target = next((item for item in items if item["id"] == asked), None)
            if target:
                used = (
                    f' The note "{e(target["used_by"][0])}" will show it as removed.'
                    if target["used_by"]
                    else ""
                )
                dialog = ui.dialog(
                    f'Remove "{e(target["name"])}"?',
                    f"It leaves Obsidian and search now. Removing is final in Coppermind.{used}",
                    f"/admin/storage/files/{segment(target['id'])}/remove",
                    "Remove",
                    here,
                    danger=True,
                    fields={"back": here},
                )
        if rows:
            more = ""
            if not query:
                more = (
                    f'<div class="actions"><span class="muted">Showing the newest {len(items)} '
                    f"of {total}.</span>"
                    + (
                        ui.go("Show 10 more", ui.url("/admin/storage/files", shown=shown + 10))
                        if len(items) < total
                        else ""
                    )
                    + "</div>"
                )
            listing = ui.table(["File", "Came in", "Size", "Used by", ""], rows) + more
        else:
            folder = view["drop_folder"] if view else "To Coppermind"
            listing = ui.empty(
                "No file with that name" if query else "No files yet",
                f"The search covers all {total} files."
                if query
                else f'Files arrive with notes, through the companion or the "{e(folder)}" folder.',
            )
        find = (
            '<form method="get" action="/admin/storage/files">'
            + ui.text_input("q", "Find a file by name", query, live=True, autofocus=bool(query))
            + "</form>"
            if total
            else "<span></span>"
        )
        body = f"""{ui.head("Files beside your notes", lede, ("/admin/storage", "Storage"))}
<div class="stack">{ctx.flash()}
<div class="spread spread--end">{find}{ui.go("Add a file", ui.url(here, ask="add"))}</div>
{ui.card(listing)}</div>"""
        return await ctx.page("Files beside your notes", body, "storage", dialog=dialog)

    @routes.post("/admin/storage/files/add", include_in_schema=False)
    async def file_add(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        try:
            upload = await receive(request, lambda filename: upload_limit(ctx, filename))
        except UploadRefused as exc:
            ctx.say("bad", too_large(exc))
            return ctx.go("/admin/storage/files")
        try:
            ctx.say(*await take_in(ctx, upload))
        except StoreError as exc:
            ctx.say("bad", explain(exc, f'"{e(upload.filename)}" could not be added.'))
        finally:
            upload.discard()
        return ctx.go("/admin/storage/files")

    @routes.post("/admin/storage/files/{identity}/remove", include_in_schema=False)
    async def file_remove(identity: str, request: Request) -> Response:
        ctx = Ctx(request, wiring)
        await ctx.form()
        try:
            result = await ctx.act("file-remove", {"id": identity})
        except StoreError as exc:
            return await ctx.refused(exc, "/admin/storage/files")
        ctx.say("ok", f'Removed "{e(result["name"])}".')
        return ctx.go("/admin/storage/files")

    return routes
