"""Notes: find a note, see why one is stuck, fix it, and review one without Obsidian.

Notes live in Obsidian. This part of Admin says where a note is and deals with
the ones Coppermind could not handle by itself. Nothing is done to a note
unless the button for it was pressed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from coppermind.schema import normalize_note_time, note_date, note_start_time
from coppermind.settings import Wiring
from coppermind.statefiles import RevisionConflict
from coppermind.store_protocol import PatchFrontmatter, StoreError
from coppermind_admin import model, ui
from coppermind_admin.pages.common import folder_words, obsidian
from coppermind_admin.pages.settings import duration, folder_dialog, read_schema
from coppermind_admin.shell import Ctx
from coppermind_admin.ui import e, plural

LEDE = (
    "Notes live in Obsidian. This page is for finding out where a note is and fixing one "
    "that is stuck."
)
LABELS = {
    "notfiled": ("bad", "Needs you"),
    "waiting": ("accent", "Waiting for review"),
    "reviewed": ("accent", "Reviewed, being filed"),
    "deleted": ("", "Deleted"),
    "missing": ("bad", "Missing"),
    "filed": ("ok", "Filed"),
}
TEMPLATE_FOLDERS = ("Templates", "_Templates")
PATTERNS = (
    ("drawings", "Drawings", "*.excalidraw.md"),
    ("canvases", "Canvases", "*.canvas"),
    ("boards", "Boards", "*.kanban.md"),
)
ALONE_LEDE = (
    "Coppermind never treats these as notes: it does not file them, change them or add suggestions."
)


def keep_words(days: int) -> str:
    return "1 year" if days == 365 else plural(days, "day")


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


def until_text(deleted_at: str, days: int, ctx: Ctx) -> str:
    try:
        gone = datetime.fromisoformat(deleted_at)
    except ValueError:
        return ""
    from datetime import UTC, timedelta

    limit = gone + timedelta(days=days)
    if limit <= datetime.now(UTC):
        return "Past its keep time, so it is also listed under Clean up."
    until = ui.date_only(limit, ctx.zone)
    return f"Can be brought back until {until}, and after that until you clean up."


def deleted_row(note: dict[str, Any], days: int, ctx: Ctx, back: str, prefix: str = "") -> str:
    """A deleted note looks and behaves the same on every page that lists it."""
    fields = {"id": note["id"], "key": note["key"], "back": back}
    return ui.item(
        prefix + e(note["title"]),
        f"Deleted {ui.date_only(note['deleted_at'], ctx.zone)}. "
        + until_text(note["deleted_at"], days, ctx),
        ui.btn("Bring back", "/admin/notes/restore", "", fields, busy="Bringing back…")
        + ui.go(
            "Delete for good",
            ui.url(back, gone=note["id"] or note["key"]),
        ),
    )


def gone_dialog(notes: list[dict[str, Any]], asked: str, back: str) -> str:
    target = next((n for n in notes if asked and asked in {n["id"], n["key"]}), None)
    if target is None:
        return ""
    return ui.dialog(
        f'Delete "{e(target["title"])}" for good?',
        "It cannot be brought back afterwards.",
        "/admin/notes/delete-for-good",
        "Delete for good",
        back,
        danger=True,
        fields={"id": target["id"], "key": target["key"], "back": back},
        busy="Deleting…",
    )


SUMMARY_PARTS = (
    ("summary", "Summary"),
    ("decisions", "Decisions"),
    ("action_items", "Action items"),
)


def summary_text(summary: dict[str, Any]) -> str:
    """A proposed meeting summary, shown the way it will read in the note."""
    return "".join(
        f'<div class="row-l">{title}</div><div class="summary">{e(summary[key].strip())}</div>'
        for key, title in SUMMARY_PARTS
        if str(summary.get(key) or "").strip()
    )


def summary_card(
    summary: dict[str, Any], values: dict[str, str], here: str
) -> tuple[str, str, int]:
    """The summary part of a review: the card, a notice to show above the form, a refresh."""
    if not summary.get("recording"):
        return "", "", 0
    title = "The meeting summary"
    state, service = summary["state"], summary.get("service")
    again = ""
    notice = ""
    if state == "on":
        again = (
            f'<button class="lat-btn" formaction="{e(here)}/summary-again" formnovalidate '
            'data-busy="Starting…">Write the summary again</button>'
        )
        how = (
            " Writing it again uses the transcript Coppermind already has (the recording is "
            "not sent anywhere again) and the directions as they are now. "
            + ui.a("Change the directions", "/admin/suggestions/summary")
        )
    elif state == "failing" and service:
        how = (
            f" It cannot be written again right now, because {e(service['name'])} is not answering."
        )
        notice = ui.banner(
            "bad",
            f"<b>{e(service['name'])} is not answering</b>, so a summary cannot be written "
            "right now. Start it again and test it, or edit it if its address has changed. "
            + ui.btn(
                "Test now",
                f"/admin/services/{ui.segment(service['id'])}/test",
                "",
                {"back": here + "/review"},
                busy="Testing…",
            )
            + ui.go(
                "Edit this service",
                ui.url(f"/admin/services/{ui.segment(service['id'])}", back=here + "/review"),
            ),
        )
    else:
        how = (
            " It cannot be written again right now, because suggestions are "
            f"{'paused' if state == 'paused' else 'off'}. "
            + ui.a("Open Suggestions", "/admin/suggestions")
        )
    proposed, earlier = summary.get("proposed"), summary.get("earlier")

    def earlier_choice(picked: str) -> str:
        """The summary from before, shown and on offer while there is no newer one."""
        if not earlier:
            return ui.hidden({"summary": "none"})
        return (
            '<div class="row-l" style="margin-top:10px">The summary from before</div>'
            f"{summary_text(earlier)}"
            + ui.choice(
                "summary",
                "earlier",
                "Add the summary from before to the note",
                "It goes at the end of the note when you mark it reviewed.",
                picked != "none",
            )
            + ui.choice(
                "summary",
                "none",
                "Leave the summary out",
                "The note is filed without one.",
                picked == "none",
            )
        )

    if summary["writing"]:
        body = (
            '<p><span class="lat-badge lat-badge--accent">Writing</span> Coppermind is writing '
            "the summary now. It usually takes a minute or two, and this page updates by "
            "itself. Nothing is added to the note until you mark it reviewed.</p>"
            + (
                '<p class="muted">The summary from before is kept. If you mark the note '
                "reviewed before the new one is here, it is the one that is used unless you "
                "choose to leave the summary out.</p>"
                if earlier
                else ""
            )
            + earlier_choice(values.get("summary", ""))
        )
        return ui.card(body, title), notice, 8
    if not proposed:
        body = (
            "<p>"
            + (
                "The summary could not be written"
                + (" again. The one from before is still here." if earlier else ".")
                + ("" if earlier else " The note can still be reviewed and filed without one.")
                if summary["failed"]
                else "There is no summary for this note yet."
            )
            + how
            + "</p>"
            + (f'<div class="actions">{again}</div>' if again else "")
            + earlier_choice(values.get("summary", ""))
        )
        return ui.card(body, title), notice, 0
    picked = values.get("summary") or "new"
    choices = ui.choice(
        "summary",
        "new",
        "Add this summary to the note",
        "It goes at the end of the note when you mark it reviewed.",
        picked == "new",
    )
    if earlier:
        choices += ui.choice(
            "summary",
            "earlier",
            "Use the earlier summary instead",
            "The one from before you asked for it to be written again.",
            picked == "earlier",
        ) + ui.reveal("Show the earlier summary", summary_text(earlier))
    choices += ui.choice(
        "summary",
        "none",
        "Leave the summary out",
        "The note is filed without one.",
        picked == "none",
    )
    kept = (
        " The earlier summary is kept until you mark the note reviewed, so you can still "
        "choose it below."
        if earlier
        else ""
    )
    body = (
        f"{summary_text(proposed)}"
        '<p class="muted" style="margin-top:10px">Nothing is written to the note until you '
        f"mark it reviewed.{kept}{how}</p>"
        + (f'<div class="actions">{again}</div>' if again else "")
        + f'<div style="margin-top:10px">{choices}</div>'
    )
    return ui.card(body, title), notice, 0


def invited(meeting: dict[str, Any]) -> int:
    """How many people a meeting invited, each counted once: the organizer is often listed twice."""
    people = meeting.get("people") or []
    return len(
        {
            str(person.get("address") or person.get("name") or "").strip().lower()
            if isinstance(person, dict)
            else str(person).strip().lower()
            for person in people
        }
        - {""}
    )


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
        days = ctx.settings().retention.days
        try:
            waiting = await ctx.act("notes-waiting", {})
            deleted = (await ctx.act("notes-deleted", {}))["items"]
            alone = (await ctx.act("notes-alone", {}))["items"]
            found = (await ctx.act("notes-find", {"q": query}))["items"] if query else []
        except StoreError:
            return await ctx.unavailable("Notes", LEDE, "notes")
        waiting = waiting["items"]
        stuck, unsure = view["notes"]["stuck"], view["notes"]["unsure"]
        need = len(stuck) + len(unsure)
        if not (waiting or deleted or alone or stuck) and not query:
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
        if tab not in {"notfiled", "waiting", "alone", "deleted"}:
            tab = default
        here = ui.url("/admin/notes", tab=tab)
        sugg_on = view["sugg"]["state"] == "on"

        def waiting_row(note: dict[str, Any], prefix: str = "") -> str:
            link = model.note_path(note["id"])
            title = prefix + ui.a(e(note["title"]), link)
            if note["unsure"]:
                title += " " + ui.badge("warn", f"{note['unsure']} to check")
            detail = (
                f"Arrived {ui.when(note['arrived'], ctx.zone)}. Waiting for you to check it "
                "in Obsidian."
            )
            if note["unsure"]:
                detail += (
                    f" Coppermind was not sure about {plural(note['unsure'], 'suggestion')} "
                    f"and needs your answer."
                )
            missing = note["no_suggestions"]
            if missing and view["sugg"]["state"] == "none":
                detail += " No suggestions: suggestions are off."
            elif missing == "before":
                detail += " No suggestions yet: it arrived before suggestions were turned on."
            elif missing == "failed":
                detail += " No suggestions: the model's answer could not be used."
            elif missing == "soon" and sugg_on:
                detail += " Suggestions are being written."
            actions = (
                ui.go(
                    "Check it" if note["unsure"] == 1 else "Check them", link + "/review", "primary"
                )
                if note["unsure"]
                else ui.go("Review here", link + "/review")
            )
            if missing in {"before", "failed"} and sugg_on:
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
                detail = (
                    f"You marked it reviewed on {ui.date_only(note['when'], ctx.zone)}, but a "
                    "file with its name is already in the folder."
                    if note.get("name_taken")
                    else "You marked it reviewed, but it could not be filed. " + e(note["reason"])
                )
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

        def unsure_row(note: dict[str, Any]) -> str:
            link = model.note_path(note["id"])
            return ui.item(
                ui.a(e(note["title"]), link) + " " + ui.badge("warn", f"{note['count']} to check"),
                f"Coppermind was not sure about {plural(note['count'], 'suggestion')} and "
                "needs your answer. Nothing is filled in until you say.",
                ui.go(
                    "Check it" if note["count"] == 1 else "Check them", link + "/review", "primary"
                ),
            )

        if query:
            by_id = {note["id"]: note for note in waiting}
            lines = ""
            for note in found:
                badge = ui.badge(*LABELS[note["state"]]) + " "
                link = model.note_path(note["id"])
                if note["state"] == "waiting" and note["id"] in by_id:
                    lines += waiting_row(by_id[note["id"]], badge)
                elif note["state"] == "deleted":
                    lines += ui.item(
                        badge + ui.a(e(note["title"]), link),
                        f"Deleted {ui.date_only(note['deleted_at'], ctx.zone)}.",
                        ui.go("Open", link),
                    )
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
                        "including filed and deleted ones.",
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
                "deleted": f"Deleted notes can be brought back for {keep_words(days)}, and "
                "after that until you clean up.",
            }[tab]
            if tab == "notfiled":
                lines = "".join(stuck_row(n) for n in stuck) + "".join(
                    unsure_row(n) for n in unsure
                )
                none = ui.empty(
                    "Nothing needs you",
                    "Every note you reviewed has been filed, and no suggestion is waiting for "
                    "your answer.",
                )
            elif tab == "waiting":
                lines = "".join(waiting_row(n) for n in waiting[:100])
                if len(waiting) > 100:
                    lines += (
                        f'<p class="lat-hint">Showing the newest 100 of {len(waiting)}. Use '
                        "the box above to find an older one by name.</p>"
                    )
                none = ui.empty("Nothing is waiting for review", "")
            elif tab == "alone":
                lines = "".join(
                    ui.item(e(item["title"]), alone_why(item["why"])) for item in alone[:100]
                )
                if len(alone) > 100:
                    lines += f'<p class="lat-hint">Showing 100 of {len(alone)}.</p>'
                none = ui.empty("Nothing is left alone", "")
            else:
                lines = "".join(deleted_row(n, days, ctx, here) for n in deleted)
                none = ui.empty("No deleted notes", "Nothing is waiting to be brought back.")
            inner = f'<p class="muted">{intro}</p>' + (lines or none)
            tabs = ui.seg(
                "/admin/notes",
                [
                    ("notfiled", f"Need you ({need})"),
                    ("waiting", f"Waiting for review ({len(waiting)})"),
                    ("alone", f"Left alone on purpose ({len(alone)})"),
                    ("deleted", f"Deleted ({len(deleted)})"),
                ],
                tab,
            )
        body = f"""{ui.head("Notes", LEDE)}<div class="stack">{ctx.flash()}
