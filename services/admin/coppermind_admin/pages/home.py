"""Home and the Setup guide: is it working, what needs you, what is next."""

from __future__ import annotations

import base64
import contextlib
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from coppermind.settings import Wiring
from coppermind.store_protocol import StoreError
from coppermind_admin import model, ui
from coppermind_admin.pages.common import setup_strip
from coppermind_admin.shell import Ctx
from coppermind_admin.ui import e, plural

HOME_LEDE = (
    "A quick check that Coppermind is working, and the short list of things only you can fix."
)
PLUGIN = Path(__file__).parent.parent / "obsidian_plugin"
PLUGIN_FILES = ("main.js", "manifest.json", "styles.css")
COMPANION_LEDE = (
    "The companion is a small add-on for Obsidian. With it you review notes, see what needs "
    "you and hand in recordings without opening Admin."
)
OBSIDIAN_STEPS = (
    "On your computer or phone open Obsidian, then Settings, then Community plugins, and "
    'switch on "Coppermind". If Obsidian says Restricted mode is on, turn that off first. '
    'If "Coppermind" is not in the list yet, open Settings, then Sync, switch on "Installed '
    'community plugins", and wait a minute.'
)


def pairing_code(code: str) -> str:
    return f"{code[:3]} {code[3:]}" if len(code) == 6 else code


def _tile(to: str, name: str, kind: str, word: str, fact: str) -> str:
    return (
        f'<a class="tile" href="{to}"><div class="tile-n">{name}</div>'
        f'<div class="tile-s">{ui.dot(kind)}{word}</div><div class="tile-f">{fact}</div></a>'
    )


def tiles(view: dict[str, Any], ctx: Ctx) -> str:
    sync, state = view["sync"], model.sync_state(view["sync"])
    change = sync.get("last_change") or {}
    if state == "none":
        sync_tile = ("off", "Not connected", "Needed: step 2 of setup")
    elif state == "paused":
        sync_tile = ("warn", "Paused", "Nothing is sent or received")
    elif state == "signin":
        sync_tile = ("bad", "Stopped", "Needs you to sign in again")
    else:
        sync_tile = (
            "ok",
            "Connected",
            "Last change " + ui.ago(change["at"], ctx.zone)
            if change.get("at")
            else "No change received yet",
        )
    paired = view["companion"]["paired"]
    if not paired and not view["companion"]["placed"]:
        companion = ("off", "Not added", "Needed: step 3 of setup")
        companion_to = "/admin/setup/companion"
    elif not paired:
        companion = ("warn", "Not paired", "No device has it now")
        companion_to = "/admin/apps/pair"
    else:
        used = [app["used"] for app in paired if app["used"]]
        companion = (
            "ok",
            "Connected",
            ("Last used " + ui.ago(max(used), ctx.zone) if used else "Paired, not used yet")
            if len(paired) == 1
            else f"{len(paired)} devices paired",
        )
        companion_to = "/admin/sync"
    sugg = view["sugg"]
    sugg_tile = {
        "none": ("off", "Off", "Optional"),
        "on": (
            "ok",
            "On",
            f"{plural(sugg['week']['count'], 'note')} this week"
            if sugg["week"]["count"]
            else "No notes yet",
        ),
        "paused": (
            "warn",
            "Paused",
            f"{plural(sugg['waiting'], 'note')} waiting" if sugg["waiting"] else "Nothing waiting",
        ),
        "failing": ("bad", "Stopped", "Cannot reach the AI service"),
    }[sugg["state"]]
    trans = view["trans"]
    failed = trans["week"]["failed"]
    trans_tile = {
        "none": ("off", "Off", "Optional"),
        "on": ("warn", "On", f"{plural(failed, 'recording')} failed")
        if failed
        else (
            "ok",
            "On",
            f"{trans['week']['done']} written up this week"
            if trans["week"]["done"]
            else "No recordings yet",
        ),
        "paused": ("warn", "Paused", "Recordings wait"),
        "failing": ("bad", "Stopped", "Cannot reach the service"),
    }[trans["state"]]
    calendar = view["calendar"]
    to_check = len(calendar["conflicts"]) + len(calendar["bad_files"])
    if not calendar["files"]:
        cal_tile = (
            ("warn", f"{to_check} to check", "No calendar yet")
            if to_check
            else (
                "off",
                "No calendar yet",
                "Optional",
            )
        )
    elif to_check:
        cal_tile = ("warn", f"{to_check} to check", plural(calendar["events"], "meeting"))
    else:
        cal_tile = ("ok", "Up to date", plural(calendar["events"], "meeting"))
    return (
        '<div class="tiles tiles--5">'
        + _tile("/admin/sync", "Obsidian Sync", *sync_tile)
        + _tile(companion_to, "Companion in Obsidian", *companion)
        + _tile("/admin/suggestions", "Suggestions", *sugg_tile)
        + _tile("/admin/transcription", "Transcription", *trans_tile)
        + _tile("/admin/calendar", "Calendar", *cal_tile)
        + "</div>"
    )


