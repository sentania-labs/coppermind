"""Obsidian Sync: connect in three steps, then pause, resume, sign in again, manage devices.

Passwords typed here go straight to the sync helper in the one request that uses
them. Admin keeps none of them, and neither does the helper beyond the few
minutes between choosing a vault and connecting to it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request as HTTPRequest
from urllib.request import urlopen

from fastapi import APIRouter, Request
from fastapi.responses import Response

from coppermind.api_keys import remove_key
from coppermind.settings import Wiring
from coppermind.statefiles import RevisionConflict
from coppermind.store_protocol import StoreError
from coppermind_admin import model, ui
from coppermind_admin.pages.common import setup_strip
from coppermind_admin.pages.settings import folder_dialog
from coppermind_admin.shell import Ctx
from coppermind_admin.ui import e, segment

LEDE = (
    "Sync is how notes travel between Coppermind and Obsidian on your phone and computer. It "
    "uses your Obsidian Sync subscription."
)
KINDS = (
    ("image", "images", "Images", ""),
    ("audio", "audio", "Audio", "Needed to hand in recordings from your phone."),
    ("video", "video", "Video", "Large. Uses your Obsidian Sync space quickly."),
    ("pdf", "PDFs", "PDFs", ""),
    (
        "unsupported",
        "other files",
        "Other files",
        "Needed for calendar files dropped into the folder for Coppermind.",
    ),
)
CONFIGS = (
    "app",
    "appearance",
    "appearance-data",
    "hotkey",
    "core-plugin",
    "core-plugin-data",
    "community-plugin",
    "community-plugin-data",
)
HELPER_DOWN = (
    "The part of Coppermind that talks to Obsidian is not answering, so nothing was changed. "
    "Wait a minute and try again."
)


class HelperRefused(Exception):
    def __init__(self, body: dict[str, Any]) -> None:
        super().__init__(str(body.get("detail") or body.get("error") or ""))
        self.body = body


def kinds_text(kinds: list[str]) -> str:
    yes = [words for key, words, _, _ in KINDS if key in kinds]
    no = [words for key, words, _, _ in KINDS if key not in kinds]
    return (
        "Notes"
        + (", " + ", ".join(yes) if yes else "")
        + "."
        + (" Not " + " or ".join(no) + "." if no else "")
    )


def router(wiring: Wiring) -> APIRouter:
    routes = APIRouter()
    endpoint = os.environ.get("COPPERMIND_SYNC_URL", "http://obsidian-sync:8092").rstrip("/")

    def call(action: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        request = HTTPRequest(
            endpoint + "/" + action,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Authorization": "Bearer " + wiring.read_internal_token(),
                "Content-Type": "application/json",
            },
            method="GET" if body is None else "POST",
        )
        try:
            # No exception detail, request body or upstream error body reaches HTML unread.
            with urlopen(request, timeout=300) as response:  # noqa: S310
                return json.load(response)
        except HTTPError as exc:
            try:
                refused = json.load(exc)
            except ValueError:
                refused = {}
            raise HelperRefused(refused if isinstance(refused, dict) else {}) from exc

    async def helper(action: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        for _ in range(20):
            try:
                return await asyncio.to_thread(call, action, body)
            except HelperRefused as exc:
                # The helper does one thing at a time; a moment later it is free again.
                if exc.body.get("error") != "control_busy":
                    raise
                await asyncio.sleep(0.5)
        raise OSError("sync helper stayed busy")

    async def status(ctx: Ctx) -> dict[str, Any]:
        """Sync as it is right now, from the helper, or as it last published it."""
        view = await ctx.view()
        published = dict(view["sync"]) if view else {"configured": False}
        try:
            live = await asyncio.wait_for(helper("status"), 4)
        except (OSError, ValueError, HelperRefused, TimeoutError):
            return published
        return {**published, **live}

    # -- the page ------------------------------------------------------------------------

    @routes.get("/admin/sync", include_in_schema=False)
    async def sync(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        view = await ctx.view()
        if view is None:
            return await ctx.unavailable("Obsidian Sync", LEDE, "sync")
        current = await status(ctx)
        if model.sync_state(current) == "none":
            return await connect_page(ctx, current)
        return await connected_page(ctx, view, current)

    async def connect_page(ctx: Ctx, current: dict[str, Any]) -> Response:
        labels = ["Sign in to Obsidian", "Pick your vault", "Connect"]
        device = ctx.settings().sync.device_name
        problem = ctx.q("problem")
        if not current.get("signed_in"):
            step = 1
            code = (
                ui.text_input(
                    "mfa_code",
                    "Sign-in code from your authenticator app",
                    error=problem if ctx.q("mfa") else "",
                    hint="Your Obsidian account asks for this code as well as the password.",
                    autocomplete="one-time-code",
                )
                if ctx.q("mfa")
                else ""
            )
            body = f"""<form method="post" action="/admin/sync/signin" autocomplete="off">
