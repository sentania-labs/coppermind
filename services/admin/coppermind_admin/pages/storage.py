"""Storage: what is kept, how long deleted things stay recoverable, and cleaning up."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from coppermind.settings import Wiring
from coppermind.store_protocol import StoreError
from coppermind_admin import ui
from coppermind_admin.pages.incoming import file_dialog, take_in, too_large, upload_limit
from coppermind_admin.pages.notes import deleted_row, gone_dialog, keep_words
from coppermind_admin.shell import Ctx, explain
from coppermind_admin.ui import e, plural, segment
from coppermind_admin.uploads import UploadRefused, receive

LEDE = "What Coppermind is keeping, how long deleted things stay recoverable, and cleaning up."
KEEP = (("7", "7 days"), ("30", "30 days"), ("90", "90 days"), ("365", "1 year"))
COLOURS = ("use-a", "use-b", "use-c", "use-d")


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
        days = data["keep_days"]
        keep = keep_words(days)
        if total > 50_000:
            bars = "".join(
                f'<i class="{COLOURS[index]}" style="width:'
                f'{max(1, round(part["bytes"] / total * 100))}%"></i>'
                for index, part in enumerate(usage["parts"])
                if part["bytes"]
            )
            legend = "".join(
                f'<span><i class="{COLOURS[index]}"></i>{part["name"]}: '
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
        count = data["cleanup"]
        interrupted = data["interrupted"]
        if interrupted:
            clean = (
                "The last clean-up was interrupted. "
                f"{plural(interrupted['left'], 'item is', 'items are')} left."
            )
            clean_action = ui.go("Finish or cancel", "/admin/storage/cleanup")
        elif count:
            clean = (
                f"{plural(count, 'deleted item is', 'deleted items are')} past the {keep} keep "
                "time. Review the list to see how much space deleting them for good frees."
            )
            clean_action = ui.go("Review and clean up", "/admin/storage/cleanup")
        else:
            clean = f"Nothing to clean up. Nothing deleted is older than {keep}."
            clean_action = ui.go("See what is kept", "/admin/storage/cleanup")
        deleted, files = data["deleted"], data["files"]
        rows = ui.rows(
            ui.row(
                "Keep deleted things for",
                ui.quick_select(
                    "/admin/storage/keep",
                    "days",
                    "Keep deleted things for",
                    [
                        *KEEP,
                        *([] if str(days) in dict(KEEP) else [(str(days), plural(days, "day"))]),
                    ],
                    str(days),
                )
                + ' <span class="lat-hint">Deleted notes and removed files can be brought back '
                "for this long. After that they are listed under Clean up, where you delete "
                "them for good or bring them back. Changing this deletes nothing.</span>",
            ),
            ui.row("Clean up", clean, clean_action),
            ui.row(
                "Bring back a deleted note",
                f"{plural(deleted, 'deleted note')} can still be brought back."
                if deleted
                else "Nothing has been deleted.",
                ui.go("See deleted notes", "/admin/storage/deleted") if deleted else "",
            ),
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

    @routes.post("/admin/storage/keep", include_in_schema=False)
    async def keep(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        try:
            result = await ctx.act("storage-keep", {"days": int(form.get("days") or 0)})
        except (StoreError, ValueError) as exc:
            return await ctx.refused(exc, "/admin/storage")
        count = result["count"]
        ctx.say(
            "ok",
            f"Saved. Deleted notes and removed files are now kept for "
            f"{keep_words(result['days'])}. "
            + (
                f"{plural(count, 'item is', 'items are')} older than that and "
                f"{'waits' if count == 1 else 'wait'} under Clean up."
                if count
                else "Nothing deleted is older than that, so there is nothing to clean up."
            ),
        )
        return ctx.go("/admin/storage")

    # -- deleted notes -------------------------------------------------------------------

    @routes.get("/admin/storage/deleted", include_in_schema=False)
    async def deleted(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        days = ctx.settings().retention.days
        here = "/admin/storage/deleted"
        lede = (
            f"Notes you deleted. Each can be brought back for {keep_words(days)}, and after "
            "that until you clean up."
        )
        try:
            notes = (await ctx.act("notes-deleted", {}))["items"]
        except StoreError:
            return await ctx.unavailable("Deleted notes", lede, "storage")
        inner = "".join(deleted_row(note, days, ctx, here) for note in notes) or ui.empty(
            "No deleted notes",
            "Nothing is waiting to be brought back.",
            ui.go("Back to Storage", "/admin/storage"),
        )
        body = f"""{ui.head("Deleted notes", lede, ("/admin/storage", "Storage"))}
