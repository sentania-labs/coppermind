"""Connected apps: see what may use Coppermind, connect one, change it, cut one off.

Each app has its own key, so one can be cut off without touching the others.
The Obsidian companion has no key to paste: it pairs with a short code.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from coppermind.api_keys import (
    PERMISSIONS,
    PRESETS,
    add_key,
    change_key,
    describe,
    load_keys,
    remove_key,
    scopes_for,
)
from coppermind.settings import Wiring
from coppermind.statefiles import RevisionConflict
from coppermind.store_protocol import StoreError
from coppermind_admin import ui
from coppermind_admin.pages.home import OBSIDIAN_STEPS, pairing_code
from coppermind_admin.shell import Ctx
from coppermind_admin.ui import e, segment

LEDE = (
    "Apps and assistants that are allowed to use your Coppermind, and what each one may do. "
    "Each has its own key, so you can cut one off without touching the others."
)
KINDS = {
    "companion": ("Obsidian companion", "Obsidian companion on my laptop"),
    "agent": ("An AI assistant or agent", "My assistant"),
    "intake": ("An automation that sends things in", "Intake automation"),
    "custom": ("Something else", "My app"),
}
PASTE = {
    "agent": "In your assistant's settings, add Coppermind and paste this key where it asks "
    "for one.",
    "intake": "In your automation, paste this key where it asks for a Coppermind key.",
    "custom": "Paste it where the app asks for a Coppermind key.",
}


def app_path(identity: str) -> str:
    return "/admin/apps/" + segment(identity)


def key_card(title: str, credential: str, after: str) -> str:
    """A key, shown once, with the one button that copies it."""
    return ui.card(
        f'<h3>{title}</h3><div class="keybox" id="newkey">{e(credential)}</div>'
        '<div class="actions"><button type="button" class="lat-btn lat-btn--primary" '
        'data-copy="newkey">Copy key</button><span class="lat-badge lat-badge--ok" '
        'id="newkey-copied" role="status" hidden>Copied to the clipboard</span></div>' + after
    )


def router(wiring: Wiring) -> APIRouter:
    routes = APIRouter()

    async def apps(ctx: Ctx) -> list[dict[str, Any]]:
        return (await ctx.act("apps", {}))["items"]

    @routes.get("/admin/apps", include_in_schema=False)
    async def listing(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        try:
            items = await apps(ctx)
        except StoreError:
            return await ctx.unavailable("Connected apps", LEDE, "apps")
        rows = ""
        for app in items:
            here = app_path(app["id"])
            title = ui.a(e(app["name"]), here)
            if app["starter"]:
                title += " " + ui.badge("warn", "Created at install")
            if not app["used"]:
                title += " " + ui.badge("", "Waiting for first use")
            detail = f"Can: {app['can']}. " + (
                f"Last used: {ui.ago(app['used'], ctx.zone)}."
                if app["used"]
                else "The key has not been used yet."
            )
            if app["starter"]:
                detail += " Safe to remove once your apps have their own keys."
            rows += ui.item(
                title,
                detail,
                ui.go("Edit", here) + ui.go("Remove access", ui.url("/admin/apps", ask=app["id"])),
            )
        made = (
            ui.banner(
                "ok",
                f'Key created for "{e(ctx.q("made"))}". It shows "Waiting for first use" until '
                "the app first talks to Coppermind; after that you see when it was last used.",
                fresh=True,
            )
            if ctx.q("made")
            else ""
        )
        dialog = ""
        target = next((app for app in items if app["id"] == ctx.q("ask")), None)
        if target:
            dialog = remove_dialog(target, "/admin/apps")
        body = f"""{ui.head("Connected apps", LEDE)}<div class="stack">{ctx.flash()}{made}