<h3>Sign in to your Obsidian account</h3>
{
                ui.text_input(
                    "email",
                    "Obsidian email",
                    ctx.q("email"),
                    kind="email",
                    error=problem if ctx.q("field") == "email" else "",
                )
            }
{
                ui.text_input(
                    "password",
                    "Obsidian password",
                    kind="password",
                    error=problem if ctx.q("field") == "password" else "",
                    hint="Used once to sign in. Coppermind does not keep your password. If your "
                    "account asks for a sign-in code, you will be asked for it next.",
                )
            }{code}
<div class="actions">{ui.submit("Sign in to Obsidian", busy="Signing in…")}</div></form>"""
        elif not current.get("choice"):
            step = 2
            vaults = current.get("vaults", [])
            picked = ctx.q("vault") or (vaults[0]["id"] if vaults else "__new__")
            choices = ""
            extras = ""
            for vault in vaults:
                locked = vault.get("locked")
                sub = ("Shared with you. " if vault.get("shared") else "") + (
                    "Locked with its own password."
                    if locked
                    else "No password of its own."
                    if locked is False
                    else "It may have a password of its own."
                )
                choices += ui.choice(
                    "vault", vault["id"], e(vault["name"]), sub, picked == vault["id"]
                )
                wrong = problem if ctx.q("field") == "password" and picked == vault["id"] else ""
                if locked:
                    extras += (
                        f'<div data-if="vault={e(vault["id"])}">'
                        + ui.text_input(
                            "password_" + vault["id"],
                            f'Password for "{e(vault["name"])}"',
                            kind="password",
                            error=wrong,
                            hint="Obsidian says this vault is locked with its own password: the "
                            "one you chose in Obsidian when you created the vault. Coppermind "
                            "cannot read the vault without it. If you have forgotten it, nobody "
                            'can recover it; choose "Make a new vault for Coppermind" instead.',
                        )
                        + "</div>"
                    )
                elif locked is None:
                    extras += (
                        f'<div data-if="vault={e(vault["id"])}">'
                        + ui.text_input(
                            "password_" + vault["id"],
                            f'Password for "{e(vault["name"])}", if it has one',
                            kind="password",
                            error=wrong,
                            hint="Leave empty unless you gave this vault its own password in "
                            "Obsidian.",
                        )
                        + "</div>"
                    )
                else:
                    extras += (
                        f'<p class="muted" data-if="vault={e(vault["id"])}">This vault has no '
                        "password of its own, so there is nothing more to enter.</p>"
                    )
            choices += ui.choice(
                "vault",
                "__new__",
                "Make a new vault for Coppermind",
                "Starts empty. You can move notes into it later.",
                picked == "__new__",
            )
            extras += (
                '<div data-if="vault=__new__">'
                + ui.text_input(
                    "new_name",
                    "Name for the new vault",
                    "Coppermind",
                    error=problem if ctx.q("field") == "new_name" else "",
                )
                + "</div>"
            )
            body = f"""<form method="post" action="/admin/sync/vault" autocomplete="off">
<h3>Which vault should Coppermind join?</h3>
<p class="muted">These are the vaults on your Obsidian account.</p>{choices}{extras}
<div class="actions">
<button class="lat-btn" formaction="/admin/sync/back" name="to" value="1">Back</button>
{ui.submit("Continue", busy="Checking…")}</div></form>"""
        else:
            step = 3
            vault = current["choice"]["vault_name"]
            body = f"""<form method="post" action="/admin/sync/connect">
