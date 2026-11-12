"""AI services: the one place a server or account is added, tested, edited and removed.

Suggestions, Transcription and Search by meaning only choose one from this list.
A side trip here from one of those pages carries where it came from, so Back
returns to what the person was doing.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from coppermind.settings import Wiring
from coppermind.store_protocol import StoreError
from coppermind_admin import ui
from coppermind_admin.pages.common import (
    FEATS,
    JOB,
    can,
    caps_text,
    down_help,
    health_badge,
    outside_note,
    service_path,
    since,
    support_detail,
    tested,
    used_text,
    where_text,
)
from coppermind_admin.shell import Ctx
from coppermind_admin.ui import e, plural, segment

LEDE = (
    "The AI servers and accounts Coppermind can use. Each one is added once here, and edited, "
    "tested and removed here. Suggestions, Transcription and Search by meaning then pick from "
    "this list."
)
OFF_TEXT = {
    "sugg": "New notes get no suggestions until you turn them on again.",
    "trans": "Recordings are kept but not written up until you turn it on again.",
    "search": "Search goes back to exact words only.",
}


def trip(ctx: Ctx) -> dict[str, str]:
    """The side trip this request is part of: which feature it came from, and why."""
    feature = ctx.q("for")
    return {
        "for": feature if feature in FEATS else "",
        "pick": "1" if ctx.q("pick") == "1" and feature in FEATS else "",
        "rm": ctx.q("rm"),
    }


def resumed_text(result: dict[str, Any], service: dict[str, Any]) -> str:
    """What restarted because a service answered again."""
    resumed = result.get("resumed") or {}
    out = []
    if "sugg" in service["used_by"]:
        notes = resumed.get("notes", 0)
        out.append(
            "Suggestions are back on"
            + (
                f", and the {plural(notes, 'note that was', 'notes that were')} waiting "
                f"{'is' if notes == 1 else 'are'} being done now"
                if notes
                else ""
            )
            + "."
        )
    if "trans" in service["used_by"]:
        recordings = resumed.get("recordings", 0)
        out.append(
            "Transcription is back on"
            + (
                f", and the {plural(recordings, 'recording that was', 'recordings that were')} "
                f"waiting {'is' if recordings == 1 else 'are'} being written up now"
                if recordings
                else ""
            )
            + "."
        )
    return " ".join(out)


def test_message(
    result: dict[str, Any], *, was_down: bool, on_service_page: bool
) -> tuple[str, str]:
    service = result["service"]
    name = e(service["name"])
    if not result["answering"]:
        if result.get("key_refused"):
            return "bad", (
                f"Tested just now: {name} answered, but refused the key. Replace the key"
                + (" below" if on_service_page else "")
                + " and test again."
            )
        return "bad", (
            f"Tested just now: still no answer from {name}. Start it again and test again. "
            "If its address or key has changed, edit it"
            + (" below" if on_service_page else "")
            + "."
        )
    back = resumed_text(result, service) if was_down else ""
    return "ok", (
        f"Tested just now: {name} is answering. It can do: {caps_text(service)}."
        + (" " + back if back else "")
    )


def text_row(service: dict[str, Any]) -> str:
    working = [m for m in service["text"]["models"] if m["ok"]]
    if working:
        models = ", ".join(f"{e(m['name'])} ({plural(m['secs'], 'second')})" for m in working)
        text = f"Yes. {plural(len(working), 'model')} worked on a made-up meeting: {models}."
    else:
        text = "No. It did not answer a request for text."
    if can(service, "text") and not can(service, "embed"):
        text += " It has no model that search by meaning can use."
    elif can(service, "embed") and not can(service, "text"):
        text = "Suggestions: no. Search by meaning: yes, with " + e(service["embed"]["model"]) + "."
    label = (
        "Suggestions and search by meaning"
        if can(service, "embed") or not can(service, "text")
        else "Suggestions"
    )
    return ui.row(label, text)


def speech_row(service: dict[str, Any]) -> str:
    speech = service["speech"]
    if not speech["ok"]:
        return ui.row(
            "Transcription", "No. " + e(speech.get("reason") or "It does not accept recordings.")
        )
    apart = (
        "and told the two speakers apart"
        if speech.get("speakers", 0) >= 2
        else "and named the speaker, though it heard one voice where the sample has two"
    )
    return ui.row(
        "Transcription",
        f"Yes. It wrote up a made-up 10-second recording in "
        f"{plural(speech.get('secs', 1), 'second')} {apart}.",
    )


def router(wiring: Wiring) -> APIRouter:
    routes = APIRouter()

    async def listing(ctx: Ctx) -> dict[str, Any]:
        return await ctx.act("services", {})

    def back_link(ctx: Ctx, default: tuple[str, str]) -> tuple[str, str]:
        going = trip(ctx)
        if going["for"]:
            return FEATS[going["for"]]["to"], FEATS[going["for"]]["name"]
        return default

    @routes.get("/admin/services", include_in_schema=False)
    async def services(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        going = trip(ctx)
        try:
            data = await listing(ctx)
        except StoreError:
            return await ctx.unavailable("AI services", LEDE, "services")
        carry = {"for": going["for"], "pick": going["pick"]}
        rows = ""
        for service in data["services"]:
            here = service_path(service, **carry)
            state = (
                f"Not answering since {since(service, ctx.zone)}."
                if not service["answering"]
                else f"Last tested {tested(service, ctx.zone)}."
            )
            actions = (
                ui.btn(
                    "Test",
                    f"/admin/services/{segment(service['id'])}/test",
                    "" if service["answering"] else "primary",
                    {"back": ui.url("/admin/services", **carry)},
                    busy="Testing…",
                )
                + ui.go("Edit", here)
                + ui.go(
                    "Remove",
                    ui.url(here, ask="remove")
                    if not service["used_by"]
                    else f"/admin/services/{segment(service['id'])}/remove",
                )
            )
            rows += ui.item(
                ui.a(e(service["name"]), here) + " " + health_badge(service),
                f"{where_text(service)} Can do: {caps_text(service)}. "
                f"Used for: {used_text(service)}. {state}",
                actions,
            )
        if not rows:
            rows = ui.empty(
                "No AI services yet",
                "Suggestions, transcription and search by meaning each need one. Add your own "
                "server, or an OpenAI or Anthropic account.",
            )
        back = back_link(ctx, ("", "")) if going["for"] else None
        body = f"""{ui.head("AI services", LEDE, back)}<div class="stack">{ctx.flash()}