<div class="stack">{ctx.flash()}{ui.card(inner)}</div>"""
        return await ctx.page(
            "Deleted notes", body, "storage", dialog=gone_dialog(notes, ctx.q("gone"), here)
        )

    # -- clean up ------------------------------------------------------------------------

    @routes.get("/admin/storage/cleanup", include_in_schema=False)
    async def cleanup(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        keep = keep_words(ctx.settings().retention.days)
        lede = (
            f"Things deleted more than {keep} ago. Deleting them here is permanent and frees "
            "the space."
        )
        try:
            data = await ctx.act("cleanup", {})
        except StoreError as exc:
            ctx.say("bad", explain(exc, "The clean-up list could not be made just now."))
            return ctx.go("/admin/storage")
        items, interrupted = data["items"], data["interrupted"]
        count = len(items)
        here = "/admin/storage/cleanup"

        def back_button(item: dict[str, Any]) -> str:
            if not item["can_restore"] or interrupted:
                return ""
            return ui.btn(
                "Bring back",
                "/admin/storage/cleanup/bring-back",
                "",
                {"kind": item["kind"], "id": item["id"]},
                busy="Bringing back…",
            )

        rows = [
            [
                e(item["title"]),
                item["what"],
                ui.date_only(item["deleted_at"], ctx.zone) if item["deleted_at"] else "",
                ui.size(item["bytes"]) if item["bytes"] else "",
                back_button(item),
            ]
            for item in items
        ]
        warning = (
            ui.banner(
                "warn",
                f"<b>The last clean-up was interrupted.</b> It had deleted {interrupted['done']} "
                f"of {plural(interrupted['total'], 'item')} when it stopped. The "
                f"{plural(count, 'item')} below {'is' if count == 1 else 'are'} what is left, "
                "untouched.",
            )
            if interrupted and count
            else ""
        )
        dialog = ""
        if ctx.q("ask") == "clean" and count:
            dialog = ui.dialog(
                f"Delete {plural(count, 'item')} for good?",
                (
                    "This finishes the interrupted clean-up"
                    if interrupted
                    else "This deletes everything in the list"
                )
                + f" and frees {ui.size(data['bytes'])}. They cannot be brought back afterwards.",
                "/admin/storage/cleanup/confirm",
                "Delete for good",
                here,
                danger=True,
                fields={"token": data["token"]},
                busy="Deleting…",
            )
        if count:
            buttons = (
                ui.go(
                    f"Finish: delete {'this 1' if count == 1 else f'these {count}'} for good",
                    ui.url(here, ask="clean"),
                    "danger",
                )
                + ui.btn(
                    f"Cancel and keep {'this 1' if count == 1 else f'these {count}'}",
                    "/admin/storage/cleanup/cancel",
                )
                if interrupted
                else ui.go(
                    f"Delete {'this 1 item' if count == 1 else f'these {count} items'} for good",
                    ui.url(here, ask="clean"),
                    "danger",
                )
                + ui.go("Not now", "/admin/storage")
            )
            listing = ui.card(
                ui.table(["What", "Kind", "Deleted", "Size", ""], rows)
                + '<p class="muted" style="margin-top:10px">Bring back anything you still '
                "want first. Notes you still have, and anything they use, are never in this "
                f'list.</p><div class="actions">{buttons}</div>'
            )
        else:
            listing = ui.card(
                ui.empty(
                    "Nothing to clean up",
                    f"Nothing deleted is older than {keep}.",
                    ui.go("Back to Storage", "/admin/storage"),
                )
            )
        kept = data.get("kept", [])
        still = ""
        if kept:
            still = ui.card(
                f'<p class="muted">Removed less than {keep} ago, so not yet in the list above. '
                "Each can still be brought back.</p>"
                + "".join(
                    ui.item(
                        e(item["title"]),
                        f"{item['what']}. Removed {ui.date_only(item['deleted_at'], ctx.zone)}.",
                        ui.btn(
                            "Bring back",
                            "/admin/storage/cleanup/bring-back",
                            "",
                            {"kind": item["kind"], "id": item["id"]},
                            busy="Bringing back…",
                        ),
                    )
                    for item in kept
                ),
                "Removed files and people, still kept",
            )
        body = f"""{ui.head("Clean up", lede, ("/admin/storage", "Storage"))}
