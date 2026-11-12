"""Calendar: add a calendar file, settle a disagreement between two files, find a meeting."""

from __future__ import annotations

import contextlib
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from coppermind.settings import Wiring
from coppermind.store_protocol import StoreError
from coppermind_admin import model, ui
from coppermind_admin.pages.common import setup_strip
from coppermind_admin.pages.incoming import file_dialog, take_in
from coppermind_admin.shell import Ctx
from coppermind_admin.ui import e, plural, segment
from coppermind_admin.uploads import UploadRefused, receive

LEDE = (
    "Your calendar lets Coppermind match a recording or note to the meeting it came from and "
    "the people invited. Calendar details stay on this server."
)
EXPORT_HELP = (
    "Google Calendar: Settings, then Import and export, then Export. Outlook: File, then Save "
    "Calendar. Apple Calendar: File, then Export."
)
MAX_CHOICES = (("10000", "10,000"), ("50000", "50,000"), ("200000", "200,000"))


def meeting_when(stamp: str, ctx: Ctx) -> str:
    """A meeting's time the way a calendar shows it: "Tue, Nov 12, 10:00 am"."""
    moment = ui._local(stamp, ctx.zone) if "T" in stamp else None  # noqa: SLF001
    if moment is not None:
        return moment.strftime("%a, %b ") + f"{moment.day}, {ui.clock(moment)}"
    try:
        from datetime import date

        day = date.fromisoformat(stamp[:10])
    except ValueError:
        return "Time not recorded"
    return day.strftime("%a, %b ") + f"{day.day}, all day"


