"""Recordings and imports: everything handed to Coppermind and what happened to each."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from coppermind.settings import Wiring
from coppermind.statefiles import RevisionConflict
from coppermind.store_protocol import (
    StagedDeclaration,
    StagedReservation,
    StoreError,
)
from coppermind_admin import model, ui
from coppermind_admin.pages.common import (
    health_badge,
    service_path,
    where_text,
)
from coppermind_admin.pages.notes import keep_words
from coppermind_admin.pages.settings import folder_dialog
from coppermind_admin.shell import Ctx, explain
from coppermind_admin.ui import e, plural, segment
from coppermind_admin.uploads import Upload, UploadRefused, receive

LEDE = (
    "Everything handed to Coppermind in the last 60 days (recordings, calendar files and "
    "documents) and what happened to each."
)
STATES = {
    "failed": ("bad", "Failed"),
    "waiting": ("warn", "Waiting"),
    "working": ("accent", "Working"),
    "done": ("ok", "Done"),
}
KIND_WORDS = {"recording": "Recording", "calendar": "Calendar file", "document": "Document"}
CALENDAR_SUFFIXES = {".ics", ".zip"}
CALENDAR_LIMIT = 200_000_000
RECORDING_SUFFIXES = {".m4a", ".mp3", ".wav", ".ogg", ".opus", ".flac", ".aac"}
RECORDING_LIMITS = (
    (500_000_000, "500 MB"),
    (1024**3, "1 GB"),
    (2 * 1024**3, "2 GB"),
    (4 * 1024**3, "4 GB"),
)
FILE_LIMITS = (
    (10 * 1024**2, "10 MB"),
    (50_000_000, "50 MB"),
    (200_000_000, "200 MB"),
    (500_000_000, "500 MB"),
    (1024**3, "1 GB"),
)
ADD_BODY = (
    "Choose a recording, a calendar file or a document from this computer. Coppermind works "
    "out which it is."
)


def limit_choices(choices: tuple[tuple[int, str], ...], current: int) -> list[tuple[str, str]]:
    listed = [(str(value), label) for value, label in choices]
    if str(current) not in {value for value, _ in listed}:
        listed.append((str(current), ui.size(current)))
        listed.sort(key=lambda item: int(item[0]))
    return listed


def estimate(minutes: int | None) -> str:
    if not minutes:
        return "It will be ready within the wait you chose on the Transcription page."
    low, high = max(1, round(minutes / 5)), max(2, round(minutes / 3))
    return f"Usually ready in about {low} to {high} minutes."


def why(item: dict[str, Any], ctx: Ctx, transcription: str) -> str:
    """One sentence about what happened to an item, from what was observed."""
    if item["kind"] == "recording":
        if item["state"] == "done":
            if item["audio_removed"]:
                # The audio goes once the note is reviewed, so its absence says it was.
                return "Written up, reviewed and filed."
            told = item["speakers"]
            return (
                "Written up"
                + (f" with {plural(told, 'speaker')} told apart" if told else "")
                + ". Note placed in Review."
            )
        if item["state"] == "working":
            return "Being written up. " + estimate(item["minutes"])
        if item["state"] == "waiting":
            if transcription == "none":
                return "Kept. It will be written up once transcription is on."
            if transcription == "paused":
                return "Kept. Transcription is paused; it starts when you resume."
            if item.get("held"):
                return (
                    "Waiting for the transcription service, which is not answering. It is "
                    "sent again by itself once the service answers."
                )
            if item.get("retry_after"):
                return (
                    "Waiting for the transcription service. Next try at "
                    f"{ui.clock(ui._local(item['retry_after'], ctx.zone))}."  # noqa: SLF001
                    if ui._local(item["retry_after"], ctx.zone)  # noqa: SLF001
                    else "Waiting for the transcription service."
                )
            return "Waiting for its turn. Recordings are written up one at a time."
        return e(item.get("error") or "It could not be written up.")
    if item["kind"] == "calendar":
        if item["state"] == "done":
            return (
                f"{item['new']:,} new {'meeting' if item['new'] == 1 else 'meetings'} added to "
                "your calendar."
            )
        if item["state"] == "working":
            return "Being read now."
        # The raw reason is on the file's own page, behind Details for support.
        return "Could not be read, so nothing was added from it."
    if item["state"] == "failed":
        return e(item.get("error") or "It could not be taken in.")
    return (
        "Stored. Its text can be searched."
        if item.get("searchable")
        else "Stored beside your notes."
    )


def upload_limit(ctx: Ctx, filename: str) -> int:
    """The most Admin accepts of a file, by what its name says it is."""
    suffix = Path(filename).suffix.lower()
    limits = ctx.settings().limits
    if suffix in CALENDAR_SUFFIXES:
        return CALENDAR_LIMIT
    if suffix in RECORDING_SUFFIXES:
        return limits.staged_max_bytes
    return limits.artifact_max_bytes


def too_large(exc: UploadRefused) -> str:
    """What a person is told when a file passed the most that kind of file may be."""
    if str(exc) != "too large":
        return e(str(exc))
    return (
        f"That file is larger than {ui.size(exc.limit)}, the largest accepted for a file like "
        "it. Nothing was added."
    )


async def spool(upload: Upload) -> AsyncIterator[bytes]:
    assert upload.path is not None
    with upload.path.open("rb") as handle:
        while chunk := await asyncio.to_thread(handle.read, 1 << 20):
            yield chunk


async def take_in(ctx: Ctx, upload: Upload) -> tuple[str, str]:
    """Hand one uploaded file to the store by what it is, and say what became of it."""
    assert upload.path is not None
    name = e(upload.filename)
    if Path(upload.filename).suffix.lower() in CALENDAR_SUFFIXES:
        if upload.size > CALENDAR_LIMIT:
            return "bad", f'"{name}" is larger than 200 MB, the most a calendar file may be.'
        result = await ctx.store.add_calendar_file(upload.filename, spool(upload))
        return calendar_message(result, ctx)
    receipt = await ctx.store.reserve_staged_upload(
        StagedReservation(
            filename=upload.filename,
            size_bytes=upload.size,
            sha256=upload.sha256,
            fields=StagedDeclaration(origin="admin"),
        )
    )
    if receipt.state != "stored":
        await ctx.store.put_staged_bytes(receipt.id, spool(upload), declared_bytes=upload.size)
        receipt = await ctx.store.finalize_staged_upload(receipt.id)
    if receipt.artifact is None:
        return "bad", (
            f'"{name}" could not be stored. Try adding it again.'
            + (ui.tech(receipt.failure) if receipt.failure else "")
        )
    result = await ctx.act("incoming-accept", {"artifact_id": receipt.artifact.id})
    if result["kind"] == "audio":
        return "warn", (
            f'Added "{e(result["title"])}" as a file, but transcription is off, so it is not '
            "being written up. Turn transcription on under Transcription, then add the "
            "recording again."
        )
    if result["kind"] == "recording":
        return "ok", (
            f'Added "{e(result["title"])}". Coppermind recognised it as a recording.'
            + (
                " It is being written up now."
                if result["transcribing"]
                else " Transcription is paused, so it will be written up when you resume it."
            )
        )
    return "ok", (
        f'Added "{name}". It is in the "{e(ctx.settings().notes.artifacts_folder)}" folder in '
        "Obsidian, and its text can be searched once it has been read. No note uses it yet."
    )


def calendar_message(result: dict[str, Any], ctx: Ctx) -> tuple[str, str]:
    name = e(result["name"])
    if result["state"] == "already":
        return "info", (
            f'"{name}" was already added: it is exactly the same file as one you added '
            "before, so nothing changed."
        )
    if result["state"] == "not_calendar":
        return "bad", f'"{name}" is not a calendar file, so nothing was added from it.'
    if result["state"] == "failed":
        return "bad", (
            f'"{name}" could not be read, so nothing was added from it. The usual cause is '
            "a download that was cut short: export the calendar again and add the fresh file."
        )
    span = ""
    if result["from"] and result["to"]:
        first, last = ui.month(result["from"], ctx.zone), ui.month(result["to"], ctx.zone)
        span = f", all in {first}" if first == last else f", from {first} to {last}"
    new = result["new"]
    tail = (
        ""
        if new == result["has"]
        else f"; {new:,} of them {'is' if new == 1 else 'are'} new to Coppermind"
    )
    return "ok", (
        f'Added "{name}". It holds {plural(result["has"], "meeting")}{span}{tail}. Coppermind '
        "can now match recordings and notes to these meetings."
    )


def file_dialog(
    title: str,
    body: str,
    action: str,
    cancel: str,
    fields: dict[str, Any] | None = None,
    accept: str = "",
) -> str:
    """Ask for a file from this computer. The form token goes first, so it is checked first."""
    from coppermind_admin.shell import csrf_token

    limit = f' accept="{accept}"' if accept else ""
    return (
        f'<div class="overlay"><div class="dialog" role="dialog" aria-modal="true" '
        f'aria-label="{e(title)}"><form method="post" enctype="multipart/form-data" '
        f'action="{e(action)}"><input type="hidden" name="csrf" value="{csrf_token.get()}">'
        f'{ui.hidden(fields)}<h2>{title}</h2><p class="muted">{body}</p>'
        f'<div class="field"><label class="lat-label" for="file">File</label>'
        f'<input class="lat-input" type="file" id="file" name="file" required{limit}></div>'
        '<div class="actions actions--end">'
        f'<a class="lat-btn" href="{e(cancel)}" data-cancel>Cancel</a>'
        '<button class="lat-btn lat-btn--primary" data-busy="Adding…">Add this file</button>'
        "</div></form></div></div>"
    )


def router(wiring: Wiring) -> APIRouter:
    routes = APIRouter()

    def drop_row(ctx: Ctx, folder: str) -> str:
        limits = ctx.settings().limits
        return ui.card(
            ui.rows(
                ui.row(
                    "Folder in your vault for dropping files",
                    f'"{e(folder)}". A recording, calendar file or document put in this folder '
                    "is picked up within a minute.",
                    ui.go("Use a different folder", "/admin/incoming?ask=folder"),
                ),
                ui.row(
                    "Largest accepted",
                    "Recordings up to "
                    + ui.quick_select(
                        "/admin/incoming/limits",
                        "recording",
                        "Largest recording accepted",
                        limit_choices(RECORDING_LIMITS, limits.staged_max_bytes),
                        str(limits.staged_max_bytes),
                    )
                    + ", other files up to "
                    + ui.quick_select(
                        "/admin/incoming/limits",
                        "file",
                        "Largest other file accepted",
                        limit_choices(FILE_LIMITS, limits.artifact_max_bytes),
                        str(limits.artifact_max_bytes),
                    )
                    + ". Anything larger is refused with a message saying so.",
                ),
            )
        )

    @routes.get("/admin/incoming", include_in_schema=False)
    async def incoming(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        view = await ctx.view()
        if view is None:
            return await ctx.unavailable("Recordings and imports", LEDE, "incoming")
        try:
            items = (await ctx.act("incoming", {}))["items"]
        except StoreError:
            return await ctx.unavailable("Recordings and imports", LEDE, "incoming")
        folder = view["drop_folder"]
        transcription = view["trans"]["state"]
        tab = ctx.q("tab") or "all"
        here = ui.url("/admin/incoming", tab=tab if tab != "all" else "")
        add = ui.go("Add a file", ui.url(here, ask="add"), "" if items else "primary")
        off = (
            ui.banner(
                "info",
                "Transcription is off, so a recording you add is kept as a file, not written up. "
                + ui.go("Set up transcription", "/admin/transcription", "primary"),
            )
            if transcription == "none"
            else ""
        )
        dialog = ""
        asked = ctx.q("ask")
        if asked == "add":
            dialog = file_dialog("Add a file", ADD_BODY, "/admin/incoming/add", here)
        elif asked == "folder":
            try:
                known = await ctx.act("folders", {})
                # Folders Coppermind already uses for something else are not offered.
                taken = {name for use, name in known["current"].items() if use != "drop"}
                folders = [
                    name
                    for name in known["folders"]
                    if name not in taken and not name.startswith("_")
                ]
            except StoreError:
                folders = []
            dialog = folder_dialog(
                "Choose the folder to drop files into",
                folders,
                folder,
                action="/admin/incoming/folder",
                cancel=here,
                error=ctx.q("problem"),
            )
        elif asked.startswith(("a:", "c:", "d:")):
            target = next((item for item in items if item["id"] == asked), None)
            if target is not None:
                dialog = remove_dialog(ctx, target, here, folder)
        if not items:
            body = f"""{ui.head("Recordings and imports", LEDE)}<div class="stack">
{ctx.flash()}{off}{
                ui.card(
                    ui.empty(
                        "Nothing has come in yet",
                        "Record with the companion in Obsidian, drop a file into "
                        f'the "{e(folder)}" folder in your vault, or add one here.',
                        add,
                    )
                )
            }{drop_row(ctx, folder)}</div>"""
            return await ctx.page("Recordings and imports", body, "incoming", dialog=dialog)

        def matches(item: dict[str, Any], key: str) -> bool:
            return item["state"] == "failed" if key == "attn" else item["kind"] == key

        shown = [item for item in items if tab == "all" or matches(item, tab)]
        limit = 500 if ctx.q("all") else 8
        rows = []
        for item in shown[:limit]:
            link = model.incoming_path(item["id"])
            name = (
                ui.a(e(item["title"]), link)
                if item["kind"] == "recording" or item["state"] == "failed"
                else e(item["title"])
            )
            kind = KIND_WORDS[item["kind"]] + (
                f", {plural(item['minutes'], 'minute')}" if item.get("minutes") else ""
            )
            if item["state"] == "failed" and item["kind"] == "recording":
                action = ui.btn(
                    "Try again", f"{link}/retry", "primary", {"back": here}, busy="Trying again…"
                )
            elif item["state"] == "failed":
                action = ui.go("What to do", link, "primary")
            elif item["kind"] == "recording":
                action = ui.go("Open", link)
            elif item["kind"] == "calendar":
                action = ui.go("Open", ui.url("/admin/calendar", tab="files"))
            else:
                action = ui.go("Remove", ui.url(here, ask=item["id"]))
            rows.append(
                [
                    name,
                    kind,
                    ui.when(item["when"], ctx.zone),
                    ui.badge(*STATES[item["state"]]) + " " + why(item, ctx, transcription),
                    action,
                ]
            )
        more = (
            f'<div class="actions"><span class="muted">Showing the newest {limit} of '
            f"{len(shown)}, failures first.</span>"
            + ui.go("Show the rest", ui.url("/admin/incoming", tab=tab, all=1))
            + "</div>"
            if len(shown) > limit
            else ""
        )
        hint = (
            '<p class="lat-hint" style="margin-top:8px">Items older than 60 days drop off '
            "this list. Documents themselves stay listed under "
            + ui.a("Storage, Files beside your notes", "/admin/storage/files")
            + ", and calendar files under "
            + ui.a("Calendar", "/admin/calendar?tab=files")
            + ".</p>"
        )
        table = (
            ui.table(["What", "Kind", "Came in", "What happened", ""], rows) + more + hint
            if rows
            else ui.empty("Nothing has failed" if tab == "attn" else "Nothing of this kind", "")
        )

        def count(key: str) -> int:
            return sum(1 for item in items if matches(item, key))

        tabs = ui.seg(
            "/admin/incoming",
            [
                ("all", f"All ({len(items)})"),
                ("attn", f"Failed ({count('attn')})"),
                ("recording", f"Recordings ({count('recording')})"),
                ("calendar", f"Calendar files ({count('calendar')})"),
                ("document", f"Documents ({count('document')})"),
            ],
            tab,
        )
        body = f"""{ui.head("Recordings and imports", LEDE)}<div class="stack">{ctx.flash()}{off}
