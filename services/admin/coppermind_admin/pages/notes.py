"""Notes: find a note, see why one is stuck, and fix it.

Notes live in Obsidian. This part of Admin says where a note is and deals with
the ones Coppermind could not handle by itself. Nothing is done to a note
unless the button for it was pressed.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from coppermind.settings import Wiring
from coppermind.statefiles import RevisionConflict
from coppermind.store_protocol import StoreError
from coppermind_admin import model, ui
from coppermind_admin.pages.common import folder_words, obsidian
from coppermind_admin.pages.settings import duration, folder_dialog
from coppermind_admin.shell import Ctx, explain, not_saved
from coppermind_admin.ui import e, plural

LEDE = (
    "Notes live in Obsidian. This page is for finding out where a note is and fixing one "
    "that is stuck."
)
LABELS = {
    "notfiled": ("bad", "Needs you"),
    "waiting": ("accent", "Waiting for review"),
    "reviewed": ("accent", "Reviewed, being filed"),
    "missing": ("bad", "Missing"),
    "filed": ("ok", "Filed"),
}
TEMPLATE_FOLDERS = ("Templates", "_Templates")
PATTERNS = (
    ("drawings", "Drawings", "*.excalidraw.md"),
    ("canvases", "Canvases", "*.canvas"),
    ("boards", "Boards", "*.kanban.md"),
)
# What Admin says wherever it deletes a file: Coppermind keeps no copy of its own.
FINAL = "Deleting is final in Coppermind; your vault's Git history holds earlier versions."
ALONE_LEDE = (
    "Coppermind never treats these as notes: it does not file them, change them or add suggestions."
)


def alone_why(reason: str) -> str:
    """Why a file is left alone, from the rule the last scan recorded for it."""
    if reason.startswith("folder: "):
        return f"It is in the {e(reason[8:].rstrip('/'))} folder."
    lowered = reason.lower()
    if "excalidraw" in lowered:
        return "It is a drawing, not a note."
    if "canvas" in lowered:
        return "It is a canvas, not a note."
    if "kanban" in lowered:
        return "It is a board, not a note."
    if reason == "sync test note":
        return "It is the test note Admin sent to check sync."
    if reason.startswith("pattern: "):
        return f'Its name matches "{e(reason[9:])}", which you chose to leave alone.'
    return e(reason)


def summary_card(summary: dict[str, Any], here: str) -> tuple[str, str, int]:
    """The meeting summary of a note written from a recording: the card, a notice, a refresh."""
    if not summary.get("recording"):
        return "", "", 0
    state, service = summary["state"], summary.get("service")
    again = ""
    notice = ""
    if state == "on":
        again = ui.btn("Write the summary again", f"{here}/summary-again", "", busy="Starting…")
        how = (
            "Writing it again uses the transcript Coppermind already has (the recording is "
            "not sent anywhere again) and the directions as they are now. "
            + ui.a("Change the directions", "/admin/suggestions/summary")
        )
    elif state == "failing" and service:
        how = (
            f"It cannot be written again right now, because {e(service['name'])} is not answering."
        )
        notice = ui.banner(
            "bad",
            f"<b>{e(service['name'])} is not answering</b>, so a summary cannot be written "
            "right now. Start it again and test it, or edit it if its address has changed. "
            + ui.btn(
                "Test now",
                f"/admin/services/{ui.segment(service['id'])}/test",
                "",
                {"back": here},
                busy="Testing…",
            )
            + ui.go(
                "Edit this service",
                ui.url(f"/admin/services/{ui.segment(service['id'])}", back=here),
            ),
        )
    else:
        how = (
            "It cannot be written again right now, because suggestions are "
            f"{'paused' if state == 'paused' else 'off'}. "
            + ui.a("Open Suggestions", "/admin/suggestions")
        )
    if summary["writing"]:
        body = (
            '<p><span class="lat-badge lat-badge--accent">Writing</span> Coppermind is writing '
            "the summary now. It usually takes a minute or two, and this page updates by "
            "itself.</p>"
        )
        return ui.card(body, "The meeting summary"), notice, 8
    said = "The summary could not be written. " if summary["failed"] else ""
    body = f"<p>{said}{how}</p>" + (f'<div class="actions">{again}</div>' if again else "")
    return ui.card(body, "The meeting summary"), notice, 0


def history_line(entry: dict[str, Any], here: str) -> str:
    """One line of a note's history: a plain sentence, with any raw text kept for support."""
    line = e(entry["text"])
    if entry.get("summary_again"):
        line += " To have Coppermind try once more, press Write the summary again on this page."
    if entry.get("plain"):
        line += f"<div>{e(entry['plain'])}</div>"
    if entry.get("answer"):
        line += ui.reveal(
            "Show the model's answer",
            f'<div class="tech" style="white-space:pre-wrap;overflow-wrap:anywhere">'
            f"{e(entry['answer'])}</div>",
        )
    if entry.get("get_suggestions"):
        line += (
            "<div>To try again, press Get suggestions. Only the blanks are filled.</div>"
            + ui.btn(
                "Get suggestions",
                "/admin/suggestions/older",
                "",
                {"note": entry["note_id"], "back": here},
                busy="Asking…",
            )
        )
    if entry.get("details"):
        line += ui.tech(entry["details"])
    return line