def notice_text(notice: dict[str, Any]) -> tuple[str, str]:
    reference = e(notice["reference"].rsplit("/", 1)[-1])
    if notice["kind"] == "returned_note":
        return (
            f'"{reference}" was moved in Obsidian, and Coppermind put it back',
            e(notice["reason"]) + " Nothing for you to do.",
        )
    if notice["kind"] == "folder_kept":
        return ("An old month folder was kept", e(notice["reason"]))
    if notice["kind"] == "reserved_type":
        return (f'"{reference}" was filed in the general notes folder', e(notice["reason"]))
    if notice["kind"] == "date_mismatch":
        return (
            f'"{reference}" has two dates that disagree',
            "Coppermind used the one in the date property. " + e(notice["reason"]),
        )
    return (f'"{reference}" is in a different place than Coppermind expected', e(notice["reason"]))


def router(wiring: Wiring) -> APIRouter:
    routes = APIRouter()

    @routes.get("/admin", include_in_schema=False)
    async def home(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        view = await ctx.view()
        if view is None:
            return await ctx.unavailable("Home", HOME_LEDE, "home")
        items = model.attention(view)
        steps = model.setup_steps(view)
        following = model.setup_next(steps)
        if following is None and not view["setup"].get("finished_seen"):
            # Home has now been seen with setup finished, so the step pages stop saying so.
            with contextlib.suppress(StoreError):
                await ctx.act("setup-finished", {})
        bad = any(item.bad for item in items)
        if model.sync_state(view["sync"]) == "none" and following is not None:
            headline = (
                "off",
                "Coppermind is installed. Setup is not finished yet.",
                "Nothing is syncing yet. Two setup steps are needed first: connect Obsidian "
                "Sync, then add the companion to Obsidian.",
            )
        elif items:
            headline = (
                "bad" if bad else "warn",
                "1 thing needs you" if len(items) == 1 else f"{len(items)} things need you",
                ("Something has stopped. " if bad else "")
                + "The list is in order of urgency: start at the top.",
            )
        else:
            headline = ("ok", "Everything is working", "Nothing needs you right now.")
        listed = ""
        for index, item in enumerate(items):
            kind = "primary" if index == 0 else ""
            title = (ui.badge("bad", "Stopped") + " " if item.bad else "") + (
                ui.a(item.title, item.more or item.to) if (item.more or item.to) else item.title
            )
            action = (
                ui.btn(item.button, item.post, kind, item.fields, busy=item.busy)
                if item.post
                else ui.go(item.button, item.to, kind)
            )
            listed += ui.item(title, item.detail, action)
        setup_card = ""
        if following is not None:
            optional = "" if following.needed else " (optional)"
            setup_card = ui.card(
                '<div class="item" style="padding:0"><div class="item-b">'
                f'<div class="item-t">Setup: {model.setup_done(steps)} of {len(steps)} steps '
                f'done</div><div class="item-d">Next: {following.title}{optional}. '
                f'{following.why}</div></div><div class="item-a">'
                + ui.go(
                    "Continue setup",
                    following.to if following.needed else "/admin/setup",
                    "primary",
                )
                + "</div></div>"
            )
        waiting = view["notes"]["waiting"]
        also = (
            '<div class="item" style="padding:0;border:0"><div class="item-b muted">Also, '
            f"{plural(waiting, 'new note is', 'new notes are')} waiting for your usual review "
            "in Obsidian. That is normal, not a problem.</div>"
            f'<div class="item-a">{ui.go("See which", "/admin/notes?tab=waiting")}</div></div>'
            if waiting
            else ""
        )
        notices = ""
        if view["notices"]:
            notices = ui.card(
                "".join(
                    ui.item(
                        *notice_text(notice),
                        ui.btn("OK, got it", "/admin/notices/dismiss", "", {"id": notice["id"]}),
                    )
                    for notice in view["notices"]
                ),
                "For your information",
            )
        recent = ""
        if view["recent"]:
            recent = (
                f'<div class="{"hide-narrow" if len(items) > 2 else ""}">'
                + ui.card(
                    ui.timeline(
                        [
                            (ui.when(entry["at"], ctx.zone), e(entry["text"]))
                            for entry in view["recent"]
                        ]
                    ),
                    "Recently",
                )
                + "</div>"
            )
        body = f"""{ui.head("Home", HOME_LEDE)}<div class="stack">{ctx.flash()}
<section class="lat-card"><div class="status">{ui.dot(headline[0])}<div>
<div class="status-t">{headline[1]}</div><div class="muted">{headline[2]}</div></div></div>
{f'<div class="status-list">{listed}</div>' if listed else ""}
</section>{setup_card}{tiles(view, ctx)}{also}{notices}{recent}</div>"""
        return await ctx.page("Home", body, "home")

    @routes.post("/admin/notices/dismiss", include_in_schema=False)
    async def dismiss(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        try:
            await ctx.act("notice-dismiss", {"id": form.get("id", "")})
        except StoreError as exc:
            return await ctx.refused(exc, "/admin")
        return ctx.go("/admin")

    # -- setup guide ---------------------------------------------------------------------

    @routes.get("/admin/setup", include_in_schema=False)
    async def setup(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        lede = "Six short steps, in order. Each one opens its own page and brings you back here."
        view = await ctx.view()
        if view is None:
            return await ctx.unavailable("Setup guide", lede, "setup")
        steps = model.setup_steps(view)
        following = model.setup_next(steps)
        done = model.setup_done(steps)
        listed = ""
        for number, step in enumerate(steps, start=1):
            is_next = following is not None and step.key == following.key
            if step.done:
                state = ui.badge("ok", "Done")
            elif step.skipped:
                state = ui.badge("", "Skipped")
            elif is_next:
                state = ui.badge("accent", "Next")
            elif step.needed:
                state = ui.badge("warn", "Needed")
            else:
                state = ui.badge("", "Optional")
            if step.done:
                action = ui.a("Change", step.to) if step.to else ""
            elif is_next:
                action = ui.go("Start" if step.needed else "Set up", step.to, "primary")
                if not step.needed:
                    action += " " + ui.btn(
                        "Skip for now", "/admin/setup/skip", "", {"step": step.key}
                    )
            else:
                action = ui.go("Set up", step.to)
            listed += ui.item(
                f"{number}. {step.title} {state}", step.summary if step.done else step.why, action
            )
        needed_left = sum(1 for step in steps if step.needed and not step.done)
        if following is not None:
            top = (
                f"<p><b>{done} of {len(steps)} done.</b> "
                + (
                    "Steps 2 and 3 are needed before Coppermind is useful. Steps 4 to 6 are "
                    "optional."
                    if needed_left
                    else "The needed steps are done. Steps 4 to 6 are optional: do them now, skip "
                    "them, or come back any time."
                )
                + "</p>"
            )
        else:
            with contextlib.suppress(StoreError):
                await ctx.act("setup-finished", {})
            top = ui.banner(
                "ok",
                "<b>Setup is finished. Coppermind is fully set up.</b> This guide has left "
                "the menu. You can reopen it from Settings. "
                + ui.go("Go to Home", "/admin", "primary"),
            )
        from coppermind_admin.pages.settings import zone_name

        body = f"""{ui.head("Setup guide", lede)}<div class="stack">{ctx.flash()}{top}
{ui.card(listed)}
<p class="muted">Time zone: {e(zone_name(view["timezone"]))}.
{ui.a("Change", "/admin/settings/timezone")}</p></div>"""
        return await ctx.page("Setup guide", body, "setup")

    @routes.post("/admin/setup/skip", include_in_schema=False)
    async def skip(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        try:
            await ctx.act("setup-skip", {"step": form.get("step", "")})
        except StoreError as exc:
            return await ctx.refused(exc, "/admin/setup")
        return ctx.go("/admin/setup")

    # -- add the companion ---------------------------------------------------------------

    @routes.get("/admin/setup/companion", include_in_schema=False)
    async def companion(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        view = await ctx.view()
        if view is None:
            return await ctx.unavailable("Add the companion to Obsidian", COMPANION_LEDE, "setup")
        steps = model.setup_steps(view)
        back = (
            ("/admin/setup", "Setup guide")
            if model.setup_next(steps) is not None
            else ("/admin/apps", "Connected apps")
        )
        placed = view["companion"]["placed"]
        paired = view["companion"]["paired"]
        pairing: dict[str, Any] = {}
        if placed and not paired:
            try:
                pairing = await ctx.act("pairing-start", {})
            except StoreError:
                pairing = {}
        if placed:
            first = ui.badge("ok", "Done") + (
                ' <span class="muted">It reaches your devices with the next sync, whatever '
                "your sync choices are.</span>"
            )
        else:
            first = ui.btn(
                "Add the companion to my vault",
                "/admin/setup/companion/place",
                "primary",
                busy="Adding…",
            )
        if paired:
            third = ui.badge("ok", "Done")
            fourth = (
                ui.badge("ok", "Connected")
                + ' <span class="muted">'
                + e(paired[0]["name"].replace("Obsidian companion", "Obsidian"))
                + " is connected.</span> "
                + ui.go("Pair another device", "/admin/apps/pair?from=setup")
            )
        elif placed and pairing.get("code"):
            third = (
                f'<span class="keybox keybox--code">{pairing_code(pairing["code"])}</span> '
                '<span class="muted">Works for the next 10 minutes.</span> '
                + ui.btn("New code", "/admin/setup/companion/code")
            )
            fourth = (
                '<span class="muted">Waiting for the companion to connect. This page updates '
                "by itself, usually within a few seconds of you typing the code in "
                "Obsidian.</span> " + ui.go("Check now", "/admin/setup/companion")
            )
        else:
            third = '<span class="muted">The code appears here after step 1.</span>'
            fourth = '<span class="muted">Starts after step 1.</span>'
        need_sync = (
            ui.banner(
                "warn",
                "Connect Obsidian Sync first. Coppermind uses it to put the companion in "
                "your vault. " + ui.go("Connect Obsidian Sync", "/admin/sync", "primary"),
            )
            if model.sync_state(view["sync"]) == "none"
            else ""
        )
        body = f"""{ui.head("Add the companion to Obsidian", COMPANION_LEDE, back)}
<div class="stack">{setup_strip(view, "companion")}{ctx.flash()}{need_sync}
{
            ui.card(f'''<ol class="steps-list stack">
<li><b>Put the companion in your vault.</b> {first}</li>
<li><b>Turn it on in Obsidian.</b> <span class="muted">{OBSIDIAN_STEPS}</span></li>
<li><b>Type this pairing code when the companion asks for it:</b> {third}</li>
<li><b>Wait for it to connect.</b> {fourth}</li></ol>''')
        }</div>"""
        waiting = placed and not paired and bool(pairing.get("code"))
        return await ctx.page("Add the companion", body, "setup", refresh=5 if waiting else 0)

    @routes.post("/admin/setup/companion/place", include_in_schema=False)
    async def place(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        target = ctx.back("/admin/setup/companion")
        try:
            await place_companion(ctx, wiring)
        except (StoreError, OSError) as exc:
            return await ctx.refused(exc, target)
        if form.get("again"):
            ctx.say(
                "ok",
                "The companion add-on was put in your vault again, in its newest version. It "
                "reaches each device with the next sync. Paired devices stay paired.",
            )
        return ctx.go(target)

    @routes.post("/admin/setup/companion/code", include_in_schema=False)
    async def new_code(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        await ctx.form()
        try:
            await ctx.act("pairing-start", {"fresh": True})
        except StoreError as exc:
            return await ctx.refused(exc, "/admin/setup/companion")
        return ctx.go(ctx.back("/admin/setup/companion"))

    return routes


async def place_companion(ctx: Ctx, wiring: Wiring) -> None:
    """Hand the companion's files to the store, which writes them into the notes filesystem."""
    files = {name: base64.b64encode((PLUGIN / name).read_bytes()).decode() for name in PLUGIN_FILES}
    await ctx.act("companion-install", {"files": files, "url": wiring.public_url})