<h3>Ready to connect</h3>
{
                ui.text_input(
                    "device_name",
                    "Name for this server",
                    device,
                    error=problem if ctx.q("field") == "device_name" else "",
                    hint="This is how Coppermind appears in Obsidian's list of devices.",
                )
            }
<p class="muted">Coppermind will add its own folders (Review and a few others) to
"{e(vault)}". Nothing already in the vault is changed.</p>
<div class="actions">
<button class="lat-btn" formaction="/admin/sync/back" name="to" value="2">Back</button>
{ui.submit("Connect and start syncing", busy="Connecting…")}</div></form>"""
        page = f"""{ui.head("Obsidian Sync", LEDE)}<div class="stack">{ctx.flash()}
{
            ui.status(
                "off",
                "Not connected yet",
                "Three short steps. You need an Obsidian Sync subscription.",
            )
        }
{ui.card(ui.stepper(labels, step) + body)}</div>"""
        return await ctx.page("Obsidian Sync", page, "sync")

    async def connected_page(ctx: Ctx, view: dict[str, Any], current: dict[str, Any]) -> Response:
        state = model.sync_state(current)
        vault = e(current.get("vault_name") or "your vault")
        change = current.get("last_change") or {}
        activity = current.get("last_activity_at") or change.get("at")
        settings = ctx.settings().sync
        if state == "on":
            top = ui.status(
                "ok",
                f'Connected to "{vault}"',
                f"Last synced {ui.ago(activity, ctx.zone)}."
                if activity
                else "Connected. Nothing has been sent or received yet.",
                ui.btn("Pause sync", "/admin/sync/pause"),
            )
        elif state == "paused":
            top = ui.status(
                "warn",
                "Sync is paused",
                "Changes made from now on wait here. Nothing is lost; everything goes out when "
                "you resume.",
                ui.btn("Resume sync", "/admin/sync/resume", "primary", busy="Resuming…"),
            )
        else:
            top = ui.status(
                "bad",
                "Sync has stopped",
                (
                    "Obsidian is asking Coppermind to sign in again. This happens when your "
                    "Obsidian password changes or the sign-in runs out."
                    if current.get("needs_signin")
                    else "The sync program keeps stopping. The usual cause is that Obsidian "
                    "wants Coppermind to sign in again, which happens when your Obsidian "
                    "password changes or the sign-in runs out."
                )
                + " Your notes are safe here, but they are not reaching your devices.",
            )
        again = ""
        if state == "signin":
            problem = ctx.q("problem")
            code = (
                ui.text_input(
                    "mfa_code",
                    "Sign-in code from your authenticator app",
                    error=problem if ctx.q("mfa") else "",
                    autocomplete="one-time-code",
                )
                if ctx.q("mfa")
                else ""
            )
            # The helper remembers which account it connected with, so only the password is
            # asked for. An install connected before it kept that is asked for the address once.
            address = (
                ""
                if current.get("account_known") and ctx.q("field") != "email"
                else ui.text_input(
                    "email",
                    "Obsidian email",
                    ctx.q("email"),
                    kind="email",
                    hint="The address of your Obsidian account. Coppermind asks for it this "
                    "once and remembers it for next time.",
                    error=problem if ctx.q("field") == "email" else "",
                )
            )
            again = ui.card(
                f"""<form method="post" action="/admin/sync/again" autocomplete="off">
<h3>Sign in again to restart sync</h3>
{address}
{
                    ui.text_input(
                        "password",
                        "Obsidian password",
                        kind="password",
                        error=problem if ctx.q("field") == "password" else "",
                    )
                }{code}
<div class="actions">{ui.submit("Sign in and restart sync", busy="Signing in…")}</div></form>"""
            )
        if state == "paused":
            received = "Nothing is received while paused." + (
                f" Before the pause: {received_text(change, ctx)}" if change.get("at") else ""
            )
        elif change.get("at"):
            received = received_text(change, ctx)
        else:
            received = (
                "Nothing yet. This fills in the first time you change a note on your phone or "
                "computer."
            )
        if ctx.q("rename"):
            name_row = ui.row(
                "This server's name in Obsidian",
                f"""<form class="act" method="post" action="/admin/sync/rename">