<div class="actions actions--top">
{ui.go("Add a service", ui.url("/admin/services/new", **carry), "primary")}</div>
{ui.card(rows)}</div>"""
        return await ctx.page("AI services", body, "services")

    # -- add -----------------------------------------------------------------------------

    def add_form(ctx: Ctx, values: dict[str, str], errors: dict[str, str], suggested: str) -> str:
        going = trip(ctx)
        kind = values.get("kind", "own")
        cancel = (
            f"/admin/services/{segment(going['rm'])}/remove"
            if going["rm"]
            else FEATS[going["for"]]["to"]
            if going["for"]
            else "/admin/services"
        )
        own = (
            ui.text_input(
                "address",
                "Address of your server",
                values.get("address", ""),
                hint="For example 192.168.1.20:8000 or ai.home.example. Type it the way you "
                "have it; Coppermind works out the rest. If one server does both suggestions "
                "and recordings, enter it once.",
                error=errors.get("address", ""),
            )
            + ui.text_input(
                "key_own",
                "Key",
                kind="password",
                hint="Leave empty unless your server was set up with one. If it needs one, "
                "Coppermind tells you when it connects.",
                error=errors.get("key", "") if kind == "own" else "",
                autocomplete="off",
            )
            + ui.text_input(
                "name",
                "Name, so you recognise it later",
                values.get("name", suggested),
                error=errors.get("name", ""),
            )
        )
        account = {
            provider: ui.text_input(
                "key_" + provider,
                f"Your {label} API key",
                kind="password",
                hint=f'{label} calls it an "API key". Make one at {site}, under API keys. '
                "It is stored on this server only.",
                error=errors.get("key", "") if kind == provider else "",
                autocomplete="off",
            )
            for provider, label, site in (
                ("openai", "OpenAI", "platform.openai.com"),
                ("anthropic", "Anthropic", "console.anthropic.com"),
            )
        }
        return f"""<form method="post" action="{e(ui.url("/admin/services/new", **going))}">
