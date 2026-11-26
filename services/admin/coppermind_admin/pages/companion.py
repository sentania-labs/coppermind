"""The companion: put it in the vault, pair a device, keep it current."""

from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from coppermind.companion import fingerprint
from coppermind.settings import Wiring
from coppermind.store_protocol import StoreError
from coppermind_admin import model, ui
from coppermind_admin.shell import Ctx
from coppermind_admin.ui import e

PLUGIN = Path(__file__).parent.parent / "obsidian_plugin"
PLUGIN_FILES = ("main.js", "manifest.json", "styles.css")
COMPANION_LEDE = (
    "The companion is a small add-on for Obsidian. With it you review your notes without "
    "opening Admin."
)
OBSIDIAN_STEPS = (
    "On your computer or phone open Obsidian, then Settings, then Community plugins, and "
    'switch on "Coppermind". If Obsidian says Restricted mode is on, turn that off first. '
    'If "Coppermind" is not in the list yet, open Settings, then Sync, switch on "Installed '
    'community plugins", and wait a minute.'
)


def companion_files() -> dict[str, bytes]:
    files = {name: (PLUGIN / name).read_bytes() for name in PLUGIN_FILES}
    manifest = json.loads(files["manifest.json"])
    release = os.environ.get("COPPERMIND_BUILD_VERSION", "")
    match = re.fullmatch(r"v?(\d+\.\d+\.\d+)", release)
    if match:
        manifest["version"] = match.group(1)
    files["manifest.json"] = json.dumps(manifest).encode()
    return files


def companion_needs_update(view: dict[str, Any]) -> bool:
    companion = view["companion"]
    return bool(
        companion["placed"]
        and (
            not companion.get("version")
            or companion.get("fingerprint") != fingerprint(companion_files())
        )
    )


def companion_status(view: dict[str, Any], back: str) -> str:
    companion = view["companion"]
    if not companion["placed"]:
        return ""
    installed = companion.get("version")
    version = json.loads(companion_files()["manifest.json"])["version"]
    if not companion_needs_update(view):
        return f'<span class="muted">Companion version {e(installed)} is current.</span>'
    message = (
        f"The companion in your vault is out of date. You have version {e(installed)}. "
        if installed
        else "Could not read the companion version in your vault. "
    ) + f"The update installs version {e(version)}."
    return (
        ui.badge("warn", "Update available")
        + " "
        + message
        + " "
        + ui.btn(
            "Update the companion",
            "/admin/setup/companion/place",
            "primary",
            {"again": "1", "back": back},
            busy="Updating…",
        )
    )


def pairing_code(code: str) -> str:
    return f"{code[:3]} {code[3:]}" if len(code) == 6 else code


def updated_text(notice: dict[str, Any]) -> str:
    """The companion's own update, said once in the Obsidian line."""
    old = notice.get("from")
    return (
        "Updated"
        + (f" from {e(old)}" if old else "")
        + f" to {e(notice.get('to', ''))}. Reload Obsidian on each device to pick it up."
    )


def router(wiring: Wiring) -> APIRouter:
    routes = APIRouter()

    # -- add the companion ---------------------------------------------------------------

    @routes.get("/admin/setup/companion", include_in_schema=False)
    async def companion(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        view = await ctx.view()
        if view is None:
            return await ctx.unavailable("Add the companion to Obsidian", COMPANION_LEDE, "home")
        back = ("/admin", "Coppermind")
        placed = view["companion"]["placed"]
        paired = view["companion"]["paired"]
        pairing: dict[str, Any] = {}
        if placed and not paired:
            try:
                pairing = await ctx.act("pairing-state", {})
                if not pairing.get("expires_at"):
                    pairing = await ctx.act("pairing-start", {})
            except StoreError:
                pairing = {}
        carried = "community-plugin" in ctx.settings().sync.sync_configs
        if placed:
            first = companion_status(view, "/admin/setup/companion") + (
                ' <span class="muted">It reaches your devices with the next sync.</span>'
                if carried
                else ""
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
        elif placed and pairing.get("expires_at"):
            third = (
                '<span class="muted">That code has run out, or too many wrong codes were '
                "typed. Press New code to get another.</span> "
                + ui.btn("New code", "/admin/setup/companion/code", "primary")
            )
            fourth = '<span class="muted">Starts once you have a new code.</span>'
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
        not_carried = (
            ui.banner(
                "warn",
                "Obsidian Sync only carries the companion to your devices if \u201cYour own "
                "Obsidian settings\u201d is ticked, and it is not. "
                + ui.go("Open What syncs", "/admin/sync/what", "primary"),
            )
            if placed and not carried
            else ""
        )
        body = f"""{ui.head("Add the companion to Obsidian", COMPANION_LEDE, back)}
<div class="stack">{ctx.flash()}{need_sync}{not_carried}
{
            ui.card(f'''<ol class="steps-list stack">
<li><b>Put the companion in your vault.</b> {first}</li>
<li><b>Turn it on in Obsidian.</b> <span class="muted">{OBSIDIAN_STEPS}</span></li>
<li><b>Type this pairing code when the companion asks for it:</b> {third}</li>
<li><b>Wait for it to connect.</b> {fourth}</li></ol>''')
        }</div>"""
        waiting = placed and not paired and bool(pairing.get("code"))
        return await ctx.page("Add the companion", body, "home", refresh=5 if waiting else 0)

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
                "The companion add-on was put in your vault again, in its newest version. "
                + (
                    "It reaches each device with the next sync. "
                    if "community-plugin" in ctx.settings().sync.sync_configs
                    else "It only reaches your devices if \u201cYour own Obsidian settings\u201d "
                    "is ticked under What syncs. "
                )
                + "Reload the companion in Obsidian. Paired devices stay paired.",
            )
        else:
            ctx.say("ok", "The companion is in your vault. Turn it on in Obsidian next.")
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


def place_payload(wiring: Wiring) -> dict[str, Any]:
    files = {name: base64.b64encode(data).decode() for name, data in companion_files().items()}
    return {"files": files, "url": wiring.public_url}


async def place_companion(ctx: Ctx, wiring: Wiring) -> None:
    """Hand the companion's files to the store, which writes them into the notes filesystem."""
    await ctx.act("companion-install", place_payload(wiring))