<input class="lat-input inline narrow" name="device_name" value="{e(settings.device_name)}"
aria-label="Name for this server" autofocus>{ui.submit("Save", busy="Saving…")}
{ui.go("Cancel", "/admin/sync")}</form>""",
            )
        else:
            name_row = ui.row(
                "This server's name in Obsidian",
                e(settings.device_name),
                ui.go("Rename", "/admin/sync?rename=1"),
            )
        used = current.get("used_bytes")
        rows = [
            ui.row("Last change received", received),
            ui.row("Vault", f'"{vault}"', ui.go("Use a different vault", "/admin/sync?ask=vault")),
            name_row,
            ui.row(
                "What syncs",
                kinds_text(list(settings.file_types)),
                ui.go("Change", "/admin/sync/what"),
            ),
            ui.row(
                "Space used",
                f"{ui.size(used)} in the vault." if used else "Shown once the first sync finishes.",
            ),
        ]
        if state == "on":
            rows.append(
                ui.row(
                    "Check it end to end",
                    "Send a small test note and look for it on your phone.",
                    ui.btn("Send a test note", "/admin/sync/test", busy="Sending…"),
                )
            )
        paired = {app["device"].casefold(): app for app in view["companion"]["paired"]}
        seen = {device["name"].casefold(): device for device in current.get("devices", [])}
        listed = ""
        for key in [*seen, *(name for name in paired if name not in seen)]:
            device = seen.get(key)
            app = paired.get(key)
            name = e(device["name"] if device else app["device"] if app else "")
            last = (
                f"Last change {ui.ago(device['last_change_at'], ctx.zone)}."
                if device
                else "No change seen from it yet."
            )
            if app:
                talked = (
                    f" Its companion last talked to Coppermind {ui.ago(app['used'], ctx.zone)}."
                    if app["used"]
                    else " Its companion is paired and has not been used yet."
                )
                listed += ui.item(
                    f"{name} {ui.badge('ok', 'Companion paired')}",
                    last + talked,
                    ui.go("Companion details", "/admin/apps/" + segment(app["id"]))
                    + ui.go("Unpair companion", ui.url("/admin/sync", ask="unpair:" + app["id"])),
                )
            else:
                listed += ui.item(
                    f"{name} {ui.badge('', 'No companion')}",
                    last + " Notes sync to it, but it cannot review notes or hand in "
                    "recordings until the companion is paired.",
                    ui.go("Pair companion", "/admin/apps/pair?new=1&from=sync"),
                )
        if not listed:
            listed = ui.empty(
                "No devices seen yet",
                "Open the vault in Obsidian on your phone or computer and change a note. It "
                "shows here within a minute or two.",
            )
        devices = ui.card(
            f'<p class="muted">Phones and computers that open the vault "{vault}". A device '
            f"shows here the first time it changes a note.</p>{listed}"
            '<div class="actions">'
            + ui.go("Pair the companion on another device", "/admin/apps/pair?new=1&from=sync")
            + ui.btn(
                "Put the companion in my vault again",
                "/admin/setup/companion/place",
                "",
                {"again": "1", "back": "/admin/sync"},
                busy="Adding…",
            )
            + '</div><p class="lat-hint" style="margin-top:8px">To add a device, install '
            "Obsidian on it and open the vault. A lost or sold device: unpair its companion "
            "here, which cuts it off from Coppermind. To stop it syncing the vault as well, "
            "sign it out in your Obsidian account. That part only Obsidian can do.</p>",
            "Your devices",
        )
        dialog = ""
        asked = ctx.q("ask")
        if asked == "vault":
            dialog = ui.dialog(
                "Use a different vault?",
                f'Coppermind stops syncing with "{vault}" and takes you through picking another '
                "vault. Nothing is deleted in either vault or here.",
                "/admin/sync/disconnect",
                "Disconnect and pick another",
                "/admin/sync",
                fields={"again": "1"},
                busy="Disconnecting…",
            )
        elif asked == "off":
            dialog = ui.dialog(
                "Disconnect from Obsidian Sync?",
                "Notes stop travelling between Coppermind and your devices. Nothing is deleted "
                "here or in your vault. You can connect again later.",
                "/admin/sync/disconnect",
                "Disconnect",
                "/admin/sync",
                danger=True,
                busy="Disconnecting…",
            )
        elif asked.startswith("unpair:"):
            app = next((a for a in view["companion"]["paired"] if a["id"] == asked[7:]), None)
            if app:
                dialog = ui.dialog(
                    f"Unpair the companion on {e(app['device'])}?",
                    f"The companion on {e(app['device'])} can no longer review notes or hand in "
                    "recordings. It stops at once. Notes keep syncing to the device until you "
                    "sign it out in your Obsidian account. You can pair it again later.",
                    "/admin/sync/unpair",
                    "Unpair companion",
                    "/admin/sync",
                    danger=True,
                    fields={"id": app["id"]},
                )
        page = f"""{ui.head("Obsidian Sync", LEDE)}<div class="stack">
{setup_strip(view, "sync")}{ctx.flash()}{top}{again}{ui.card(ui.rows(*rows))}{devices}
<p>{ui.go("Disconnect from Obsidian Sync…", "/admin/sync?ask=off", "ghost")}</p></div>"""
        return await ctx.page("Obsidian Sync", page, "sync", dialog=dialog)

    def received_text(change: dict[str, Any], ctx: Ctx) -> str:
        name = str(change.get("path", "")).rsplit("/", 1)[-1].removesuffix(".md")
        return (
            f"{ui.ago(change['at'], ctx.zone).capitalize()}"
            + (f", from {e(change['device'])}" if change.get("device") else "")
            + (f': "{e(name)}".' if name else ".")
        )

    # -- connecting ----------------------------------------------------------------------

    def signin_problem(exc: HelperRefused, form: dict[str, str], base: str) -> str:
        body = exc.body
        if body.get("error") == "account_required":
            return ui.url(
                base,
                field="email",
                problem="Enter the email address of your Obsidian account.",
            )
        if body.get("mfa_needed"):
            return ui.url(
                base,
                mfa=1,
                email=form.get("email", ""),
                problem="Type the sign-in code from your authenticator app, then sign in again."
                if not form.get("mfa_code")
                else "That sign-in code was not accepted. Wait for a new one and try again.",
            )
        return ui.url(
            base,
            email=form.get("email", ""),
            field="password",
            problem="Obsidian did not accept that email and password. Check both and try again."
            if "email" in form
            else "Obsidian did not accept that password. Check it and try again.",
        )

    @routes.post("/admin/sync/signin", include_in_schema=False)
    async def signin(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        email, password = form.get("email", "").strip(), form.get("password", "")
        if "@" not in email:
            return ctx.go(
                ui.url(
                    "/admin/sync",
                    field="email",
                    email=email,
                    problem="Enter the email address of your Obsidian account.",
                )
            )
        if not password:
            return ctx.go(
                ui.url(
                    "/admin/sync",
                    field="password",
                    email=email,
                    problem="Enter your Obsidian password.",
                )
            )
        try:
            await helper(
                "login",
                {"email": email, "password": password, "mfa_code": form.get("mfa_code", "")},
            )
        except HelperRefused as exc:
            return ctx.go(signin_problem(exc, form, "/admin/sync"))
        except (OSError, ValueError):
            ctx.say("bad", HELPER_DOWN)
        return ctx.go("/admin/sync")

    @routes.post("/admin/sync/back", include_in_schema=False)
    async def back(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        try:
            if form.get("to") == "2":
                await helper("vault", {"clear": True})
            else:
                await helper("forget-login", {})
        except (OSError, ValueError, HelperRefused):
            ctx.say("bad", HELPER_DOWN)
        return ctx.go("/admin/sync")

    @routes.post("/admin/sync/vault", include_in_schema=False)
    async def vault(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        picked = form.get("vault", "")
        if picked == "__new__":
            name = form.get("new_name", "").strip()
            if not name:
                return ctx.go(
                    ui.url(
                        "/admin/sync",
                        vault=picked,
                        field="new_name",
                        problem="Give the new vault a name.",
                    )
                )
            body: dict[str, Any] = {"new_name": name}
        else:
            body = {"vault_id": picked, "password": form.get("password_" + picked, "")}
        try:
            await helper("vault", body)
        except HelperRefused as exc:
            problems = {
                "password_required": "Enter the password for this vault.",
                "wrong_password": "That is not the password for this vault. Check it and try "
                "again.",
                "name_taken": "You already have a vault with that name. Pick that vault from "
                "the list, or choose another name.",
                "vault_required": "Choose a vault first.",
                "not_signed_in": "The sign-in ran out. Sign in to Obsidian again.",
            }
            field = "new_name" if picked == "__new__" else "password"
            return ctx.go(
                ui.url(
                    "/admin/sync",
                    vault=picked,
                    field=field,
                    problem=problems.get(
                        str(exc.body.get("error")), "That vault could not be used."
                    ),
                )
            )
        except (OSError, ValueError):
            ctx.say("bad", HELPER_DOWN)
        return ctx.go("/admin/sync")

    def save_device_name(ctx: Ctx, name: str) -> bool:
        try:
            ctx.save_settings(lambda doc: doc.setdefault("sync", {}).update(device_name=name))
        except (ValueError, RevisionConflict):
            return False
        return True

    @routes.post("/admin/sync/connect", include_in_schema=False)
    async def connect(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        name = " ".join(form.get("device_name", "").split())
        if not name or not save_device_name(ctx, name):
            return ctx.go(
                ui.url("/admin/sync", field="device_name", problem="Give this server a name.")
            )
        try:
            result = await helper("connect", {"device_name": name})
        except HelperRefused as exc:
            ctx.say(
                "bad",
                "Coppermind could not connect to that vault, so nothing was changed. "
                + e(str(exc.body.get("detail") or ""))
                + " Try again; if it keeps failing, go back and sign in again.",
            )
            return ctx.go("/admin/sync")
        except (OSError, ValueError):
            ctx.say("bad", HELPER_DOWN)
            return ctx.go("/admin/sync")
        vault_name = e(result.get("vault_name") or "your vault")
        with contextlib.suppress(StoreError):
            await ctx.act(
                "setup-log", {"text": f'Obsidian Sync connected to "{result.get("vault_name")}".'}
            )
        ctx.say(
            "ok",
            f'Connected. Coppermind is now syncing with "{vault_name}". To see it from your '
            "side, open Obsidian on your phone or computer: a folder called "
            f"{e(ctx.settings().notes.review_folder)} appears within a minute.",
        )
        return ctx.go("/admin/sync")

    # -- once connected ------------------------------------------------------------------

    async def simple(
        ctx: Ctx, action: str, said: str, body: dict[str, Any] | None = None
    ) -> Response:
        await ctx.form()
        try:
            await helper(action, body or {})
            ctx.say("ok", said)
        except (OSError, ValueError, HelperRefused):
            ctx.say("bad", HELPER_DOWN)
        return ctx.go(ctx.back("/admin/sync"))

    @routes.post("/admin/sync/pause", include_in_schema=False)
    async def pause(request: Request) -> Response:
        return await simple(
            Ctx(request, wiring),
            "pause",
            "Sync is paused. Nothing will be sent or received until you resume.",
        )

    @routes.post("/admin/sync/resume", include_in_schema=False)
    async def resume(request: Request) -> Response:
        return await simple(
            Ctx(request, wiring),
            "resume",
            "Sync is running again. Anything that was waiting is on its way to your devices.",
        )

    @routes.post("/admin/sync/disconnect", include_in_schema=False)
    async def disconnect(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        was = (await status(ctx)).get("vault_name") or "your vault"
        try:
            await helper("disconnect", {})
        except (OSError, ValueError, HelperRefused):
            ctx.say("bad", HELPER_DOWN)
            return ctx.go("/admin/sync")
        ctx.say(
            "ok",
            f'Disconnected from "{e(was)}". Nothing was deleted. Sign in and pick the vault to '
            "use instead."
            if form.get("again")
            else "Disconnected. Nothing was deleted.",
        )
        return ctx.go("/admin/sync")

    @routes.post("/admin/sync/again", include_in_schema=False)
    async def again(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        email, password = form.get("email", "").strip(), form.get("password", "")
        if "email" in form and "@" not in email:
            return ctx.go(
                ui.url(
                    "/admin/sync",
                    field="email",
                    email=email,
                    problem="Enter the email address of your Obsidian account.",
                )
            )
        if not password:
            return ctx.go(
                ui.url(
                    "/admin/sync",
                    field="password",
                    email=email,
                    problem="Enter your Obsidian password.",
                )
            )
        try:
            await helper(
                "signin",
                {"email": email, "password": password, "mfa_code": form.get("mfa_code", "")},
            )
        except HelperRefused as exc:
            return ctx.go(signin_problem(exc, form, "/admin/sync"))
        except (OSError, ValueError):
            ctx.say("bad", HELPER_DOWN)
            return ctx.go("/admin/sync")
        with contextlib.suppress(StoreError):
            await ctx.act("setup-log", {"text": "You signed in to Obsidian again. Sync restarted."})
        ctx.say(
            "ok",
            "Signed in. Sync is running again. Changes that had been waiting on your devices "
            "arrive over the next minute or two.",
        )
        return ctx.go("/admin/sync")

    @routes.post("/admin/sync/rename", include_in_schema=False)
    async def rename(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        name = " ".join(form.get("device_name", "").split())
        if not name or not save_device_name(ctx, name):
            ctx.say("bad", "Give this server a name. Nothing was changed.")
            return ctx.go("/admin/sync?rename=1")
        try:
            result = await helper("configure", {})
            applied = bool(result.get("config_matches"))
        except (OSError, ValueError, HelperRefused):
            applied = False
        ctx.say(
            "ok",
            f'Saved. This server now shows in Obsidian\'s device list as "{e(name)}".'
            if applied
            else f'Saved. This server will show in Obsidian\'s device list as "{e(name)}" once '
            "sync has restarted, within a minute.",
        )
        return ctx.go("/admin/sync")

    @routes.post("/admin/sync/test", include_in_schema=False)
    async def test(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        await ctx.form()
        try:
            result = await ctx.act("test-note", {})
        except StoreError as exc:
            return await ctx.refused(exc, "/admin/sync")
        ctx.say(
            "ok",
            f'Sent a note called "{e(result["name"])}" just now. Open Obsidian on your phone; '
            "it should be at the top of your vault within a minute. Delete it whenever you "
            "like.",
        )
        return ctx.go("/admin/sync")

    @routes.post("/admin/sync/unpair", include_in_schema=False)
    async def unpair(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        try:
            record = remove_key(ctx.control, form.get("id", ""))
        except (ValueError, RevisionConflict, OSError):
            ctx.say("bad", "That companion could not be unpaired just now. Try again.")
            return ctx.go("/admin/sync")
        ctx.say(
            "ok",
            f"Unpaired the companion on {e(record.device or 'that device')}. It is also gone "
            "from Connected apps. Its access has stopped.",
        )
        return ctx.go("/admin/sync")

    # -- what syncs ----------------------------------------------------------------------

    def what_draft(ctx: Ctx) -> dict[str, Any]:
        settings = ctx.settings().sync
        params = ctx.request.query_params
        if "d" in params:
            draft = {
                "kinds": [key for key, *_ in KINDS if key in params],
                "own": "own" in params,
                "conflict": params.get("conflict", settings.conflict_strategy),
                "skip": params.getlist("skip"),
            }
        else:
            draft = {
                "kinds": list(settings.file_types),
                "own": any(item != "community-plugin-data" for item in settings.sync_configs),
                "conflict": settings.conflict_strategy,
                "skip": list(settings.excluded_folders),
            }
        if params.get("set") == "add":
            picked = (
                params.get("new_folder", "").strip()
                if params.get("picked") == "__new__"
                else params.get("picked", "")
            )
            if picked and picked not in draft["skip"]:
                draft["skip"].append(picked)
        if params.get("drop") in draft["skip"]:
            draft["skip"].remove(params.get("drop"))
        return draft

    @routes.get("/admin/sync/what", include_in_schema=False)
    async def what(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        draft = what_draft(ctx)
        from coppermind_admin.shell import csrf_token

        carry = ui.hidden({"d": 1}) + "".join(ui.hidden({"skip": f}) for f in draft["skip"])
        boxes = "".join(
            ui.check(key, label, hint, checked=key in draft["kinds"])
            for key, _, label, hint in KINDS
        ) + ui.check(
            "own",
            "Your own Obsidian settings",
            "Your themes, hotkeys and other add-ons. Most people leave this off. The "
            "Coppermind companion always syncs, whatever you choose here.",
            checked=draft["own"],
        )
        chips = "".join(
            f'<span class="chip">{e(folder)}<button name="drop" value="{e(folder)}" '
            f'aria-label="Stop leaving out {e(folder)}">&times;</button></span>'
            for folder in draft["skip"]
        )
        conflict = ui.select(
            "conflict",
            "If a note is edited in two places at once",
            [("merge", "Merge the edits (recommended)"), ("conflict", "Keep both copies")],
            draft["conflict"],
            hint="Merge: both sets of edits end up in the one note. Keep both copies: the "
            "second edit is saved as a separate file beside the note, for you to sort out.",
        )
        problem = ui.banner("bad", e(ctx.q("problem"))) if ctx.q("problem") else ""
        form = f"""<form method="get" action="/admin/sync/what" class="stack">{carry}{problem}
{ui.card(f'<h3>Kinds of files</h3><div class="checks">{boxes}</div>')}
{
            ui.card(
                conflict
                + f'''<div class="field field--wide"><span class="lat-label">Folders to leave
out of sync</span><div class="chips">{chips}
<button class="lat-btn" name="pick" value="1">Choose a folder…</button></div></div>'''
            )
        }