<h3>What kind of service is it?</h3>
{
            ui.choice(
                "kind",
                "own",
                "My own server",
                "A server on your own network. Nothing leaves it.",
                kind == "own",
            )
        }
{
            ui.choice(
                "kind",
                "openai",
                "OpenAI",
                "Your OpenAI account. What you use it for is sent outside your network, and OpenAI "
                "charges your account for each use.",
                kind == "openai",
            )
        }
{
            ui.choice(
                "kind",
                "anthropic",
                "Anthropic",
                "Your Anthropic account. What you use it for is sent outside your network, and "
                "Anthropic charges your account for each use.",
                kind == "anthropic",
            )
        }
<div data-if="kind=own">{own}</div>
<div data-if="kind=openai">{account["openai"]}</div>
<div data-if="kind=anthropic">{account["anthropic"]}</div>
<p class="muted">Coppermind then tests it with a made-up meeting and a made-up 10-second
recording, to find out whether it can write suggestions, write up recordings, or both.
Nothing of yours is sent.</p>
<div class="actions">{ui.submit("Connect and test", busy="Connecting and testing…")}
{ui.go("Cancel", cancel)}</div></form>"""

    def add_back(ctx: Ctx, services: list[dict[str, Any]]) -> tuple[str, str]:
        going = trip(ctx)
        if going["rm"]:
            name = next((s["name"] for s in services if s["id"] == going["rm"]), "the service")
            return (
                f"/admin/services/{segment(going['rm'])}/remove",
                f'removing "{e(name)}"',
            )
        return back_link(ctx, ("/admin/services", "AI services"))

    @routes.get("/admin/services/new", include_in_schema=False)
    async def new(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        lede = (
            "Enter it once. Coppermind connects, finds out what it can do, and then any "
            "feature can use it."
        )
        try:
            data = await listing(ctx)
        except StoreError:
            return await ctx.unavailable("Add an AI service", lede, "services")
        body = f"""{ui.head("Add an AI service", lede, add_back(ctx, data["services"]))}
<div class="stack">{ctx.flash()}{ui.card(add_form(ctx, {}, {}, data["suggested_name"]))}</div>"""
        return await ctx.page("Add an AI service", body, "services")

    @routes.post("/admin/services/new", include_in_schema=False)
    async def add(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        going = trip(ctx)
        kind = form.get("kind", "own")
        payload = {
            "kind": kind,
            "address": form.get("address", ""),
            "name": form.get("name", ""),
            "key": form.get("key_" + kind, ""),
        }
        try:
            result = await ctx.act("service-add", payload)
            data = await listing(ctx)
        except StoreError as exc:
            return await ctx.refused(exc, ui.url("/admin/services/new", **going))
        if "refused" in result:
            refusal = result["refused"]
            message = e(refusal["message"])
            existing = (
                '<p class="actions">'
                + ui.go(
                    "Open the service already in your list",
                    "/admin/services/" + segment(refusal["id"]),
                )
                + "</p>"
                if refusal.get("id")
                else ""
            )
            lede = (
                "Enter it once. Coppermind connects, finds out what it can do, and then any "
                "feature can use it."
            )
            body = f"""{ui.head("Add an AI service", lede, add_back(ctx, data["services"]))}