<form method="get" action="/admin/notes">
{
            ui.text_input(
                "q",
                "Find any note by name",
                query,
                live=True,
                autofocus=bool(query),
                hint="Searches every note, including the ones already filed.",
            )
        }</form>
{tabs}{ui.card(inner)}</div>"""
        return await ctx.page(
            "Notes", body, "notes", dialog=gone_dialog(deleted, ctx.q("gone"), here)
        )

    @routes.post("/admin/notes/restore", include_in_schema=False)
    async def restore(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        target = ctx.back("/admin/notes?tab=deleted")
        try:
            result = await ctx.act(
                "note-restore", {"id": form.get("id", ""), "key": form.get("key", "")}
            )
        except StoreError as exc:
            ctx.say(
                "bad",
                "That note could not be brought back just now. " + e(str(exc)),
            )
            return ctx.go(target)
        where = folder_words(str(result["path"]).rsplit("/", 1)[0]) or "your vault"
        ctx.say(
            "ok",
            f'Brought back "{e(result["title"])}". It is in {where} in Obsidian again, and is '
            "off the Deleted list.",
        )
        return ctx.go(target)

    @routes.post("/admin/notes/delete-for-good", include_in_schema=False)
    async def delete_for_good(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        target = ctx.back("/admin/notes?tab=deleted")
        try:
            result = await ctx.act(
                "note-delete-for-good", {"id": form.get("id", ""), "key": form.get("key", "")}
            )
        except StoreError as exc:
            return await ctx.refused(exc, target)
        ctx.say("ok", f'Deleted "{e(result["title"])}" for good.')
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
            dialog = (
                folder_dialog(
                    "Choose a folder for Coppermind to leave alone",
                    [name for name in available if name not in folders],
                    "",
                    action="/admin/notes/leave-alone",
                    method="get",
                    extra=carry + ticked + ui.hidden({"set": "add"}),
                    cancel=ui.url("/admin/notes/leave-alone"),
                )
                .replace('name="folder"', 'name="picked"')
                .replace('data-if="folder=', 'data-if="picked=')
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
        except (ValueError, RevisionConflict):
            ctx.say("bad", "That could not be saved. Check the folder names and try again.")
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
        days = keep_words(ctx.settings().retention.days)
        here = model.note_path(identity)
        back = ("/admin", "Home") if ctx.q("from") == "home" else ("/admin/notes", "Notes")
        head = ui.head(e(data["title"]), "A note, and what has happened to it.", back)
        history = ui.card(
            ui.timeline(
                [(ui.when(entry["at"], ctx.zone), e(entry["text"])) for entry in data["history"]]
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
                    f"The copy moves to Deleted, where it can be brought back for {days}.",
                    ui.go("Delete the copy", ui.url(here, ask="original")),
                )
                + ui.item(
                    "Keep only the copy",
                    f"The original moves to Deleted, where it can be brought back for {days}. "
                    "The copy takes its name and waits in Review, because you have not "
                    "reviewed it yet.",
                    ui.go("Delete the original", ui.url(here, ask="copy")),
                )
            )
            asked = ctx.q("ask")
            if asked in {"original", "copy"}:
                dialog = ui.dialog(
                    "Delete the copy?" if asked == "original" else "Delete the original?",
                    (
                        f'"{e(copy["name"])}" moves to Deleted.'
                        if asked == "original"
                        else f'"{e(original["name"])}" moves to Deleted, and the copy takes '
                        "its name."
                    )
                    + f" It can be brought back for {days}.",
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
        if stuck == "notfiled" and data.get("name_taken"):
            mine, taken = data["mine"], data["taken"]
            if ctx.q("ask") == "replace":
                dialog = ui.dialog(
                    "Replace the older file?",
                    f'"{e(taken["name"])}" (edited {ui.date_only(taken["edited"], ctx.zone)}, '
                    f"{plural(taken['words'], 'word')}) moves to Deleted and can be brought "
                    f"back for {days}. This note is filed in its place.",
                    f"{here}/replace",
                    "Replace it",
                    here,
                    danger=True,
                    busy="Filing…",
                    fields={"seen": taken.get("hash", "")},
                )
            body = f"""{head}<div class="stack">{ctx.flash()}
{
                ui.status(
                    "bad",
                    "Not filed",
                    "You marked this note reviewed, so it should move to its folder. A different "
                    f'file with exactly its name, "{e(taken["name"])}", is already in the folder.',
                )
            }
{
                ui.card(f'''<div class="two">
{file_card(view, "This note (waiting to move)", mine, ctx)}
{file_card(view, "Already in the folder", taken, ctx)}</div>
<h3 style="margin-top:12px">Choose what to do</h3>
{
                    ui.item(
                        "Keep both",
                        f'File this note next to the other one as "{e(data["both_as"])}". '
                        "Nothing is lost.",
                        ui.btn("Keep both", f"{here}/keep-both", "primary", busy="Filing…"),
                    )
                }
{
                    ui.item(
                        "Replace the older file",
                        "The file already in the folder moves to Deleted, where it can be brought "
                        f"back for {days}. This note takes its place.",
                        ui.go("Replace it", ui.url(here, ask="replace")),
                    )
                }
{
                    ui.item(
                        "Sort it out in Obsidian",
                        "Rename or move one of the two files yourself. Coppermind files the note "
                        "within a minute once the name is free.",
                        ui.btn("Check again now", f"{here}/check", busy="Checking…"),
                    )
                }''')
            }