<div class="spread">{tabs}{add}</div>{ui.card(table)}{drop_row(ctx, folder)}</div>"""
        return await ctx.page("Recordings and imports", body, "incoming", dialog=dialog)

    def remove_dialog(ctx: Ctx, item: dict[str, Any], back: str, folder: str) -> str:
        days = keep_words(ctx.settings().retention.days)
        title = e(item["title"])
        if item["id"].startswith("a:"):
            text = (
                f"It leaves Obsidian and search now. It can be brought back for {days}. A note "
                "that uses it will show it as removed."
            )
        else:
            text = (
                "It was never imported, so nothing else changes. The file is removed from the "
                f'"{e(folder)}" folder in your vault.'
            )
        return ui.dialog(
            f'Remove "{title}"?',
            text,
            f"{model.incoming_path(item['id'])}/remove",
            "Remove",
            back,
            danger=True,
            fields={"back": back},
        )

    @routes.post("/admin/incoming/add", include_in_schema=False)
    async def add(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        try:
            upload = await receive(request, lambda filename: upload_limit(ctx, filename))
        except UploadRefused as exc:
            ctx.say("bad", too_large(exc))
            return ctx.go("/admin/incoming")
        target = upload.fields.get("back", "")
        target = target if target.startswith("/admin") else "/admin/incoming"
        replacing = upload.fields.get("replaces", "")
        try:
            kind, said = await take_in(ctx, upload)
            if replacing and kind in {"ok", "info"}:
                old = await ctx.act("incoming-remove", {"id": replacing})
                said += (
                    f' The damaged file "{e(old["title"])}" was removed, so there is nothing '
                    "left to fix for it."
                )
        except StoreError as exc:
            kind, said = "bad", explain(exc, f'"{e(upload.filename)}" could not be added.')
        finally:
            upload.discard()
        ctx.say(kind, said)
        return ctx.go(target)

    @routes.post("/admin/incoming/folder", include_in_schema=False)
    async def folder_save(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        picked = (
            form.get("new_folder", "").strip()
            if form.get("folder") == "__new__"
            else form.get("folder", "")
        )
        if not picked:
            return ctx.go(
                ui.url(
                    "/admin/incoming",
                    ask="folder",
                    problem="Pick a folder, or type a name for the new one.",
                )
            )
        try:
            result = await ctx.act("folders-save", {"drop": picked})
        except StoreError as exc:
            return await ctx.refused(exc, "/admin/incoming")
        if "refused" in result:
            return ctx.go(
                ui.url("/admin/incoming", ask="folder", problem=result["refused"]["message"])
            )
        if "drop" in result["changed"]:
            ctx.say(
                "ok",
                f'Saved. Coppermind now picks up files dropped into "{e(picked)}". Anything '
                f'still in "{e(result["was"]["drop"])}" is left where it is.',
            )
        return ctx.go("/admin/incoming")

    @routes.post("/admin/incoming/limits", include_in_schema=False)
    async def limits(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()

        def change(document: dict[str, Any]) -> None:
            section = document.setdefault("limits", {})
            if "recording" in form:
                section["staged_max_bytes"] = int(form["recording"])
            if "file" in form:
                section["artifact_max_bytes"] = int(form["file"])

        try:
            ctx.save_settings(change)
        except (ValueError, RevisionConflict):
            ctx.say("bad", "That limit could not be saved. Pick one from the list.")
            return ctx.go("/admin/incoming")
        if "recording" in form:
            ctx.say(
                "ok", f"Saved. Recordings up to {ui.size(int(form['recording']))} are now accepted."
            )
        else:
            ctx.say(
                "ok",
                f"Saved. Documents and calendar files up to {ui.size(int(form['file']))} are "
                "now accepted.",
            )
        return ctx.go("/admin/incoming")

    # -- one item ------------------------------------------------------------------------

    @routes.get("/admin/incoming/{identity}", include_in_schema=False)
    async def item_page(identity: str, request: Request) -> Response:
        ctx = Ctx(request, wiring)
        view = await ctx.view()
        try:
            item = await ctx.act("incoming-item", {"id": identity})
        except StoreError:
            return ctx.go("/admin/incoming")
        here = model.incoming_path(identity)
        came = ctx.q("from")
        back = {
            "home": ("/admin", "Home"),
            "calendar": ("/admin/calendar", "Calendar"),
        }.get(came, ("/admin/incoming", "Recordings and imports"))
        folder = view["drop_folder"] if view else "To Coppermind"
        transcription = view["trans"]["state"] if view else "none"
        dialog = ""
        if item["kind"] != "recording":
            if ctx.q("ask") == "new":
                dialog = file_dialog(
                    "Add the new calendar file"
                    if item["kind"] == "calendar"
                    else "Add the new file",
                    "Choose the calendar file you just exported again (its name usually ends in "
                    ".ics). It replaces the damaged one."
                    if item["kind"] == "calendar"
                    else "Choose the file again from this computer. It replaces this one.",
                    "/admin/incoming/add",
                    here,
                    {"replaces": identity, "back": back[0]},
                )
            elif ctx.q("ask") == "remove":
                dialog = remove_dialog(ctx, item, here, folder).replace(
                    f'value="{e(here)}"', f'value="{e(back[0])}"', 1
                )
            if item["kind"] == "calendar":
                first = ui.item(
                    "Export the calendar again and add the new file",
                    "The usual cause is a download that was cut short. In your calendar app, "
                    "export the calendar again (Google Calendar: Settings, then Import and "
                    "export, then Export. Outlook: File, then Save Calendar), then add the "
                    "fresh file here. It takes the place of this one.",
                    ui.go("Add the new file", ui.url(here, ask="new", **{"from": came}), "primary"),
                )
                nothing = " Nothing was added to your calendar from it."
            else:
                first = ui.item(
                    "Add the file again",
                    "If the file was still being copied when Coppermind looked at it, adding "
                    "it again is usually enough. If Coppermind does not accept this kind of "
                    "file, it says so above.",
                    ui.go("Add the new file", ui.url(here, ask="new", **{"from": came}), "primary"),
                )
                nothing = " Nothing was stored from it."
            body = f"""{
                ui.head(
                    e(item["title"]), "A file handed to Coppermind, and what happened to it.", back
                )
            }