def router(wiring: Wiring) -> APIRouter:
    routes = APIRouter()

    def add_dialog(ctx: Ctx, folder: str, back: str) -> str:
        return file_dialog(
            "Add a calendar file",
            "Choose the calendar file you exported from your calendar app (its name usually "
            f'ends in .ics). You can also drop one into the "{e(folder)}" folder in your '
            "vault.",
            "/admin/calendar/add",
            back,
            {"back": back},
            accept=".ics,.zip,text/calendar",
        )

    @routes.get("/admin/calendar", include_in_schema=False)
    async def calendar(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        view = await ctx.view()
        query = ctx.q("q").strip()
        try:
            data = await ctx.act("calendar", {"q": query})
        except StoreError:
            return await ctx.unavailable("Calendar", LEDE, "calendar")
        folder = view["drop_folder"] if view else "To Coppermind"
        tab = "files" if ctx.q("tab") == "files" else "meet"
        here = ui.url("/admin/calendar", tab="files" if tab == "files" else "")
        files, bad = data["files"], data["bad_files"]
        dialog = ""
        asked = ctx.q("ask")
        if asked == "add":
            dialog = add_dialog(ctx, folder, here)
        elif asked.startswith("new:"):
            dialog = file_dialog(
                "Add the new calendar file",
                "Choose the calendar file you just exported again (its name usually ends in "
                ".ics). It replaces the damaged one.",
                "/admin/incoming/add",
                here,
                {"replaces": asked[4:], "back": here},
                accept=".ics,.zip,text/calendar",
            )
        damaged = "".join(
            ui.banner(
                "warn",
                f'<b>"{e(item["name"])}" could not be read</b>, so nothing was added from it. '
                "The usual cause is a download that was cut short: export the calendar again "
                "from your calendar app, then add the fresh file. "
                + ui.go("Add the new file", ui.url(here, ask="new:" + item["id"]), "primary"),
            )
            for item in bad
        )
        if not files:
            body = f"""{ui.head("Calendar", LEDE)}<div class="stack">{ctx.flash()}{damaged}
{
                ui.card(
                    ui.empty(
                        "No calendar yet",
                        "Save your calendar as a file from your calendar app, then add it here. "
                        "You can add a newer file any time; Coppermind merges them.",
                        ui.go(
                            "Add a calendar file", ui.url("/admin/calendar", ask="add"), "primary"
                        ),
                    )
                    + f'<p class="lat-hint" style="text-align:center">{EXPORT_HELP}</p>'
                )
            }</div>"""
            return await ctx.page("Calendar", body, "calendar", dialog=dialog)
        conflicts = ""
        for conflict in data["conflicts"]:
            stored, other = conflict["stored"], conflict["incoming"]
            fields = {"id": conflict["id"], "version": conflict["version"]}
            if other is None:
                conflicts += ui.card(
                    '<p class="muted">Two of your calendar files gave different details for '
                    "this meeting, and the other version can no longer be read from its file. "
                    "Coppermind is using the one below. If it is wrong, export the calendar "
                    "again and add the fresh file.</p>"
                    f'<div class="cmp"><div class="k">In "{e(stored["file"])}"</div>'
                    f"<b>{e(conflict['title'])}</b><div>{meeting_when(stored['start'], ctx)}"
                    '</div><div class="actions">'
                    + ui.btn(
                        "Keep this one", "/admin/calendar/choose", "", {**fields, "pick": "stored"}
                    )
                    + "</div></div>",
                    f'Two calendar files disagree about "{e(conflict["title"])}"',
                )
                continue
            differs = e(conflict["differs"]) or "the details"

            def side(
                version: dict[str, Any],
                pick: str,
                note: str,
                fields: dict[str, Any] = fields,
                title: str = conflict["title"],
            ) -> str:
                return (
                    f'<div class="cmp"><div class="k">In "{e(version["file"])}", added '
                    f"{ui.date_only(version['added'], ctx.zone)}.{note}</div>"
                    f"<b>{e(version['title'] or title)}</b>"
                    f"<div><mark>{meeting_when(version['start'], ctx)}</mark></div>"
                    '<div class="actions">'
                    + ui.btn("Use this one", "/admin/calendar/choose", "", {**fields, "pick": pick})
                    + "</div></div>"
                )

            conflicts += ui.card(
                '<p class="muted">Normally a newer file simply updates a meeting. Here both '
                "files say the meeting was last changed at the same moment, so Coppermind "
                f"cannot tell which is right. What differs: {differs}. Not sure? Look at the "
                "meeting in your calendar app: whichever it shows there is the right one.</p>"
                '<div class="two">'
                + side(stored, "stored", " Coppermind is using this one for now.")
                + side(other, "incoming", "")
                + "</div>",
                f'Two calendar files disagree about "{e(conflict["title"])}"',
            )
        if tab == "meet":
            lines = "".join(
                f"<tr><td>{ui.a(e(event['title']), '/admin/calendar/' + segment(event['id']))}"
                f"</td><td>{meeting_when(event['start'], ctx)}</td>"
                f'<td class="hide-narrow">{event["invited"]} invited</td></tr>'
                for event in data["events"]
            )
            inner = (
                '<p class="muted">Meetings come from your calendar files. To change one, '
                "change it in your calendar app and add a fresh file here.</p>"
                '<form method="get" action="/admin/calendar">'
                + ui.text_input("q", "Find a meeting", query, live=True, autofocus=bool(query))
                + "</form>"
                + (
                    f'<table class="lat-table"><tbody>{lines}</tbody></table>'
                    if lines
                    else ui.empty("No meeting with that name" if query else "No meetings yet", "")
                )
            )
        else:
            rows = [
                [
                    e(item["name"]),
                    ui.date_only(item["added"], ctx.zone),
                    f"{item['has']:,}",
                    f"{item['adds']:,}",
                ]
                for item in files
            ]
            inner = (
                ui.table(["File", "Added", "Meetings in the file", "New to Coppermind"], rows)
                + '<div class="actions">'
                + ui.go("Add a calendar file", ui.url(here, ask="add"))
                + '</div><p class="lat-hint" style="margin-top:8px">Meetings that appear in '
                "more than one file are counted once. Coppermind keeps up to "
                + ui.quick_select(
                    "/admin/calendar/max",
                    "max_events",
                    "Most meetings kept",
                    [
                        *MAX_CHOICES,
                        *(
                            []
                            if str(data["max_events"]) in dict(MAX_CHOICES)
                            else [(str(data["max_events"]), f"{data['max_events']:,}")]
                        ),
                    ],
                    str(data["max_events"]),
                )
                + " meetings; a file that would take it past that is refused.</p>"
            )
        open_count = len(data["conflicts"])
        newest = files[0]
        top = ui.status(
            "warn" if open_count else "ok",
            f"{plural(open_count, 'meeting needs', 'meetings need')} your choice"
            if open_count
            else "Calendar is up to date",
            f"{plural(data['total'], 'meeting')} from {plural(len(files), 'calendar file')}. "
            f'Newest: "{e(newest["name"])}", added {ui.date_only(newest["added"], ctx.zone)}.',
            ui.go("Add a calendar file", ui.url(here, ask="add")),
        )
        tabs = ui.seg(
            "/admin/calendar",
            [("meet", "Recent meetings"), ("files", f"Past imports ({len(files)})")],
            tab,
        )
        body = f"""{ui.head("Calendar", LEDE)}<div class="stack">{setup_strip(view, "cal")}
{ctx.flash()}{top}{damaged}{conflicts}{tabs}{ui.card(inner)}</div>"""
        return await ctx.page("Calendar", body, "calendar", dialog=dialog)

    @routes.post("/admin/calendar/add", include_in_schema=False)
    async def add(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        try:
            upload = await receive(request, 200_000_000)
        except UploadRefused as exc:
            ctx.say(
                "bad",
                "That file is larger than 200 MB, the most a calendar file may be."
                if str(exc) == "too large"
                else e(str(exc)),
            )
            return ctx.go("/admin/calendar")
        target = upload.fields.get("back", "")
        target = target if target.startswith("/admin") else "/admin/calendar"
        try:
            ctx.say(*await take_in(ctx, upload))
        except StoreError as exc:
            ctx.say("bad", f'"{e(upload.filename)}" could not be added. {e(str(exc))}')
        finally:
            upload.discard()
        return ctx.go(target)

    @routes.post("/admin/calendar/choose", include_in_schema=False)
    async def choose(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        try:
            result = await ctx.act(
                "calendar-choose",
                {
                    "id": form.get("id", ""),
                    "version": form.get("version", ""),
                    "pick": form.get("pick", ""),
                },
            )
        except StoreError as exc:
            return await ctx.refused(exc, "/admin/calendar")
        with contextlib.suppress(StoreError):
            chosen = meeting_when(result["start"], ctx)
            await ctx.act("setup-log", {"text": f'You chose {chosen} for "{result["title"]}".'})
        ctx.say(
            "ok",
            f'Done. "{e(result["title"])}" is now set to {meeting_when(result["start"], ctx)}, '
            f'as in "{e(result["file"])}". Coppermind will not ask about this meeting again '
            "unless a newer file changes it.",
        )
        return ctx.go("/admin/calendar")

    @routes.post("/admin/calendar/max", include_in_schema=False)
    async def maximum(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        try:
            result = await ctx.act("calendar-max", {"max_events": int(form.get("max_events") or 0)})
        except (StoreError, ValueError) as exc:
            return await ctx.refused(exc, "/admin/calendar?tab=files")
        ctx.say("ok", f"Saved. Coppermind now keeps up to {result['max_events']:,} meetings.")
        return ctx.go("/admin/calendar?tab=files")

    @routes.get("/admin/calendar/{identity}", include_in_schema=False)
    async def meeting(identity: str, request: Request) -> Response:
        ctx = Ctx(request, wiring)
        try:
            data = await ctx.act("calendar-meeting", {"id": identity})
        except StoreError:
            return ctx.go("/admin/calendar")
        invited = data["invited"]
        names = ", ".join(
            e(person["name"] or person["address"].removeprefix("mailto:"))
            for person in invited[:12]
        )
        more = f" and {len(invited) - 12} more" if len(invited) > 12 else ""
        notes = data["notes"]
        matched = (
            ui.sentence_list(
                [ui.a(e(note["title"]), model.note_path(note["id"])) for note in notes]
            )
            + "."
            if notes
            else "Nothing yet."
        )
        rows = ui.rows(
            ui.row(
                "When",
                meeting_when(data["start"], ctx) + (" (cancelled)" if data["cancelled"] else ""),
            ),
            ui.row(
                "Invited",
                f"{plural(len(invited), 'person', 'people')}"
                + (f": {names}{more}" if invited else "")
                + ". Being invited does not mean they came.",
            ),
            ui.row(
                "Found in",
                ui.sentence_list([f'"{e(name)}"' for name in data["files"]])
                or "A calendar file added before Coppermind kept track of files.",
            ),
            ui.row("Matched to", matched),
        )
        body = f"""{
            ui.head(
                e(data["title"]),
                "A meeting from your calendar, and what Coppermind has matched to it.",
                ("/admin/calendar", "Calendar"),
            )
        }
<div class="stack">{ui.card(rows)}
<p class="lat-hint">Meetings cannot be changed here. Change it in your calendar app and add a
fresh calendar file.</p></div>"""
        return await ctx.page(data["title"], body, "calendar")

    return routes