<div class="hide-narrow">{history}</div></div>"""
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
                "time": " If there was no set time, say so here. If there was, add it to the "
                "note in Obsidian and Coppermind files it within a minute.",
                "date": " Add the date to the note in Obsidian and Coppermind files it within "
                "a minute.",
                "source": " Open the note in Obsidian, check it against the new version, then "
                "untick Reviewed and tick it again.",
                "disk": " Whoever runs the server should check that the notes folder has free "
                "space and can be written to. Coppermind keeps trying by itself.",
            }.get(why, "")
            no_time = (
                ui.btn("There was no set time", f"{here}/no-time", "primary", busy="Filing…")
                if why == "time"
                else ""
            )
            top = ui.status(
                "bad",
                "Not filed",
                "You marked this note reviewed, so it should move to its folder, but it could "
                "not be filed. " + e(data.get("reason", "")) + fix,
                no_time
                + ui.btn(
                    "Check again now",
                    f"{here}/check",
                    "" if no_time else "primary",
                    busy="Checking…",
                )
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
                "computer, Coppermind confirms that within a few minutes and lists it under "
                "Deleted, where it can be brought back. If it was moved, it shows up again by "
                "itself.",
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
        elif data["state"] == "waiting":
            unsure = data["unsure"]
            top = ui.status(
                "off",
                "Waiting for your review",
                f"In {folder_words(data['path'].rsplit('/', 1)[0])} since "
                f"{ui.when(data['arrived'], ctx.zone)}. Open it in Obsidian, check it and tick "
                "Reviewed."
                + (
                    f" Coppermind was not sure about {plural(unsure, 'suggestion')}."
                    if unsure
                    else ""
                ),
                obsidian(view, data["path"], kind="" if unsure else "primary")
                + ui.go(
                    "Check them here" if unsure else "Review it here instead",
                    f"{here}/review",
                    "primary" if unsure else "",
                ),
            )
        else:
            keep = ctx.settings().retention.days
            top = ui.status(
                "off",
                "Deleted",
                f"Deleted {ui.date_only(data['deleted_at'], ctx.zone)}. "
                + until_text(data["deleted_at"], keep, ctx),
                ui.btn(
                    "Bring back",
                    "/admin/notes/restore",
                    "primary",
                    {"id": identity, "key": "", "back": here},
                    busy="Bringing back…",
                ),
            )
        body = f'{head}<div class="stack">{ctx.flash()}{top}{history}</div>'
        return await ctx.page(data["title"], body, "notes")

    async def fix(ctx: Ctx, identity: str, action: str, payload: dict[str, Any]) -> Any:
        try:
            return await ctx.act(action, {"id": identity, **payload})
        except StoreError as exc:
            ctx.say("bad", "That could not be done. " + e(str(exc)) + " Nothing was lost.")
            return None

    def filed_message(result: dict[str, Any], title: str, tail: str) -> tuple[str, str]:
        if result.get("filed"):
            where = folder_words(str(result["path"]).rsplit("/", 1)[0])
            return "ok", (f'Filed. "{e(title)}" is now in {where} as "{e(result["name"])}". {tail}')
        if result.get("error") == "PathCollision":
            return "warn", (
                "Checked just now. The other file is still there, so the note is still "
                "waiting. Rename or move one of them in Obsidian, or choose Keep both or "
                "Replace."
            )
        if result.get("settling"):
            return "info", (
                "Checked just now. Coppermind is waiting a moment to be sure the file has "
                "stopped changing, then files it. Nothing is wrong."
            )
        return "warn", "Checked just now. The note still cannot be filed. " + e(
            str(result.get("error", "")).removeprefix("ValueError: ")
        )

    @routes.post("/admin/notes/{identity}/keep-both", include_in_schema=False)
    async def keep_both(identity: str, request: Request) -> Response:
        ctx = Ctx(request, wiring)
        await ctx.form()
        here = model.note_path(identity)
        try:
            title = (await ctx.act("note", {"id": identity}))["title"]
        except StoreError:
            return ctx.go("/admin/notes")
        result = await fix(ctx, identity, "note-keep-both", {})
        if result is not None:
            ctx.say(*filed_message(result, title, "The older file was not touched."))
        return ctx.go(here)

    @routes.post("/admin/notes/{identity}/replace", include_in_schema=False)
    async def replace(identity: str, request: Request) -> Response:
        ctx = Ctx(request, wiring)
        await ctx.form()
        here = model.note_path(identity)
        days = keep_words(ctx.settings().retention.days)
        try:
            title = (await ctx.act("note", {"id": identity}))["title"]
        except StoreError:
            return ctx.go("/admin/notes")
        result = await fix(
            ctx, identity, "note-replace", {"seen": (await ctx.form()).get("seen", "")}
        )
        if result is not None and "refused" in result:
            ctx.say("warn", e(result["refused"]["message"]))
            return ctx.go(here)
        if result is not None:
            ctx.say(
                *filed_message(
                    result,
                    title,
                    f"The older file is under Notes, Deleted, and can be brought back for {days}.",
                )
            )
        return ctx.go(here)

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

    @routes.post("/admin/notes/{identity}/no-time", include_in_schema=False)
    async def no_time(identity: str, request: Request) -> Response:
        ctx = Ctx(request, wiring)
        await ctx.form()
        try:
            title = (await ctx.act("note", {"id": identity}))["title"]
        except StoreError:
            return ctx.go("/admin/notes")
        result = await fix(ctx, identity, "note-no-time", {})
        if result is not None:
            ctx.say(*filed_message(result, title, "It is marked as having no set time."))
        return ctx.go(model.note_path(identity))

    @routes.post("/admin/notes/{identity}/copy", include_in_schema=False)
    async def copy(identity: str, request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        days = keep_words(ctx.settings().retention.days)
        result = await fix(ctx, identity, "note-copy", {"choice": form.get("choice", "")})
        if result is not None:
            original, duplicate = e(result["original"]), e(result["copy"])
            if result["choice"] == "both":
                said = (
                    f'Done. "{original}" stays exactly as it was. "{duplicate}" is now a '
                    "separate note, waiting in Review for you to check."
                )
            elif result["choice"] == "original":
                said = (
                    f'Done. "{original}" stays exactly as it was. The copy is under Notes, '
                    f"Deleted, and can be brought back for {days}."
                )
            else:
                said = (
                    f'Done. The copy is now "{original}". It has not been reviewed, so it is '
                    "waiting in Review and has not been filed. The original is under Notes, "
                    f"Deleted, and can be brought back for {days}."
                )
            ctx.say("ok", said)
        return ctx.go(model.note_path(identity))

    # -- review a note here --------------------------------------------------------------

    async def review_data(ctx: Ctx, identity: str) -> dict[str, Any]:
        return await ctx.store.context_action("lifecycle", {"note_id": identity})

    async def note_summary(ctx: Ctx, identity: str) -> dict[str, Any]:
        try:
            return await ctx.act("note-summary", {"id": identity})
        except StoreError:
            return {"recording": False}

    def open_questions(data: dict[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """What Coppermind was not sure about: which meeting, and each open suggestion."""
        meeting = data.get("recording_meeting")
        ask_meeting = None
        if meeting and len(meeting["candidates"]) > 1 and not meeting.get("selected_event_id"):
            ask_meeting = meeting
        suggestions = [
            s for s in data["suggestions"] if s["kind"] != "filename" and not data["stale"]
        ]
        return ask_meeting, suggestions

    async def review_page(
        ctx: Ctx, identity: str, values: dict[str, str], errors: dict[str, str], status: int = 200
    ) -> Response:
        view = await ctx.view()
        try:
            data = await review_data(ctx, identity)
        except StoreError:
            return ctx.go("/admin/notes")
        note = data["note"]
        here = model.note_path(identity)
        _, schema = read_schema(ctx.control)
        roles = data["roles"]
        front = note["frontmatter"]
        if front.get(roles["reviewed_key"]) is True:
            ctx.say("info", "That note has already been reviewed.")
            return ctx.go(here)
        day = note_date(front, schema)
        start = note_start_time(front, schema)
        type_key = next(key for key in schema.keys if key.name == roles["type_key"])
        context_key = next(key for key in schema.keys if key.name == roles["context_key"])
        kind = str(front.get(roles["type_key"]) or "")
        types = list(type_key.vocabulary) + (
            [kind] if kind and kind not in type_key.vocabulary else []
        )

        def listed(name: str) -> str:
            value = front.get(name)
            if isinstance(value, list):
                return ", ".join(str(item) for item in value)
            return str(value or "")

        def field(name: str, label: str, value: str, kind: str = "text", hint: str = "") -> str:
            return ui.text_input(
                name,
                label,
                values.get(name, value),
                kind=kind,
                hint=hint,
                error=errors.get(name, ""),
            )

        meeting, suggestions = open_questions(data)
        unsure = ""
        if meeting:
            options = "".join(
                ui.choice(
                    "meeting",
                    candidate["event_id"],
                    e(candidate["title"]),
                    f"{ui.when(candidate['start'], ctx.zone)}. "
                    f"{plural(invited(candidate), 'person', 'people')} invited.",
                    values.get("meeting") == candidate["event_id"],
                )
                for candidate in meeting["candidates"]
            ) + ui.choice("meeting", "none", "None of these", "", values.get("meeting") == "none")
            unsure += (
                '<div class="field field--wide"><span class="lat-label">Which meeting was '
                f"this? {plural(len(meeting['candidates']), 'meeting')} on your calendar "
                f"could be it.</span>{options}{ui.field_error(errors.get('meeting', ''))}</div>"
            )
        for suggestion in suggestions:
            name = f"s_{suggestion['index']}"
            answer = values.get(name, "")
            if suggestion["kind"] == "speaker":
                people = "".join(
                    ui.choice(name, c["id"], f"Yes, that was {e(c['name'])}", "", answer == c["id"])
                    for c in suggestion.get("candidates", [])
                    if not suggestion["value"] or c["id"] == suggestion["value"]
                )
                question = f"Who is {e(suggestion['field'])}? {e(suggestion['reason'])}"
                options = people + ui.choice(
                    name, "no", "No, leave that speaker unnamed", "", answer == "no"
                )
            else:
                shown = e(suggestion.get("display_value", ""))
                what = e(suggestion["field"] or suggestion["kind"])
                question = f"{what[0].upper() + what[1:]}: {shown}? {e(suggestion['reason'])}"
                options = ui.choice(name, "yes", "Yes, use it", "", answer == "yes") + ui.choice(
                    name, "no", "No, leave it out", "", answer == "no"
                )
            quote = (
                f'<div class="quote">{e(suggestion["quote"])}</div>'
                if suggestion.get("quote")
                else ""
            )
            unsure += (
                f'<div class="field field--wide"><span class="lat-label">{question}</span>'
                f"{quote}{options}{ui.field_error(errors.get(name, ''))}</div>"
            )
        unsure_card = (
            ui.card(unsure, "Coppermind was not sure about these. Nothing is chosen for you.")
            if unsure
            else ""
        )
        left = (
            field("date", "Date", day.isoformat() if day else "", "date")
            + field(
                "time",
                "Start time, if it was a meeting",
                start.strftime("%H:%M") if start else "",
                "time",
                "Leave empty if there was no set time.",
            )
            + ui.select(
                "type",
                "Type",
                [(item, item) for item in types],
                values.get("type", kind),
                hint="Decides the folder. These are the types you already use.",
            )
            + ui.select(
                "context",
                "Context",
                [(item, item) for item in context_key.vocabulary],
                values.get("context", str(front.get(roles["context_key"]) or "")),
            )
        )
        right = (
            field(
                "account",
                "Account",
                listed(roles["account_key"]),
                hint="Customers or companies, separated by commas.",
            )
            + field("tags", "Tags", listed(roles["tags_key"]), hint="Separated by commas.")
            + (
                field(
                    "attendees",
                    "Attendees",
                    listed("attendees"),
                    hint="Who was there, separated by commas.",
                )
                if any(key.name == "attendees" for key in schema.keys)
                else ""
            )
        )
        problem = ui.banner("warn", e(data["review_problem"])) if data.get("review_problem") else ""
        summary_part, summary_notice, refresh = summary_card(
            await note_summary(ctx, identity), values, here
        )
        lede = (
            "The same check you would do in Obsidian, for when Obsidian is not to hand. "
            "Nothing is filed until you press the button at the bottom."
        )
        body = f"""{ui.head(f'Review "{e(note["title"])}"', lede, (here, "the note"))}