<div class="stack">{ctx.flash()}
{
                ui.status(
                    "bad",
                    "Could not be read",
                    (
                        "Coppermind could not make sense of this file."
                        if item["kind"] == "calendar"
                        else why(item, ctx, transcription)
                    )
                    + nothing,
                )
                + (ui.tech(item["error"]) if item.get("error") else "")
            }
{
                ui.card(f'''<h3>What to do</h3>{first}
{
                    ui.item(
                        "Remove this file",
                        "It was never imported, so nothing else changes. It is removed from the "
                        f'"{e(folder)}" folder in your vault.',
                        ui.go("Remove", ui.url(here, ask="remove", **{"from": came})),
                    )
                }''')
            }</div>"""
            return await ctx.page(item["title"], body, "incoming", dialog=dialog)
        if item["state"] == "failed":
            tries = item["attempts"]
            top = ui.status(
                "bad",
                "Not written up",
                why(item, ctx, transcription)
                + f" Coppermind tried {'once' if tries == 1 else plural(tries, 'time')} and has "
                "stopped trying.",
                ui.btn(
                    "Try again", f"{here}/retry", "primary", {"back": here}, busy="Trying again…"
                )
                if transcription != "none"
                else ui.go("Set up transcription", "/admin/transcription", "primary"),
            )
        elif item["state"] == "waiting":
            top = ui.status("warn", "Waiting", why(item, ctx, transcription))
        elif item["state"] == "working":
            top = ui.status(
                "ok", "Being written up", estimate(item["minutes"]) + " You can leave this page."
            )
        else:
            note = model.note_path(item["note_id"]) if item["note_id"] else ""
            top = ui.status(
                "ok",
                "Written up",
                "The note was written from this recording. Open it to see where it is now.",
                ui.go("See the note", note) if note else "",
            )
        service = view["trans"]["service"] if view else None
        sent_to = ""
        if item["state"] == "failed" and service:
            sent_to = ui.card(
                ui.rows(
                    ui.row(
                        "The service it was sent to",
                        f"{e(service['name'])} {health_badge(service)} {where_text(service)}",
                        ui.btn(
                            "Test it now",
                            f"/admin/services/{segment(service['id'])}/test",
                            "",
                            {"back": here},
                            busy="Testing…",
                        )
                        + ui.go("Edit this service", service_path(service, **{"for": "trans"})),
                    )
                )
            )
        name = e(service["name"]) if service else "the transcription service"
        events = [
            (
                ui.when(item["when"], ctx.zone),
                "Arrived."
                + (f" {plural(item['minutes'], 'minute')} long." if item["minutes"] else ""),
            )
        ]
        if item["started_at"]:
            events.append((ui.when(item["started_at"], ctx.zone), f"Sent to {name}."))
        if item["state"] == "done" and item["done_at"]:
            speakers = item["speakers"]
            events.append(
                (
                    ui.when(item["done_at"], ctx.zone),
                    "Written up"
                    + (f" with {plural(speakers, 'speaker')} told apart" if speakers else "")
                    + ". Note placed in Review.",
                )
            )
        elif item["state"] == "failed":
            events.append(("Most recent try", e(item["error"]) + " Coppermind stopped trying."))
        if ctx.q("ask") == "delete":
            dialog = ui.dialog(
                f'Delete the recording "{e(item["title"])}"?',
                "The audio is deleted for good. A note already written from it stays.",
                f"{here}/delete",
                "Delete recording",
                here,
                danger=True,
            )
        listen = (
            f'<p><audio controls preload="none" src="{here}/audio"></audio> '
            '<span class="lat-hint">The recording as it was handed in.</span></p>'
            if item.get("has_audio")
            else ""
        )
        delete = (
            ""
            if item["audio_removed"]
            else f"<p>{ui.go('Delete this recording…', ui.url(here, ask='delete'), 'ghost')}</p>"
        )
        body = f"""{ui.head(e(item["title"]), "A recording, and what has happened to it.", back)}