<div class="stack">{
                ui.card(add_form(ctx, form, {refusal["field"]: message}, data["suggested_name"]))
            }{existing}</div>"""
            return await ctx.page("Add an AI service", body, "services", status=422)
        service = result["service"]
        said = (
            f'Added "{e(service["name"])}" and tested it. It is answering, and it can do: '
            f"{caps_text(service)}."
        )
        if going["rm"]:
            ctx.say("ok", said + " It is offered below where it can take over.")
            return ctx.go(f"/admin/services/{segment(going['rm'])}/remove")
        ctx.say("ok", said)
        return ctx.go(service_path(service, **{"for": going["for"], "pick": going["pick"]}))

    # -- one service ---------------------------------------------------------------------

    async def one(ctx: Ctx, identity: str) -> dict[str, Any] | None:
        try:
            return (await ctx.act("service", {"id": identity}))["service"]
        except StoreError:
            return None

    @routes.get("/admin/services/{identity}", include_in_schema=False)
    async def service_page(identity: str, request: Request) -> Response:
        ctx = Ctx(request, wiring)
        service = await one(ctx, identity)
        if service is None:
            return ctx.go("/admin/services")
        return await render_service(ctx, service, {}, {})

    async def render_service(
        ctx: Ctx,
        service: dict[str, Any],
        values: dict[str, str],
        errors: dict[str, str],
        status: int = 200,
    ) -> Response:
        going = trip(ctx)
        carry = {"for": going["for"], "pick": going["pick"]}
        identity = service["id"]
        here = service_path(service, **carry)
        used = service["used_by"]
        feature = FEATS.get(going["for"])
        offer = ""
        if feature and going["pick"]:
            need = feature["need"]
            if going["for"] in used:
                verb = "use" if going["for"] == "sugg" else "uses"
                offer = ui.banner(
                    "info",
                    f"{feature['name']} already {verb} this service. "
                    + ui.go("Back to " + feature["name"], feature["to"], "primary"),
                )
            elif can(service, need) and service["answering"]:
                offer = ui.banner(
                    "info",
                    f'<b>Use "{e(service["name"])}" for {feature["lower"]}?</b>'
                    f"{outside_note(service, need)} "
                    + ui.go(
                        "Use it for " + feature["lower"],
                        ui.url(feature["to"], use=identity),
                        "primary",
                    )
                    + " "
                    + ui.go("Not this one", feature["to"]),
                )
            elif not service["answering"]:
                offer = ui.banner(
                    "warn",
                    f'"{e(service["name"])}" is not answering, so {feature["lower"]} cannot '
                    "use it yet.",
                )
            else:
                offer = ui.banner(
                    "warn",
                    f'"{e(service["name"])}" cannot {JOB[need]}, so {feature["lower"]} cannot '
                    "use it. " + ui.go("Back to " + feature["name"], feature["to"]),
                )
        test = ui.btn(
            "Test now",
            f"/admin/services/{segment(identity)}/test",
            "" if service["answering"] else "primary",
            {"back": here},
            busy="Testing…",
        )
        if service["answering"]:
            top = ui.status(
                "ok",
                "Answering",
                f"Last tested {tested(service, ctx.zone)}. "
                + (
                    "It is outside your network and charges your account for each use."
                    if service["outside"]
                    else "It is on your network, so nothing sent to it leaves."
                ),
                test,
            )
        else:
            names = [FEATS[key]["name"] for key in used]
            stopped = (
                ui.sentence_list(names)
                + (" has" if len(names) == 1 and used[0] != "sugg" else " have")
                + " stopped until it answers. Nothing is lost."
                if names
                else "Nothing is using it."
            )
            top = ui.status(
                "bad",
                f"Not answering since {since(service, ctx.zone)}",
                stopped + down_help(service),
                test,
            )
        key_field = (
            ui.text_input(
                "key",
                "New key" if service["has_key"] else "Key",
                kind="password",
                hint="Replaces the saved key when you save."
                if service["has_key"]
                else "Leave empty unless the server needs one.",
                error=errors.get("key", ""),
                autocomplete="off",
            )
            if ctx.q("key") == "1" or not service["has_key"] or errors.get("key")
            else '<div class="field"><span class="lat-label">Key</span><div class="muted">A '
            "key is saved. It is never shown. "
            + ui.go("Replace key", ui.url(here, key=1))
            + "</div></div>"
        )
        address = (
            ui.text_input(
                "address",
                "Address",
                values.get("address", service["address"]),
                hint="If the server has moved, change this and save. Coppermind tests the "
                "new address at once.",
                error=errors.get("address", ""),
            )
            if service["kind"] == "own"
            else ""
        )
        save_to = e(ui.url(f"/admin/services/{segment(identity)}/save", **carry))
        edit = f"""<form method="post" action="{save_to}">
{ui.hidden({"back": here})}
{ui.text_input("name", "Name", values.get("name", service["name"]), error=errors.get("name", ""))}
{address}{key_field}
<div class="actions">{ui.submit("Save changes", busy="Saving and testing…")}</div></form>"""
        used_links = (
            ", ".join(ui.a(FEATS[key]["name"], FEATS[key]["to"]) for key in used) + "."
            if used
            else "Nothing at the moment."
        )
        does = ui.rows(
            text_row(service), speech_row(service), ui.row("Used for", used_links), one=True
        )
        dialog = ""
        if ctx.q("ask") == "remove" and not used:
            dialog = ui.dialog(
                f'Remove "{e(service["name"])}"?',
                "Its address and key are forgotten. Nothing is using it, so nothing else changes.",
                f"/admin/services/{segment(identity)}/remove",
                "Remove",
                here,
                danger=True,
            )
        remove = (
            f"/admin/services/{segment(identity)}/remove" if used else ui.url(here, ask="remove")
        )
        lede = (
            "Everything about this AI service is on this page: whether it is answering, what "
            "it can do, its address and key, and what uses it."
        )
        body = f"""{
            ui.head(e(service["name"]), lede, back_link(ctx, ("/admin/services", "AI services")))
        }