<div class="actions"><button class="lat-btn lat-btn--primary" formmethod="post"
formaction="/admin/sync/what" name="csrf" value="{csrf_token.get()}" data-busy="Saving…">Save
</button>{ui.go("Cancel", "/admin/sync")}</div></form>"""
        dialog = ""
        if ctx.q("pick"):
            try:
                available = (await ctx.act("folders", {}))["folders"]
            except StoreError:
                available = []
            ticked = "".join(ui.hidden({key: "on"}) for key in draft["kinds"])
            ticked += ui.hidden({"own": "on"}) if draft["own"] else ""
            ticked += ui.hidden({"conflict": draft["conflict"]})
            dialog = (
                folder_dialog(
                    "Choose a folder to leave out of sync",
                    [name for name in available if name not in draft["skip"]],
                    "",
                    action="/admin/sync/what",
                    method="get",
                    extra=carry + ticked + ui.hidden({"set": "add"}),
                    cancel="/admin/sync/what",
                )
                .replace('name="folder"', 'name="picked"')
                .replace('data-if="folder=', 'data-if="picked=')
            )
        page = f"""{
            ui.head(
                "What syncs",
                "Choose what travels between Coppermind and your devices. Notes always sync.",
                ("/admin/sync", "Obsidian Sync"),
            )
        }