<div class="stack">{ctx.flash()}{problem}{summary_notice}
<form method="post" action="{here}/review" class="stack">
{ui.hidden({"if_match": note["content_hash"]})}{unsure_card}{summary_part}
{
            ui.card(
                f'<div class="two"><div>{left}</div><div>{right}</div></div>',
                "What Coppermind filled in. Correct anything that is wrong.",
            )
        }
<div class="actions">
{ui.submit("Mark as reviewed and file it", name="how", value="file", busy="Filing…")}
{ui.submit("Save, but do not file yet", "", name="how", value="save", busy="Saving…")}
{obsidian(view, note["path"])}</div></form></div>"""
        return await ctx.page(
            "Review a note", body, "notes", status=status, refresh=refresh if status == 200 else 0
        )

    @routes.post("/admin/notes/{identity}/summary-again", include_in_schema=False)
    async def summary_again(identity: str, request: Request) -> Response:
        ctx = Ctx(request, wiring)
        await ctx.form()
        back = model.note_path(identity) + "/review"
        try:
            result = await ctx.act("note-summary-again", {"id": identity})
        except StoreError as exc:
            return await ctx.refused(exc, back)
        if "refused" in result:
            ctx.say("bad", e(result["refused"]["message"]))
        elif result["already"]:
            ctx.say("info", "Coppermind is already writing the summary. This page shows it soon.")
        else:
            ctx.say(
                "ok",
                "Coppermind is writing the summary again from the same transcript. The one "
                "you had is kept, and you can still choose it when the new one is here. "
                "Anything you had typed on this page was not saved.",
            )
        return ctx.go(back)

    @routes.get("/admin/notes/{identity}/review", include_in_schema=False)
    async def review(identity: str, request: Request) -> Response:
        return await review_page(Ctx(request, wiring), identity, {}, {})

    @routes.post("/admin/notes/{identity}/review", include_in_schema=False)
    async def review_save(identity: str, request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        here = model.note_path(identity)
        try:
            data = await review_data(ctx, identity)
        except StoreError:
            return ctx.go("/admin/notes")
        meeting, suggestions = open_questions(data)
        errors: dict[str, str] = {}
        if meeting and not form.get("meeting"):
            errors["meeting"] = "Choose one, so Coppermind is not guessing."
        for suggestion in suggestions:
            if not form.get(f"s_{suggestion['index']}"):
                errors[f"s_{suggestion['index']}"] = "Choose one, so Coppermind is not guessing."
        if not form.get("date"):
            errors["date"] = "Pick the date of the note."
        if errors:
            return await review_page(ctx, identity, form, errors, 422)
        roles = data["roles"]
        note = data["note"]
        front = note["frontmatter"]
        if form.get("if_match") and form["if_match"] != note["content_hash"]:
            # The note was edited somewhere else after this page was drawn. What is on the
            # page is older than the note, so none of it is written over the newer edit.
            ctx.say(
                "warn",
                "This note was changed somewhere else while the page was open, so nothing "
                "was saved. The page now shows the note as it is; check it and press the "
                "button again.",
            )
            return ctx.go(here + "/review")
        _, schema = read_schema(ctx.control)

        def split(text: str) -> list[str]:
            return [part.strip() for part in text.split(",") if part.strip()]

        entered = form["date"] + (f"T{form['time']}:00" if form.get("time") else "")
        changes: dict[str, Any] = {
            roles["date_key"]: normalize_note_time(entered) if form.get("time") else entered,
            roles["type_key"]: form.get("type", ""),
            roles["context_key"]: form.get("context", ""),
            roles["account_key"]: split(form.get("account", "")),
            roles["tags_key"]: split(form.get("tags", "")),
        }
        if "attendees" in form:
            changes["attendees"] = split(form["attendees"])
        if form.get("type") == "work":
            changes["time_unknown"] = not form.get("time")
        current_day, current_start = note_date(front, schema), note_start_time(front, schema)
        same_time = (
            current_day is not None
            and current_day.isoformat() == form["date"]
            and (current_start.strftime("%H:%M") if current_start else "") == form.get("time", "")
        )
        if same_time:
            changes.pop(roles["date_key"])
        # Only what its owner actually changed is written. An empty box is the same as a
        # property the note does not have, so it never adds an empty property to the note.
        changes = {
            key: value
            for key, value in changes.items()
            if front.get(key) != value and (value or front.get(key))
        }
        selected: list[int] = []
        speakers: dict[str, str] = {}
        for suggestion in suggestions:
            answer = form.get(f"s_{suggestion['index']}", "no")
            if answer == "no":
                continue
            selected.append(suggestion["index"])
            if suggestion["kind"] == "speaker":
                speakers[str(suggestion["index"])] = answer
        picked = form.get("meeting", "")
        choose = picked if meeting and picked != "none" else ""
        # The summary its owner chose: the one proposed now, the earlier one, or none.
        offered = await note_summary(ctx, identity)
        wanted = form.get("summary", "new")
        summary = None
        # The review a summary is added through: the current one, or, when the newer summary
        # is not here (still being written, or it failed), the one the earlier summary came with.
        review_job = data["job_id"]
        current = offered.get("proposed") if not offered.get("writing") else None
        if offered.get("recording"):
            if wanted == "new" and current:
                summary = current
            elif wanted == "earlier" and offered.get("earlier"):
                summary = offered["earlier"]
                if not current:
                    review_job = offered["earlier_job"]
        # The first write is held to the note as the page showed it; later ones follow on.
        expected = form.get("if_match", "") or note["content_hash"]
        calendar_hash = (data.get("recording_meeting") or {}).get("calendar_hash", "")

        async def fresh() -> None:
            nonlocal data, note, expected, calendar_hash
            data = await review_data(ctx, identity)
            note = data["note"]
            expected = note["content_hash"]
            calendar_hash = (data.get("recording_meeting") or {}).get(
                "calendar_hash", calendar_hash
            )

        async def apply_edits() -> None:
            """The person's own corrections, then the meeting they picked."""
            if changes:
                await ctx.store.patch_frontmatter(
                    identity,
                    PatchFrontmatter(set=changes, unset=[] if same_time else ["time", "timezone"]),
                    if_match=expected,
                )
                await fresh()
            if choose:
                await ctx.store.context_action(
                    "choose-recording-meeting",
                    {
                        "note_id": identity,
                        "if_match": expected,
                        "calendar_hash": calendar_hash,
                        "event_id": choose,
                    },
                )
                await fresh()

        async def complete(
            chosen: list[int], voices: dict[str, str], prose: dict[str, Any] | None = None
        ) -> None:
            request: dict[str, Any] = {
                "note_id": identity,
                "if_match": expected,
                "job_id": review_job if prose is not None else data["job_id"],
                "confirmation": "Complete review",
                "accept_link_risk": False,
                "selected": chosen,
                "speaker_choices": voices,
            }
            if prose is not None:
                request["meeting_summary"] = prose
            await ctx.store.context_action("complete-review", request)
            await fresh()

        review_here = here + "/review"
        try:
            if form.get("how") == "file":
                if selected or summary:
                    # Suggestions and the summary were written for the note as it stands, so
                    # they are taken before anything else changes it.
                    try:
                        await complete(selected, speakers, summary)
                    except StoreError:
                        if not (changes or choose):
                            raise
                        await apply_edits()
                        ctx.say(
                            "warn",
                            "Saved your corrections, but the note is not filed yet. The "
                            "suggestions and summary were written for the note as it was, so "
                            "Coppermind is writing them again. They are back on this page "
                            "within a minute or two; then press Mark as reviewed and file it.",
                        )
                        return ctx.go(review_here)
                    try:
                        await apply_edits()
                    except StoreError as exc:
                        ctx.say(
                            "warn",
                            "Reviewed, with what you accepted. One of your own "
                            "corrections could not be saved, so check the note in Obsidian. "
                            + e(str(exc)),
                        )
                        return ctx.go(here)
                else:
                    await apply_edits()
                    await complete([], {})
                result = await ctx.act("note-check", {"id": identity, "reviewed": True})
                if result.get("filed"):
                    where = folder_words(str(result["path"]).rsplit("/", 1)[0])
                    ctx.say("ok", f'Reviewed and filed. "{e(note["title"])}" is now in {where}.')
                elif result.get("settling"):
                    ctx.say(
                        "ok",
                        f'Reviewed. "{e(note["title"])}" is filed within a minute, once '
                        "Coppermind has seen that the file has stopped changing.",
                    )
                else:
                    ctx.say(
                        "warn",
                        f'Reviewed. "{e(note["title"])}" could not be filed yet; this page '
                        "says why.",
                    )
                return ctx.go(here)
            kept_for_later = 0
            for suggestion in suggestions:
                answer = form.get(f"s_{suggestion['index']}", "")
                if suggestion["kind"] == "speaker":
                    kept_for_later += 1
                    continue
                await ctx.store.context_action(
                    "approve" if answer == "yes" else "reject",
                    {
                        "id": data["job_id"],
                        "index": suggestion["index"],
                        "confirmation": "Apply selected suggestion",
                    },
                )
                await fresh()
            await apply_edits()
        except StoreError as exc:
            ctx.say("bad", "That could not be saved. " + e(str(exc)))
            return await review_page(ctx, identity, form, {}, 422)
        ctx.say(
            "ok",
            "Saved. The note is still waiting for review and has not been filed."
            + (
                " Speakers are named when the note is marked reviewed, so that question will "
                "be asked again then."
                if kept_for_later
                else ""
            ),
        )
        return ctx.go(here)

    return routes