def router(wiring: Wiring) -> APIRouter:
    routes = APIRouter()

    # -- the list ------------------------------------------------------------------------

    @routes.get("/admin/notes", include_in_schema=False)
    async def notes(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        view = await ctx.view()
        if view is None:
            return await ctx.unavailable("Notes", LEDE, "notes")
        query = ctx.q("q").strip()
        try:
            waiting = await ctx.act("notes-waiting", {})
            alone = (await ctx.act("notes-alone", {}))["items"]
            found = (await ctx.act("notes-find", {"q": query}))["items"] if query else []
        except StoreError:
            return await ctx.unavailable("Notes", LEDE, "notes")
        waiting = waiting["items"]
        stuck = view["notes"]["stuck"]
        need = len(stuck)
        if not (waiting or alone or stuck) and not query:
            connect = (
                ui.go("Connect Obsidian Sync", "/admin/sync", "primary")
                if model.sync_state(view["sync"]) == "none"
                else ""
            )
            body = ui.head("Notes", LEDE) + ui.card(
                ui.empty(
                    "No notes yet",
                    "Notes show up here once Coppermind has received something: a recording, "
                    "or a note you wrote in Obsidian.",
                    connect,
                )
            )
            return await ctx.page("Notes", body, "notes")
        default = "notfiled" if need else "waiting"
        tab = ctx.q("tab") or default
        if tab not in {"notfiled", "waiting", "alone"}:
            tab = default
        here = ui.url("/admin/notes", tab=tab)
        sugg_on = view["sugg"]["state"] == "on"

        def waiting_row(note: dict[str, Any], prefix: str = "") -> str:
            link = model.note_path(note["id"])
            title = prefix + ui.a(e(note["title"]), link)
            detail = (
                f"Arrived {ui.when(note['arrived'], ctx.zone)}. Waiting for you to check it "
                "in Obsidian."
            )
            missing = note["no_suggestions"]
            if missing and view["sugg"]["state"] == "none":
                detail += " No suggestions: suggestions are off."
            elif missing == "before":
                detail += " No suggestions yet: it arrived before suggestions were turned on."
            elif missing == "failed":
                detail += " No suggestions: the model's answer could not be used."
            elif missing == "toolong":
                detail += " No suggestions: it was too long for this model."
            elif missing == "soon" and sugg_on:
                detail += " Suggestions are being written."
            actions = ""
            if missing in {"before", "failed", "toolong"} and sugg_on:
                actions += ui.btn(
                    "Get suggestions",
                    "/admin/suggestions/older",
                    "",
                    {"note": note["id"], "back": here},
                    busy="Asking…",
                )
            return ui.item(title, detail, actions + obsidian(view, note["path"]))

        def stuck_row(note: dict[str, Any], prefix: str = "") -> str:
            link = model.note_path(note["id"]) if note["id"] else ""
            title = prefix + (ui.a(e(note["title"]), link) if link else e(note["title"]))
            if note["kind"] == "copy":
                return ui.item(
                    title,
                    "This note has a copy, and Coppermind needs you to say what to do with "
                    "it. Both files are untouched.",
                    ui.go("Compare", link, "primary"),
                )
            if note["kind"] == "notfiled":
                detail = "You marked it reviewed, but it could not be filed. " + e(note["reason"])
                return ui.item(title, detail, ui.go("Fix it", link, "primary"))
            if note["kind"] == "missing":
                return ui.item(
                    title,
                    "Its file is no longer where it was, and Coppermind has no record of it "
                    "being deleted.",
                    ui.go("See what to do", link, "primary"),
                )
            if note["kind"] == "unreadable":
                return ui.item(
                    title,
                    "Coppermind found the file but could not read it as a note.",
                    ui.go("See why", link, "primary"),
                )
            fix = (
                ui.go("Open Note properties", "/admin/settings/properties")
                if note.get("action")
                else ""
            )
            return ui.item(
                title,
                f"In {folder_words(str(note['path']).rsplit('/', 1)[0]) or 'your vault'}. "
                "Coppermind could not take it on as a note: "
                + e(note["reason"])
                + " Fix it in Obsidian; Coppermind checks again by itself.",
                fix + obsidian(view, note["path"]),
            )

        if query:
            by_id = {note["id"]: note for note in waiting}
            lines = ""
            for note in found:
                badge = ui.badge(*LABELS[note["state"]]) + " "
                link = model.note_path(note["id"])
                if note["state"] == "waiting" and note["id"] in by_id:
                    lines += waiting_row(by_id[note["id"]], badge)
                else:
                    where = folder_words(note["path"].rsplit("/", 1)[0])
                    name = e(note["path"].rsplit("/", 1)[-1].removesuffix(".md"))
                    lines += ui.item(
                        badge + ui.a(e(note["title"]), link),
                        f'In {where}, as "{name}".',
                        obsidian(view, note["path"]),
                    )
            inner = (
                f'<p class="muted">{plural(len(found), "note matches", "notes match")} '
                f'"{e(query)}".</p>'
                + (
                    lines
                    or ui.empty(
                        "No note with that name",
                        "Check the spelling. The search covers every note Coppermind knows, "
                        "including filed ones.",
                    )
                )
            )
            tabs = ""
        else:
            intro = {
                "notfiled": "Notes Coppermind could not deal with by itself. Each one needs a "
                "choice from you.",
                "waiting": "These are waiting for you in Obsidian. Open one, check it and "
                "tick Reviewed. Coppermind then files it.",
                "alone": "Coppermind leaves these files untouched on purpose. That is not a "
                "problem. " + ui.a("Change which files are left alone", "/admin/notes/leave-alone"),
            }[tab]
            if tab == "notfiled":
                lines = "".join(stuck_row(n) for n in stuck)
                none = ui.empty("Nothing needs you", "Every note you reviewed has been filed.")
            elif tab == "waiting":
                lines = "".join(waiting_row(n) for n in waiting[:100])
                if len(waiting) > 100:
                    lines += (
                        f'<p class="lat-hint">Showing the newest 100 of {len(waiting)}. Use '
                        "the box above to find an older one by name.</p>"
                    )
                none = ui.empty("Nothing is waiting for review", "")
            else:
                lines = "".join(
                    ui.item(e(item["title"]), alone_why(item["why"])) for item in alone[:100]
                )
                if len(alone) > 100:
                    lines += f'<p class="lat-hint">Showing 100 of {len(alone)}.</p>'
                none = ui.empty("Nothing is left alone", "")
            inner = f'<p class="muted">{intro}</p>' + (lines or none)
            tabs = ui.seg(
                "/admin/notes",
                [
                    ("notfiled", f"Need you ({need})"),
                    ("waiting", f"Waiting for review ({len(waiting)})"),
                    ("alone", f"Left alone on purpose ({len(alone)})"),
                ],
                tab,
            )
        body = f"""{ui.head("Notes", LEDE)}<div class="stack">{ctx.flash()}
<form method="get" action="/admin/notes">
{
            ui.find_box(
                "q",
                "Find any note by name",
                query,
                "Find",
                hint="Searches every note, including the ones already filed.",
            )
        }</form>
{tabs}{ui.card(inner)}</div>"""
        return await ctx.page("Notes", body, "notes")

    @routes.post("/admin/notes/restore", include_in_schema=False)
    async def restore(request: Request) -> Response:
        """Bring back a missing note from the last version Coppermind saved of it."""
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        target = ctx.back("/admin/notes")
        try:
            result = await ctx.act("note-restore", {"id": form.get("id", "")})
        except StoreError as exc:
            ctx.say("bad", explain(exc, "That note could not be brought back just now."))
            return ctx.go(target)
        where = folder_words(str(result["path"]).rsplit("/", 1)[0]) or "your vault"
        ctx.say("ok", f'Brought back "{e(result["title"])}". It is in {where} in Obsidian again.')
        return ctx.go(target)

    # -- files left alone ----------------------------------------------------------------

    def alone_draft(ctx: Ctx) -> tuple[dict[str, bool], list[str]]:
        settings = ctx.settings().reconcile
        params = ctx.request.query_params
        if "d" in params:
            ticks = {name: name in params for name in ("templates", *(p[0] for p in PATTERNS))}
            folders = params.getlist("folder")
        else:
            ticks = {
                "templates": any(name in settings.excluded_folders for name in TEMPLATE_FOLDERS)
            }
            for name, _, pattern in PATTERNS:
                ticks[name] = pattern in settings.excluded_patterns
            folders = [f for f in settings.excluded_folders if f not in TEMPLATE_FOLDERS]
        if params.get("set") == "add":
            picked = (
                params.get("new_folder", "").strip()
                if params.get("picked") == "__new__"
                else params.get("picked", "")
            )
            if picked and picked not in folders:
                folders.append(picked)
        if params.get("drop") in folders:
            folders.remove(params.get("drop", ""))
        return ticks, folders

    @routes.get("/admin/notes/leave-alone", include_in_schema=False)
    async def leave_alone(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        ticks, folders = alone_draft(ctx)
        carry = ui.hidden({"d": 1}) + "".join(ui.hidden({"folder": f}) for f in folders)
        boxes = ui.check(
            "templates", "Anything in the Templates folder", checked=ticks["templates"]
        )
        boxes += "".join(ui.check(name, label, checked=ticks[name]) for name, label, _ in PATTERNS)
        chips = (
            "".join(
                f'<span class="chip">{e(folder)}<button name="drop" value="{e(folder)}" '
                f'aria-label="Stop leaving {e(folder)} alone">&times;</button></span>'
                for folder in folders
            )
            or '<span class="muted">None.</span>'
        )
        hint = (
            "This is separate from sync: a folder left alone here still syncs to your "
            "devices. To keep a folder off your devices, use Obsidian Sync, What syncs."
        )
        form = f"""<form method="get" action="/admin/notes/leave-alone" id="draft">{carry}
<div class="checks">{boxes}</div>
<div class="field field--wide" style="margin-top:10px"><span class="lat-label">Other folders to
leave alone</span><div class="chips">{chips}
<button class="lat-btn" name="pick" value="1">Choose a folder…</button></div>
<div class="lat-hint">{hint}</div></div>
<div class="actions">
<button class="lat-btn lat-btn--primary" formmethod="post" formaction="/admin/notes/leave-alone"
name="csrf" value="{{csrf}}">Save</button>
{ui.go("Cancel", "/admin/notes?tab=alone")}</div></form>"""
        from coppermind_admin.shell import csrf_token

        form = form.replace("{csrf}", csrf_token.get())
        dialog = ""
        if ctx.q("pick"):
            try:
                available = (await ctx.act("folders", {}))["folders"]
            except StoreError:
                available = []
            ticked = "".join(ui.hidden({name: "on"}) for name, on in ticks.items() if on)
            dialog = folder_dialog(
                "Choose a folder for Coppermind to leave alone",
                [name for name in available if name not in folders],
                "",
                action="/admin/notes/leave-alone",
                method="get",
                extra=carry + ticked + ui.hidden({"set": "add"}),
                cancel=ui.url("/admin/notes/leave-alone"),
                field="picked",
            )
        body = f"""{ui.head("Files Coppermind leaves alone", ALONE_LEDE, ("/admin/notes", "Notes"))}
<div class="stack">{ctx.flash()}{ui.card(form)}</div>"""
        return await ctx.page("Files left alone", body, "notes", dialog=dialog)

    @routes.post("/admin/notes/leave-alone", include_in_schema=False)
    async def leave_alone_save(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        from urllib.parse import parse_qs

        fields = parse_qs((await request.body()).decode(errors="replace"), keep_blank_values=True)
        chosen = [folder for folder in fields.get("folder", []) if folder.strip()]

        def change(document: dict[str, Any]) -> None:
            reconcile = document.setdefault("reconcile", {})
            current = ctx.settings().reconcile
            templates = list(TEMPLATE_FOLDERS) if "templates" in fields else []
            reconcile["excluded_folders"] = templates + [
                folder for folder in chosen if folder not in TEMPLATE_FOLDERS
            ]
            known = {pattern for _, _, pattern in PATTERNS}
            patterns = [p for p in current.excluded_patterns if p not in known]
            patterns += [pattern for name, _, pattern in PATTERNS if name in fields]
            reconcile["excluded_patterns"] = patterns

        try:
            ctx.save_settings(change)
        except (ValueError, RevisionConflict) as error:
            ctx.say(
                "bad",
                not_saved(error, "That could not be saved. Check the folder names and try again."),
            )
            return ctx.go("/admin/notes/leave-alone")
        every = duration(ctx.settings().reconcile.scan_interval_s)
        ctx.say(
            "ok",
            f"Saved. Coppermind checks the vault again within {every} and updates the Left "
            "alone list.",
        )
        return ctx.go("/admin/notes?tab=alone")

    # -- one note ------------------------------------------------------------------------

    def file_card(view: Any, label: str, facts: dict[str, Any], ctx: Ctx, extra: str = "") -> str:
        where = folder_words(facts["folder"]) or "the top of your vault"
        return (
            f'<div class="cmp"><div class="k">{label}</div><b>{e(facts["name"])}</b>'
            f'<div class="muted">In {where}. Edited {ui.when(facts["edited"], ctx.zone)}. '
            f"{plural(facts['words'], 'word')}.{extra}</div>"
            f'<div class="actions">{obsidian(view, facts["path"])}</div></div>'
        )

    @routes.get("/admin/notes/{identity}", include_in_schema=False)
    async def note(identity: str, request: Request) -> Response:
        ctx = Ctx(request, wiring)
        view = await ctx.view()
        try:
            data = await ctx.act("note", {"id": identity})
        except StoreError:
            return ctx.go("/admin/notes")
        here = model.note_path(identity)
        back = ("/admin", "Home") if ctx.q("from") == "home" else ("/admin/notes", "Notes")
        head = ui.head(e(data["title"]), "A note, and what has happened to it.", back)
        history = ui.card(
            ui.timeline(
                [
                    (ui.when(entry["at"], ctx.zone), history_line(entry, here))
                    for entry in data["history"]
                ]
            ),
            "What happened",
        )
        dialog = ""
        stuck = data.get("stuck")
        if stuck == "copy":
            original, copy, diff = data["original"], data["copy"], data["difference"]
            if diff["same"]:
                differs = "The two files hold the same text. They differ only in their properties."
            else:
                lines = []
                if diff["only_copy"]:
                    lines.append(
                        "Only in the copy: "
                        + " ".join(f"<mark>{e(line)}</mark>" for line in diff["only_copy"])
                    )
                if diff["only_original"]:
                    lines.append(
                        "Only in the original: "
                        + " ".join(f"<mark>{e(line)}</mark>" for line in diff["only_original"])
                    )
                if diff["more"]:
                    lines.append(f"And {plural(diff['more'], 'more line')} that differ.")
                differs = "<br>".join(lines) + "<br>Everything else is word for word the same."
            filed = data["original_filed"]
            choose = (
                ui.item(
                    "Keep both, as two separate notes",
                    "The original stays "
                    + ("filed " if filed else "")
                    + "where it is. The copy becomes a note of its own and waits in Review "
                    "for you, like any new note. Nothing is lost. Pick this if you are not "
                    "sure.",
                    ui.btn(
                        "Keep both", f"{here}/copy", "primary", {"choice": "both"}, busy="Working…"
                    ),
                )
                + ui.item(
                    "Keep only the original",
                    "The copy is deleted.",
                    ui.go("Delete the copy", ui.url(here, ask="original")),
                )
                + ui.item(
                    "Keep only the copy",
                    "The original is deleted. The copy takes its name and waits in Review, "
                    "because you have not reviewed it yet.",
                    ui.go("Delete the original", ui.url(here, ask="copy")),
                )
            )
            asked = ctx.q("ask")
            if asked in {"original", "copy"}:
                dialog = ui.dialog(
                    "Delete the copy?" if asked == "original" else "Delete the original?",
                    (
                        f'"{e(copy["name"])}" is deleted.'
                        if asked == "original"
                        else f'"{e(original["name"])}" is deleted, and the copy takes its name.'
                    )
                    + f" {FINAL}",
                    f"{here}/copy",
                    "Delete the copy" if asked == "original" else "Delete the original",
                    here,
                    danger=True,
                    fields={"choice": asked},
                    busy="Working…",
                )
            body = f"""{head}<div class="stack">{ctx.flash()}
{
                ui.status(
                    "bad",
                    "This note has a copy",
                    "There are two files of this note. That happens when a note is duplicated in "
                    "Obsidian, or when two devices each saved their own version. Coppermind cannot "
                    "treat two files as one note, so it has left both exactly as they are and is "
                    "asking you.",
                )
            }
{
                ui.card(f'''<div class="two">
{
                    file_card(
                        view,
                        "The original." + (" Already reviewed and filed." if filed else ""),
                        original,
                        ctx,
                    )
                }
{file_card(view, "The copy. Not reviewed.", copy, ctx)}</div>
<div class="cmp" style="margin-top:10px"><div class="k">What is different</div>{differs}</div>
<h3 style="margin-top:12px">Choose what to do</h3>{choose}''')
            }</div>"""
            return await ctx.page(data["title"], body, "notes", dialog=dialog)
        if data.get("settling"):
            top = ui.status(
                "ok",
                "Reviewed, about to be filed",
                "You marked this note reviewed. Coppermind files it within a minute, once it "
                "has seen that the file has stopped changing.",
                ui.btn("Check again now", f"{here}/check", busy="Checking…")
                + obsidian(view, data["path"]),
            )
        elif stuck == "notfiled":
            why = data.get("why", "other")
            fix = {
                "date": " Add the date to the note in Obsidian and Coppermind files it within "
                "a minute.",
                "source": " Open the note in Obsidian, check it against the new version, then "
                "untick Reviewed and tick it again.",
                "disk": " Whoever runs the server should check that the notes folder has free "
                "space and can be written to. Coppermind keeps trying by itself.",
            }.get(why, "")
            top = ui.status(
                "bad",
                "Not filed",
                "You marked this note reviewed, so it should move to its folder, but it could "
                "not be filed. " + e(data.get("reason", "")) + fix,
                ui.btn("Check again now", f"{here}/check", "primary", busy="Checking…")
                + obsidian(view, data["path"]),
            ) + ui.tech(data.get("filing_error", ""))
        elif stuck == "missing" and data.get("gone"):
            top = ui.status(
                "",
                "Gone",
                "Its file is no longer in your vault, and you said that is on purpose, so "
                "Coppermind does not list it as a problem. If the file ever comes back, it is "
                "an ordinary note again.",
            )
        elif stuck == "missing" and data.get("recent") and not data.get("can_restore"):
            top = ui.status(
                "",
                "No longer in your vault",
                "Its file left your vault a few minutes ago. If you deleted it on a phone or "
                "computer, Coppermind confirms that within a few minutes and stops listing it. "
                "If it was moved, it shows up again by itself.",
            )
        elif stuck == "missing":
            gone = ui.btn("It is gone on purpose", f"{here}/gone", "", busy="Saving…")
            top = ui.status(
                "bad",
                "Missing",
                "Its file is no longer where it was, and Coppermind has no record of it being "
                "deleted. If you moved it out of the vault or a device has not synced yet, "
                "it comes back by itself when the file returns."
                + (
                    " Coppermind still has the last version it saw."
                    if data.get("can_restore")
                    else " Coppermind has no saved copy to bring back, so restore the file "
                    "from a device or a backup. If you deleted it on purpose, say so here and "
                    "Coppermind stops listing it."
                ),
                (
                    ui.btn(
                        "Bring back the last version",
                        "/admin/notes/restore",
                        "primary",
                        {"id": identity, "back": here},
                        busy="Bringing back…",
                    )
                    if data.get("can_restore")
                    else ""
                )
                + gone,
            )
        elif stuck == "unreadable":
            top = ui.status(
                "bad",
                "Cannot be read",
                "Coppermind found the file but could not read it as a note, so it has left it "
                "exactly as it is. The usual cause is damaged properties at the top of the "
                "note. Open it in Obsidian and correct them; Coppermind checks again by "
                "itself." + ui.tech(data.get("reason", "")),
                obsidian(view, data["path"], kind="primary"),
            )
        elif data["state"] == "filed":
            where = folder_words(data["path"].rsplit("/", 1)[0])
            name = e(data["path"].rsplit("/", 1)[-1].removesuffix(".md"))
            top = ui.status(
                "ok", "Filed", f'In {where}, as "{name}".', obsidian(view, data["path"])
            )
        elif data["state"] == "reviewed":
            top = ui.status(
                "ok",
                "Reviewed, being filed",
                "You marked it reviewed. Coppermind files it within a minute.",
                ui.btn("Check again now", f"{here}/check", busy="Checking…"),
            )
        else:
            top = ui.status(
                "off",
                "Waiting for your review",
                f"In {folder_words(data['path'].rsplit('/', 1)[0])} since "
                f"{ui.when(data['arrived'], ctx.zone)}. Open it in Obsidian, check it and tick "
                "Reviewed.",
                obsidian(view, data["path"], kind="primary"),
            )
        summary, notice, refresh = "", "", 0
        if data["state"] == "waiting" and not stuck:
            summary, notice, refresh = summary_card(await note_summary(ctx, identity), here)
        live = f"<div data-live>{summary}</div>" if summary else ""
        voices = ui.unnamed_voices(data.get("unnamed_voices", []))
        body = f'{head}<div class="stack">{ctx.flash()}{notice}{top}{live}{voices}{history}</div>'
        return await ctx.page(data["title"], body, "notes", refresh=refresh)

    async def note_summary(ctx: Ctx, identity: str) -> dict[str, Any]:
        try:
            return await ctx.act("note-summary", {"id": identity})
        except StoreError:
            return {"recording": False}

    async def fix(ctx: Ctx, identity: str, action: str, payload: dict[str, Any]) -> Any:
        try:
            return await ctx.act(action, {"id": identity, **payload})
        except StoreError as exc:
            ctx.say("bad", explain(exc, "That could not be done.", "Nothing was lost."))
            return None

    def filed_message(result: dict[str, Any], title: str, tail: str) -> tuple[str, str]:
        if result.get("filed"):
            where = folder_words(str(result["path"]).rsplit("/", 1)[0])
            return "ok", (f'Filed. "{e(title)}" is now in {where} as "{e(result["name"])}". {tail}')
        if result.get("settling"):
            return "info", (
                "Checked just now. Coppermind is waiting a moment to be sure the file has "
                "stopped changing, then files it. Nothing is wrong."
            )
        return "warn", "Checked just now. The note still cannot be filed. " + e(
            str(result.get("error", "")).removeprefix("ValueError: ")
        )

    @routes.post("/admin/notes/{identity}/check", include_in_schema=False)
    async def check(identity: str, request: Request) -> Response:
        ctx = Ctx(request, wiring)
        await ctx.form()
        try:
            title = (await ctx.act("note", {"id": identity}))["title"]
        except StoreError:
            return ctx.go("/admin/notes")
        result = await fix(ctx, identity, "note-check", {})
        if result is not None:
            ctx.say(*filed_message(result, title, ""))
        return ctx.go(model.note_path(identity))

    @routes.post("/admin/notes/{identity}/gone", include_in_schema=False)
    async def gone(identity: str, request: Request) -> Response:
        ctx = Ctx(request, wiring)
        await ctx.form()
        result = await fix(ctx, identity, "note-gone", {})
        if result is not None:
            ctx.say(
                "ok",
                f'Noted. "{e(result["title"])}" is no longer listed as a problem. Nothing was '
                "deleted by this.",
            )
            return ctx.go("/admin/notes")
        return ctx.go(model.note_path(identity))

    @routes.post("/admin/notes/{identity}/copy", include_in_schema=False)
    async def copy(identity: str, request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        result = await fix(ctx, identity, "note-copy", {"choice": form.get("choice", "")})
        if result is not None:
            original, duplicate = e(result["original"]), e(result["copy"])
            if result["choice"] == "both":
                said = (
                    f'Done. "{original}" stays exactly as it was. "{duplicate}" is now a '
                    "separate note, waiting in Review for you to check."
                )
            elif result["choice"] == "original":
                said = f'Done. "{original}" stays exactly as it was. The copy was deleted.'
            else:
                said = (
                    f'Done. The copy is now "{original}". It has not been reviewed, so it is '
                    "waiting in Review and has not been filed. The original was deleted."
                )
            ctx.say("ok", said)
        return ctx.go(model.note_path(identity))

    # -- the meeting summary -------------------------------------------------------------

    @routes.post("/admin/notes/{identity}/summary-again", include_in_schema=False)
    async def summary_again(identity: str, request: Request) -> Response:
        ctx = Ctx(request, wiring)
        await ctx.form()
        back = model.note_path(identity)
        try:
            result = await ctx.act("note-summary-again", {"id": identity})
        except StoreError as exc:
            return await ctx.refused(exc, back)
        if "refused" in result:
            ctx.say("bad", e(result["refused"]["message"]))
        elif result["already"]:
            ctx.say("info", "Coppermind is already writing the summary. This page shows it soon.")
        else:
            ctx.say("ok", "Coppermind is writing the summary again from the same transcript.")
        return ctx.go(back)

    @routes.get("/admin/notes/{identity}/review", include_in_schema=False)
    async def review(identity: str, request: Request) -> Response:
        """Notes are reviewed in Obsidian now. An old link lands on the note's own page."""
        return Ctx(request, wiring).go(model.note_path(identity))

    return routes