<div class="stack">{ctx.flash()}{offer}{top}
{"" if service["answering"] else support_detail(service, ctx.zone)}
<div class="two">{ui.card(does, "What it can do")}{ui.card(edit, "Edit")}</div>
<p>{ui.go("Remove this service…", remove, "ghost")}</p></div>"""
        return await ctx.page(service["name"], body, "services", dialog=dialog, status=status)

    @routes.post("/admin/services/{identity}/test", include_in_schema=False)
    async def test(identity: str, request: Request) -> Response:
        ctx = Ctx(request, wiring)
        await ctx.form()
        target = ctx.back("/admin/services/" + segment(identity))
        before = await one(ctx, identity)
        try:
            result = await ctx.act("service-test", {"id": identity})
        except StoreError as exc:
            return await ctx.refused(exc, target)
        ctx.say(
            *test_message(
                result,
                was_down=bool(before and not before["answering"]),
                on_service_page=target.startswith("/admin/services/"),
            )
        )
        return ctx.go(target)

    @routes.post("/admin/services/{identity}/save", include_in_schema=False)
    async def save(identity: str, request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        target = ctx.back("/admin/services/" + segment(identity))
        before = await one(ctx, identity)
        if before is None:
            return ctx.go("/admin/services")
        try:
            result = await ctx.act(
                "service-save",
                {
                    "id": identity,
                    "name": form.get("name", ""),
                    "address": form.get("address", before["address"]),
                    "key": form.get("key", ""),
                },
            )
        except StoreError as exc:
            return await ctx.refused(exc, target)
        if "refused" in result:
            refusal = result["refused"]
            return await render_service(
                ctx, before, form, {refusal["field"]: e(refusal["message"])}, 422
            )
        service = result["service"]
        name = e(service["name"])
        if not result["tested"]:
            ctx.say("ok", f'Saved. This service is now called "{name}" everywhere.')
        elif not result["answering"]:
            ctx.say(
                "bad",
                (
                    f"Saved, but {name} refused the key. Check the key and save again."
                    if result.get("key_refused")
                    else f"Saved, but nothing answers at {e(service['address'])}. Check the "
                    "address and save again."
                    if service["address"]
                    else f"Saved, but {name} did not answer. Check the key and save again."
                ),
            )
        else:
            back = resumed_text(result, service) if not before["answering"] else ""
            ctx.say(
                "ok",
                f"Saved, and tested with the new {'address' if result['moved'] else 'key'}: "
                f"{name} is answering." + (" " + back if back else ""),
            )
        return ctx.go(
            service_path(service, **{"for": trip(ctx)["for"], "pick": trip(ctx)["pick"]})
            if trip(ctx)["for"]
            else "/admin/services/" + segment(identity)
        )

    # -- remove --------------------------------------------------------------------------

    @routes.get("/admin/services/{identity}/remove", include_in_schema=False)
    async def remove_page(identity: str, request: Request) -> Response:
        ctx = Ctx(request, wiring)
        try:
            data = await listing(ctx)
        except StoreError:
            return await ctx.unavailable("Remove a service", LEDE, "services")
        service = next((s for s in data["services"] if s["id"] == identity), None)
        if service is None or not service["used_by"]:
            return ctx.go("/admin/services")
        return await render_remove(ctx, service, data["services"], {}, {})

    async def render_remove(
        ctx: Ctx,
        service: dict[str, Any],
        services: list[dict[str, Any]],
        picked: dict[str, str],
        errors: dict[str, str],
        status: int = 200,
    ) -> Response:
        identity = service["id"]
        cards = ""
        for key in service["used_by"]:
            feature = FEATS[key]
            need = feature["need"]
            others = [
                other
                for other in services
                if other["id"] != identity and can(other, need) and other["answering"]
            ]
            intro = (
                f"Choose what {feature['lower']} should use instead."
                if others
                else f"No other service can {JOB[need]}. To keep {feature['lower']} on, add "
                "another service first."
            )
            choices = ""
            for other in others:
                sub = where_text(other) + outside_note(other, need)
                if key == "sugg":
                    sub += (
                        f" Its recommended model, {e(other['text']['best'])}, will be used; "
                        "you can change it on the Suggestions page."
                    )
                choices += ui.choice(
                    "rm_" + key,
                    other["id"],
                    e(other["name"]) + " " + health_badge(other),
                    sub,
                    picked.get(key) == other["id"],
                )
            choices += ui.choice(
                "rm_" + key,
                "off",
                "Turn " + feature["lower"] + " off",
                OFF_TEXT[key],
                picked.get(key, "" if others else "off") == "off",
            )
            verb = "use" if key == "sugg" else "uses"
            cards += ui.card(
                f'<p class="muted">{intro}</p>{choices}{ui.field_error(errors.get(key, ""))}',
                f"{feature['name']} {verb} this service",
            )
        name = e(service["name"])
        body = f"""{
            ui.head(
                f'Remove "{name}"',
                "This service is in use. Say what takes over, then remove it.",
                ("/admin/services/" + segment(identity), name),
            )
        }