<div class="stack">{ctx.flash()}{top}{sent_to}
{ui.card(ui.timeline(events), "What happened")}{listen}{delete}</div>"""
        return await ctx.page(item["title"], body, "incoming", dialog=dialog)

    @routes.post("/admin/incoming/{identity}/retry", include_in_schema=False)
    async def retry(identity: str, request: Request) -> Response:
        ctx = Ctx(request, wiring)
        await ctx.form()
        target = ctx.back(model.incoming_path(identity))
        try:
            result = await ctx.act("incoming-retry", {"id": identity})
        except StoreError as exc:
            ctx.say("bad", explain(exc, "That recording could not be tried again."))
            return ctx.go(target)
        ctx.say(
            "ok",
            f'Trying "{e(result["title"])}" again. It is back in the queue; when it has been '
            "written up, the note appears in Review.",
        )
        return ctx.go(target)

    @routes.post("/admin/incoming/{identity}/delete", include_in_schema=False)
    async def delete(identity: str, request: Request) -> Response:
        ctx = Ctx(request, wiring)
        await ctx.form()
        try:
            result = await ctx.act("recording-delete", {"id": identity})
        except StoreError as exc:
            return await ctx.refused(exc, model.incoming_path(identity))
        ctx.say("ok", f'Deleted the recording "{e(result["title"])}".')
        return ctx.go("/admin/incoming")

    @routes.post("/admin/incoming/{identity}/remove", include_in_schema=False)
    async def remove(identity: str, request: Request) -> Response:
        ctx = Ctx(request, wiring)
        await ctx.form()
        target = ctx.back("/admin/incoming")
        if target.startswith("/admin/incoming/"):
            target = "/admin/incoming"
        days = keep_words(ctx.settings().retention.days)
        try:
            result = await ctx.act("incoming-remove", {"id": identity})
        except StoreError as exc:
            return await ctx.refused(exc, target)
        ctx.say(
            "ok",
            f'Removed "{e(result["title"])}". It can be brought back for {days}.'
            if result["kind"] == "document"
            else f'Removed "{e(result["title"])}". Nothing had been added from it, so nothing '
            "else changed.",
        )
        return ctx.go(target)

    @routes.get("/admin/incoming/{identity}/audio", include_in_schema=False)
    async def audio(identity: str, request: Request) -> Response:
        """The recording's audio, while it is still kept, for the player on its page."""
        ctx = Ctx(request, wiring)
        try:
            item = await ctx.act("incoming-item", {"id": identity})
            if not item.get("has_audio"):
                return Response(status_code=404)
            from coppermind_admin.pages.people import stream_artifact

            return await stream_artifact(ctx, item["artifact_id"])
        except (StoreError, KeyError):
            return Response(status_code=404)

    return routes