<div class="stack">{ctx.flash()}{warning}{listing}{still}</div>"""
        return await ctx.page("Clean up", body, "storage", dialog=dialog)

    @routes.post("/admin/storage/cleanup/confirm", include_in_schema=False)
    async def confirm(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        here = "/admin/storage/cleanup"
        try:
            result = await ctx.act("cleanup-confirm", {"token": form.get("token", "")})
        except StoreError as exc:
            ctx.say(
                "bad",
                explain(
                    exc,
                    "The clean-up did not finish.",
                    "What was not deleted is still listed below.",
                ),
            )
            return ctx.go(here)
        if result["changed"]:
            ctx.say(
                "warn",
                "The list changed while you were looking at it, so nothing was deleted. Check "
                "the list below and press the button again.",
            )
            return ctx.go(here)
        count = result["count"]
        lead = (
            f"Clean-up finished. The remaining {plural(count, 'item was', 'items were')} "
            "deleted for good"
            if result["was_interrupted"]
            else f"Deleted {plural(count, 'item')} for good"
        )
        freed = f" and {ui.size(result['bytes'])} freed" if result["bytes"] else ""
        ctx.say("ok", f"{lead}{freed}. Storage now shows {ui.size(result['used'])} used.")
        return ctx.go(here)

    @routes.post("/admin/storage/cleanup/cancel", include_in_schema=False)
    async def cancel(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        await ctx.form()
        try:
            result = await ctx.act("cleanup-cancel", {})
        except StoreError as exc:
            return await ctx.refused(exc, "/admin/storage/cleanup")
        left = result["left"]
        ctx.say(
            "ok",
            f"Clean-up cancelled. The {plural(left, 'remaining item was', 'remaining items were')} "
            f"kept and {'stays' if left == 1 else 'stay'} in the list below. Nothing more was "
            "deleted.",
        )
        return ctx.go("/admin/storage/cleanup")

    @routes.post("/admin/storage/cleanup/bring-back", include_in_schema=False)
    async def bring_back(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        try:
            result = await ctx.act(
                "cleanup-bring-back", {"kind": form.get("kind", ""), "id": form.get("id", "")}
            )
        except StoreError as exc:
            ctx.say("bad", explain(exc, "That could not be brought back."))
            return ctx.go("/admin/storage/cleanup")
        where = {
            "note": "It is back in Obsidian and is off this list.",
            "file": "It is back with your files and is off this list.",
            "person": "Their page is back in Obsidian and they are off this list.",
        }[result["kind"]]
        ctx.say("ok", f'Brought back "{e(result["title"])}". {where}')
        return ctx.go("/admin/storage/cleanup")

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
        shown = int(ctx.q("shown") or 6)
        try:
            data = await ctx.act("files", {"q": query, "shown": shown})
        except StoreError:
            return await ctx.unavailable("Files beside your notes", lede, "storage")
        keep = keep_words(ctx.settings().retention.days)
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
                    f"It leaves Obsidian and search now. It can be brought back for {keep}.{used}",
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
        keep = keep_words(ctx.settings().retention.days)
        try:
            result = await ctx.act("file-remove", {"id": identity})
        except StoreError as exc:
            return await ctx.refused(exc, "/admin/storage/files")
        ctx.say(
            "ok",
            f'Removed "{e(result["name"])}". It can be brought back for {keep} under Storage, '
            "Clean up.",
        )
        return ctx.go("/admin/storage/files")

    return routes