<div class="stack">{form}</div>"""
        return await ctx.page("What syncs", page, "sync", dialog=dialog)

    @routes.post("/admin/sync/what", include_in_schema=False)
    async def what_save(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        from urllib.parse import parse_qs

        fields = parse_qs((await request.body()).decode(errors="replace"), keep_blank_values=True)
        kinds = [key for key, *_ in KINDS if key in fields]
        if not kinds:
            return ctx.go(
                ui.url(
                    "/admin/sync/what",
                    problem="Tick at least one kind of file. Obsidian Sync cannot be set to "
                    "sync notes alone.",
                )
            )

        def change(document: dict[str, Any]) -> None:
            section = document.setdefault("sync", {})
            section["file_types"] = kinds
            section["sync_configs"] = list(CONFIGS) if "own" in fields else []
            section["conflict_strategy"] = (
                "conflict" if fields.get("conflict", ["merge"])[-1] == "conflict" else "merge"
            )
            section["excluded_folders"] = [f for f in fields.get("skip", []) if f.strip()]
            # Sync is both ways: the other directions keep filed notes off the devices.
            section["mode"] = "bidirectional"

        try:
            ctx.save_settings(change)
        except (ValueError, RevisionConflict):
            return ctx.go(
                ui.url(
                    "/admin/sync/what",
                    problem="Those choices could not be saved. Check the folder names.",
                )
            )
        try:
            result = await helper("configure", {})
            applied = bool(result.get("config_matches")) or not result.get("configured")
        except (OSError, ValueError, HelperRefused):
            applied = False
        ctx.say(
            "ok",
            (
                "Saved. Sync restarted with your choices: "
                if applied
                else "Saved. Sync picks up your choices within a minute: "
            )
            + kinds_text(kinds),
        )
        return ctx.go("/admin/sync")

    return routes