<div class="stack">{ctx.flash()}
<form method="post" action="/admin/services/{segment(identity)}/remove" class="stack">{cards}
<div class="actions">{ui.submit(f'Remove "{name}"', "danger", busy="Removing…")}
{ui.go("Add another service first", ui.url("/admin/services/new", rm=identity))}
{ui.go("Cancel", "/admin/services/" + segment(identity))}</div></form></div>"""
        return await ctx.page("Remove a service", body, "services", status=status)

    @routes.post("/admin/services/{identity}/remove", include_in_schema=False)
    async def remove(identity: str, request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        try:
            data = await listing(ctx)
        except StoreError as exc:
            return await ctx.refused(exc, "/admin/services")
        service = next((s for s in data["services"] if s["id"] == identity), None)
        if service is None:
            return ctx.go("/admin/services")
        picked = {key: form.get("rm_" + key, "") for key in service["used_by"]}
        errors = {key: "Choose one before removing." for key, value in picked.items() if not value}
        if errors:
            return await render_remove(ctx, service, data["services"], picked, errors, 422)
        try:
            result = await ctx.act("service-remove", {"id": identity, "takeover": picked})
        except StoreError as exc:
            return await ctx.refused(exc, f"/admin/services/{segment(identity)}/remove")
        if "refused" in result:
            return await render_remove(
                ctx,
                service,
                data["services"],
                picked,
                {key: e(result["refused"]["message"]) for key in picked},
                422,
            )
        ctx.say(
            "ok",
            f'Removed "{e(result["name"])}". ' + " ".join(e(line) for line in result["done"]),
        )
        return ctx.go("/admin/services")

    return routes