<div class="actions actions--top">{ui.go("Connect an app", "/admin/apps/new", "primary")}</div>
{ui.card(rows or ui.empty("No apps connected", "Connect the Obsidian companion or an assistant."))}
<p class="lat-hint">The Obsidian companion on each of your devices is listed here, and can be
removed here or on its device's line under {ui.a("Obsidian Sync", "/admin/sync")}.</p></div>"""
        return await ctx.page("Connected apps", body, "apps", dialog=dialog)

    def remove_dialog(app: dict[str, Any], back: str) -> str:
        return ui.dialog(
            f'Remove access for "{e(app["name"])}"?',
            "Its key stops working at once. Nothing it already did is undone. To let it back "
            "in, connect it again with a new key."
            if not app["companion"]
            else "This companion can no longer review notes or hand in recordings. It stops at "
            "once. To let it back in, pair the device again.",
            f"{app_path(app['id'])}/remove",
            "Remove access" if not app["companion"] else "Unpair companion",
            back,
            danger=True,
        )

    # -- connect an app ------------------------------------------------------------------

    def new_form(values: dict[str, str], errors: dict[str, str]) -> str:
        kind = values.get("kind", "agent")
        choices = ""
        for key, (title, _) in KINDS.items():
            if key == "companion":
                sub = "Connects with a pairing code instead of a key."
            elif key == "custom":
                sub = "You choose what it may do."
            else:
                sub = "Can: " + describe(scopes_for(PRESETS[key])) + "."
            choices += ui.choice("kind", key, title, sub, kind == key)
        ticks = "".join(
            ui.check("can_" + key, label, checked=("can_" + key) in values)
            for key, label, _ in PERMISSIONS
        )
        names = "".join(
            f'<div data-if="kind={key}">'
            + ui.text_input(
                "name_" + key,
                "Name, so you recognise it later",
                values.get("name_" + key, default),
                error=errors.get("name", "") if kind == key else "",
            )
            + "</div>"
            for key, (_, default) in KINDS.items()
            if key != "companion"
        )
        return f"""<form method="post" action="/admin/apps/new">
<h3>What are you connecting?</h3>{choices}
<div data-if="kind=custom"><h3 style="margin-top:10px">What may it do?</h3>
<div class="checks">{ticks}</div>{ui.field_error(errors.get("can", ""))}</div>
<div data-if="kind=companion"><p class="muted">The companion in Obsidian does not need a key.
It connects with a short pairing code that you type into Obsidian.</p>
<div class="actions">{ui.go("Get a pairing code", "/admin/apps/pair?new=1", "primary")}
{ui.go("Cancel", "/admin/apps")}</div></div>
<div data-if="kind=agent|intake|custom">{names}
<div class="actions">{ui.submit("Create key")}{ui.go("Cancel", "/admin/apps")}</div></div>
</form>"""

    @routes.get("/admin/apps/new", include_in_schema=False)
    async def new(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        body = f"""{
            ui.head(
                "Connect an app",
                "Step 1 of 2: say what you are connecting. Coppermind picks what it may do.",
                ("/admin/apps", "Connected apps"),
            )
        }
<div class="stack">{ui.card(new_form({}, {}))}</div>"""
        return await ctx.page("Connect an app", body, "apps")

    @routes.post("/admin/apps/new", include_in_schema=False)
    async def create(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        kind = form.get("kind", "agent")
        if kind == "companion":
            return ctx.go("/admin/apps/pair?new=1")
        if kind not in KINDS:
            kind = "agent"
        name = form.get("name_" + kind, "").strip()
        errors: dict[str, str] = {}
        if not name:
            errors["name"] = "Give it a name so you can recognise it later."
        if kind == "custom":
            permissions = [key for key, _, _ in PERMISSIONS if ("can_" + key) in form]
            if not permissions:
                errors["can"] = "Tick at least one thing it may do."
        else:
            permissions = list(PRESETS[kind])
        if errors:
            body = f"""{
                ui.head(
                    "Connect an app",
                    "Step 1 of 2: say what you are connecting. Coppermind picks what it may do.",
                    ("/admin/apps", "Connected apps"),
                )
            }
<div class="stack">{ui.card(new_form(form, errors))}</div>"""
            return await ctx.page("Connect an app", body, "apps", status=422)
        credential = ""
        for _ in range(3):
            try:
                credential = add_key(
                    ctx.control,
                    name,
                    scopes_for(permissions),
                    if_revision=load_keys(ctx.control).revision,
                )
                break
            except RevisionConflict:
                continue
            except (ValueError, OSError):
                break
        if not credential:
            ctx.say("bad", "The key could not be created just now. Nothing was changed.")
            return ctx.go("/admin/apps/new")
        address = e(wiring.public_url)
        after = f"""<p class="muted" style="margin-top:10px">This is the only time the key is
shown. {PASTE[kind]} It will also ask for this server's address, which is:
<b id="address">{address}</b>
<button type="button" class="lat-btn" data-copy="address">Copy address</button>
<span class="lat-badge lat-badge--ok" id="address-copied" role="status" hidden>Copied to the
clipboard</span></p>
<div class="actions">{ui.go("Done, I have saved it", ui.url("/admin/apps", made=name))}</div>"""
        body = f"""{
            ui.head(
                "Connect an app",
                "Step 2 of 2: give the key to the app.",
                ("/admin/apps", "Connected apps"),
            )
        }
<div class="stack">{ui.banner("ok", f'Key created for "{e(name)}".')}
{key_card("Copy this key into the app now", credential, after)}</div>"""
        return await ctx.page("Connect an app", body, "apps", status=201)

    # -- pair the companion on another device --------------------------------------------

    @routes.get("/admin/apps/pair", include_in_schema=False)
    async def pair(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        origin = ctx.q("from")
        back = {
            "sync": ("/admin/sync", "Obsidian Sync"),
            "setup": ("/admin/setup/companion", "the companion step"),
        }.get(origin, ("/admin/apps", "Connected apps"))
        here = ui.url("/admin/apps/pair", **{"from": origin})
        lede = (
            "For a second computer or a phone. The companion is already in your vault, so that "
            "device has it as soon as it has synced."
        )
        try:
            if ctx.q("new"):
                await ctx.act("pairing-start", {"fresh": True})
                return ctx.go(here)
            view = await ctx.view()
            state = await ctx.act("pairing-state", {})
            if not state["code"] and not state["done"]:
                state = await ctx.act("pairing-start", {"fresh": True})
        except StoreError:
            return await ctx.unavailable("Pair the companion on another device", lede, "apps")
        placed = bool(view and view["companion"]["placed"])
        done = state["done"]
        if done:
            second = ui.badge("ok", "Done")
            third = (
                ui.badge("ok", "Connected")
                + f' <span class="muted">Obsidian on {e(done["device"])} is connected. It now '
                "shows on its line under Obsidian Sync and in Connected apps.</span>"
            )
            after = (
                '<div class="actions">'
                + (
                    ui.go("Back to Obsidian Sync", "/admin/sync", "primary")
                    if origin == "sync"
                    else ui.go("Back to Connected apps", "/admin/apps", "primary")
                )
                + ui.go("Pair one more device", ui.url(here, new=1))
                + "</div>"
            )
        else:
            second = (
                f'<span class="keybox keybox--code">{pairing_code(state["code"])}</span> '
                '<span class="muted">Works for the next 10 minutes.</span> '
                + ui.go("New code", ui.url(here, new=1))
            )
            third = (
                '<span class="muted">Waiting for the new device. This page updates by itself, '
                "usually within a few seconds of you typing the code.</span> "
                + ui.go("Check now", here)
            )
            after = ""
        missing = (
            ui.banner(
                "warn",
                "The companion is not in your vault yet, so a device has nothing to switch on. "
                + ui.btn(
                    "Add the companion to my vault",
                    "/admin/setup/companion/place",
                    "primary",
                    {"back": here},
                    busy="Adding…",
                ),
            )
            if not placed
            else ""
        )
        body = f"""{ui.head("Pair the companion on another device", lede, back)}
<div class="stack">{ctx.flash()}{missing}
{
            ui.card(f'''<ol class="steps-list stack">
<li><b>On the other device, open Obsidian and switch the companion on.</b>
<span class="muted">{OBSIDIAN_STEPS}</span></li>
<li><b>Type this pairing code when the companion asks for it:</b> {second}</li>
<li><b>Wait for it to connect.</b> {third}</li></ol>{after}''')
        }</div>"""
        return await ctx.page("Pair another device", body, "apps", refresh=0 if done else 5)

    # -- one connected app ---------------------------------------------------------------

    @routes.get("/admin/apps/{identity}", include_in_schema=False)
    async def app_page(identity: str, request: Request) -> Response:
        ctx = Ctx(request, wiring)
        try:
            items = await apps(ctx)
        except StoreError:
            return await ctx.unavailable("Connected apps", LEDE, "apps")
        app = next((item for item in items if item["id"] == identity), None)
        if app is None:
            return ctx.go("/admin/apps")
        return await render_app(ctx, app, {}, {})

    async def render_app(
        ctx: Ctx,
        app: dict[str, Any],
        values: dict[str, str],
        errors: dict[str, str],
        status: int = 200,
        new_key: str = "",
    ) -> Response:
        here = app_path(app["id"])
        if app["companion"]:
            may = (
                '<div class="muted">Read and change notes, hand in recordings, search. The '
                "companion always has exactly these.</div>"
            )
        else:
            held = (
                {key for key, _, _ in PERMISSIONS if ("can_" + key) in values}
                if values
                else set(app["permissions"])
            )
            may = (
                '<div class="checks checks--one">'
                + "".join(
                    ui.check("can_" + key, label, checked=key in held)
                    for key, label, _ in PERMISSIONS
                )
                + "</div>"
                + ui.field_error(errors.get("can", ""))
            )
        edit = f"""<form method="post" action="{here}/save">
{ui.text_input("name", "Name", values.get("name", app["name"]), error=errors.get("name", ""))}
<div class="field field--wide"><span class="lat-label">What it may do</span>{may}</div>
<div class="actions">{ui.submit("Save changes")}</div></form>"""
        used = (
            e(ui.ago(app["used"], ctx.zone).capitalize()) + "."
            if app["used"]
            else "Not yet. It shows here the first time the app talks to Coppermind."
        )
        key_row = (
            ui.row(
                "Pairing",
                "Paired with a code, so there is no key to keep. To pair it again, remove it "
                "and pair the device again.",
            )
            if app["companion"]
            else ui.row(
                "Key",
                "A key is in use. It is never shown again. If it may have leaked, replace it: "
                "the app then needs the new one.",
                ui.go("Replace key", ui.url(here, ask="replace")),
            )
        )
        dialog = ""
        asked = ctx.q("ask")
        if asked == "replace" and not app["companion"]:
            dialog = ui.dialog(
                f'Replace the key for "{e(app["name"])}"?',
                "A new key is made and shown once. The old key stops working at once, so the "
                "app stops until you give it the new one.",
                f"{here}/replace",
                "Make a new key",
                here,
            )
        elif asked == "remove":
            dialog = remove_dialog(app, here)
        shown = (
            key_card(
                f'New key for "{e(app["name"])}". Copy it into the app now.',
                new_key,
                '<p class="muted" style="margin-top:8px">This is the only time the key is '
                "shown. The old key has stopped working.</p>"
                f'<div class="actions">{ui.go("Done, I have saved it", here)}</div>',
            )
            if new_key
            else ""
        )
        body = f"""{
            ui.head(
                e(app["name"]),
                "One connected app: what it may do, when it was last used, and its key.",
                ("/admin/apps", "Connected apps"),
            )
        }
<div class="stack">{ctx.flash()}{shown}
<div class="two">{ui.card(edit, "Edit")}
{ui.card(ui.rows(ui.row("Last used", used), key_row, one=True), "Use and key")}</div>
<p>{
            ui.go(
                "Unpair this companion…" if app["companion"] else "Remove access…",
                ui.url(here, ask="remove"),
                "ghost",
            )
        }</p></div>"""
        return await ctx.page(app["name"], body, "apps", dialog=dialog, status=status)

    async def current(ctx: Ctx, identity: str) -> dict[str, Any] | None:
        try:
            return next((item for item in await apps(ctx) if item["id"] == identity), None)
        except StoreError:
            return None

    @routes.post("/admin/apps/{identity}/save", include_in_schema=False)
    async def save(identity: str, request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        app = await current(ctx, identity)
        if app is None:
            return ctx.go("/admin/apps")
        name = form.get("name", "").strip()
        errors: dict[str, str] = {}
        if not name:
            errors["name"] = "Give it a name."
        permissions = [key for key, _, _ in PERMISSIONS if ("can_" + key) in form]
        if not app["companion"] and not permissions:
            errors["can"] = "Tick at least one thing it may do, or remove its access instead."
        if errors:
            return await render_app(ctx, app, form, errors, 422)
        try:
            change_key(
                ctx.control,
                identity,
                name=name,
                scopes=None if app["companion"] else scopes_for(permissions),
            )
        except (ValueError, RevisionConflict, OSError):
            ctx.say("bad", "That could not be saved just now. Nothing was changed.")
            return ctx.go(app_path(identity))
        can = app["can"] if app["companion"] else describe(scopes_for(permissions))
        ctx.say(
            "ok",
            f'Saved. "{e(name)}" may now: {can}.'
            + ("" if app["companion"] else " The change reaches the app at once."),
        )
        return ctx.go(app_path(identity))

    @routes.post("/admin/apps/{identity}/replace", include_in_schema=False)
    async def replace(identity: str, request: Request) -> Response:
        ctx = Ctx(request, wiring)
        await ctx.form()
        app = await current(ctx, identity)
        if app is None or app["companion"]:
            return ctx.go("/admin/apps")
        try:
            credential = change_key(ctx.control, identity, new_secret=True)
        except (ValueError, RevisionConflict, OSError):
            ctx.say("bad", "The key could not be replaced just now. The old key still works.")
            return ctx.go(app_path(identity))
        return await render_app(ctx, app, {}, {}, 201, credential)

    @routes.post("/admin/apps/{identity}/remove", include_in_schema=False)
    async def remove(identity: str, request: Request) -> Response:
        ctx = Ctx(request, wiring)
        await ctx.form()
        try:
            record = remove_key(ctx.control, identity)
        except (ValueError, RevisionConflict, OSError):
            ctx.say("bad", "That access could not be removed just now. Try again.")
            return ctx.go("/admin/apps")
        ctx.say(
            "ok",
            f'Access removed for "{e(record.name)}". '
            + ("It" if record.companion else "Its key")
            + " has stopped working.",
        )
        return ctx.go("/admin/apps")

    return routes
