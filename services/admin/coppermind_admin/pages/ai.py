"""The AI server: one place to choose what transcribes recordings and what writes the summary.

Transcription and the summary are chosen together, from the models the tested servers
offer, so nobody types a model name. The Coppermind page shows the result on one card.
"""

from __future__ import annotations

import contextlib
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from coppermind.settings import Wiring
from coppermind.store_protocol import StoreError
from coppermind_admin import ui
from coppermind_admin.shell import Ctx
from coppermind_admin.ui import e, plural, segment

LEDE = "Choose what turns a recording into text, and what writes the summary of it."
WAITS = (
    ("1800", "30 minutes"),
    ("3600", "1 hour"),
    ("7200", "2 hours"),
    ("14400", "4 hours"),
)


async def read_ai(ctx: Ctx, view: dict[str, Any]) -> dict[str, Any]:
    """What is set for transcription and for the summary, and the server behind each."""
    trans = dict(view["trans"])
    trans.setdefault("model", "")
    trans.setdefault("wait_s", 3600)
    trans.setdefault("attempts", 2)
    with contextlib.suppress(StoreError):
        trans = {**trans, **await ctx.act("transcription", {})}
    writeup = {"connection": "", "model": "", "enabled": False}
    with contextlib.suppress(StoreError):
        writeup = (await ctx.act("writeup-settings", {}))["assignment"]
    by_id = {service["id"]: service for service in view["services"]}
    summary_service = by_id.get(writeup.get("connection", ""))
    return {
        "trans": trans,
        "writeup": writeup,
        "summary_service": summary_service,
        "summary_model": writeup.get("model")
        or ((summary_service or {}).get("text", {}).get("best") or ""),
        "summarizing": bool(writeup.get("enabled") and summary_service),
    }


def transcribe_model(ai: dict[str, Any]) -> str:
    service = ai["trans"].get("service") or {}
    return ai["trans"].get("model") or service.get("speech", {}).get("model", "")


def line(ai: dict[str, Any]) -> tuple[str, str, str]:
    """(dot kind, the one sentence, the one action) for the AI card."""
    trans, service = ai["trans"], ai["trans"].get("service")
    summary_service = ai["summary_service"]
    if trans["state"] == "none" and not ai["summarizing"]:
        return (
            "off",
            "Not connected. Connect an AI server so recordings are transcribed and summarized.",
            ui.go("Connect an AI server", "/admin/ai", "primary"),
        )
    for server in (service, summary_service if ai["summarizing"] else None):
        if server is not None and not server["answering"]:
            return (
                "bad",
                f"{e(server['name'])} is not answering. New recordings wait and are summarized "
                "when it is back. Nothing is lost.",
                ui.btn(
                    "Test again",
                    f"/admin/services/{segment(server['id'])}/test",
                    "primary",
                    {"back": "/admin"},
                    busy="Testing…",
                ),
            )
    if trans["state"] == "failing":
        return (
            "bad",
            "The AI server that transcribes recordings is not answering. New recordings wait "
            "and are done when it is back. Nothing is lost.",
            ui.go("Choose another", "/admin/ai", "primary"),
        )
    if trans["state"] == "paused":
        return (
            "warn",
            "Transcription is paused. New recordings wait until you resume it.",
            ui.btn("Resume", "/admin/ai/resume", "primary", {"back": "/admin"}),
        )
    if trans["state"] == "none":
        return (
            "warn",
            "Recordings are not being transcribed yet. Choose what should transcribe them.",
            ui.go("Choose", "/admin/ai", "primary"),
        )
    if not ai["summarizing"]:
        return (
            "warn",
            "Recordings are transcribed, but nothing writes their summary yet.",
            ui.go("Choose", "/admin/ai", "primary"),
        )
    return "ok", "Working.", ui.go("Change", "/admin/ai")


def card(ai: dict[str, Any]) -> str:
    """The AI card on the Coppermind page: one line, then what is used for each job."""
    kind, said, action = line(ai)
    service, summary_service = ai["trans"].get("service"), ai["summary_service"]
    rows = [ui.row("AI server", f"{ui.dot(kind)} {said}", action)]
    if kind != "off":
        same = service is not None and (
            summary_service is None or summary_service["id"] == service["id"]
        )
        if service is not None and same:
            rows.append(ui.row("Server", e(service["name"])))
        heard = "Nothing chosen"
        if service is not None:
            heard = (e(transcribe_model(ai)) or "Its own model") + (
                "" if same else f" on {e(service['name'])}"
            )
        written = "Nothing chosen"
        if ai["summarizing"] and summary_service is not None:
            written = (e(ai["summary_model"]) or "Its own model") + (
                "" if same else f" on {e(summary_service['name'])}"
            )
        rows.append(ui.row("Transcribes with", heard))
        rows.append(ui.row("Summarizes with", written))
    return ui.card(ui.rows(*rows), "AI")


def transcribing_options(services: list[dict[str, Any]]) -> list[tuple[str, str, bool]]:
    """(value, label, outside) for each model that can transcribe, on each answering server."""
    out = []
    several = len(services) > 1
    for service in services:
        if not service["answering"] or not service["speech"]["ok"]:
            continue
        for model in service["speech"].get("models") or [""]:
            name = model or "its own model"
            label = f"{name} on {service['name']}" if several else name
            out.append((f"{service['id']}|{model}", label, bool(service["outside"])))
    return out


def summarizing_options(services: list[dict[str, Any]]) -> list[tuple[str, str, bool]]:
    out = []
    several = len(services) > 1
    for service in services:
        if not service["answering"] or not service["text"]["ok"]:
            continue
        models = [item["name"] for item in service["text"].get("models", []) if item["ok"]]
        for model in models or [service["text"].get("best", "")]:
            name = model or "its own model"
            label = f"{name} on {service['name']}" if several else name
            out.append((f"{service['id']}|{model}", label, bool(service["outside"])))
    return out


def preselect(options: list[tuple[str, str, bool]], current: str, use: str) -> str:
    """What to show chosen: the server just added, else what is in use, else a server on
    your own network. A server outside your network is never chosen for you."""
    values = [value for value, _, _ in options]
    if use:
        picked = next((value for value in values if value.startswith(use + "|")), "")
        if picked:
            return picked
    if current in values:
        return current
    return next((value for value, _, outside in options if not outside), "")


def router(wiring: Wiring) -> APIRouter:
    routes = APIRouter()

    @routes.get("/admin/ai", include_in_schema=False)
    async def ai_page(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        view = await ctx.view()
        if view is None:
            return await ctx.unavailable("AI server", LEDE, "ai")
        try:
            listed = (await ctx.act("services", {}))["services"]
        except StoreError:
            return await ctx.unavailable("AI server", LEDE, "ai")
        ai = await read_ai(ctx, view)
        use = ctx.q("use")
        service = ai["trans"].get("service")
        heard = transcribing_options(listed)
        written = summarizing_options(listed)
        now_heard = f"{service['id']}|{ai['trans'].get('model', '')}" if service else ""
        sent = ai["summary_service"]
        now_written = f"{sent['id']}|{ai['writeup'].get('model', '')}" if sent else ""
        if not heard and not written:
            options = ui.empty(
                "No AI server yet",
                "Add your own server, or an OpenAI or Anthropic account. Coppermind tests it and "
                "finds out what it can do.",
            )
            form = ui.card(options)
        else:

            def select(name: str, label: str, found: list[tuple[str, str, bool]], now: str) -> str:
                if not found:
                    return (
                        f'<div class="field"><span class="lat-label">{label}</span>'
                        '<div class="muted">None of your servers can do this.</div></div>'
                    )
                chosen = preselect(found, now, use)
                outside = {value for value, _, flagged in found if flagged}
                choices = [("", "Choose…")] if not chosen else []
                choices += [
                    (value, text + (" (outside your network)" if value in outside else ""))
                    for value, text, _ in found
                ]
                return ui.select(name, label, choices, chosen)

            form = ui.card(
                f"""<form method="post" action="/admin/ai/save" class="stack">
{select("heard", "Transcribes with", heard, now_heard)}
{select("written", "Summarizes with", written, now_written)}
<div class="actions">{ui.submit("Save", busy="Saving…")}{ui.go("Cancel", "/admin")}</div></form>"""
            )
        links = ui.go("Add an AI server", ui.url("/admin/services/new", **{"for": "ai", "pick": 1}))
        if listed:
            links += ui.go(
                "Manage AI servers", ui.url("/admin/services", **{"for": "ai", "pick": 1})
            )
        body = f"""{ui.head("AI server", LEDE, ("/admin", "Coppermind"))}
<div class="stack">{ctx.flash()}{form}<div class="actions">{links}</div></div>"""
        return await ctx.page("AI server", body, "ai")

    def chosen(form: dict[str, str], name: str) -> tuple[str, str] | None:
        connection, _, model = form.get(name, "").partition("|")
        return (connection, model) if connection else None

    @routes.post("/admin/ai/save", include_in_schema=False)
    async def save(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        heard, written = chosen(form, "heard"), chosen(form, "written")
        if heard is None and written is None:
            ctx.say("bad", "Choose what transcribes and what summarizes, then save.")
            return ctx.go("/admin/ai")
        try:
            if heard is not None:
                current = await ctx.act("transcription", {})
                payload: dict[str, Any] = {"connection": heard[0], "model": heard[1]}
                if str(current.get("wait_s", "")) in dict(WAITS):
                    payload["wait_s"] = current["wait_s"]
                await ctx.act("transcription-save", payload)
            if written is not None:
                await ctx.act(
                    "writeup-save",
                    {"connection": written[0], "model": written[1], "enabled": True},
                )
                if (await ctx.act("suggestions", {}))["state"] == "none":
                    await ctx.act(
                        "suggestions-save", {"connection": written[0], "model": written[1]}
                    )
        except StoreError as exc:
            return await ctx.refused(exc, "/admin/ai")
        ctx.say("ok", "Saved.")
        return ctx.go("/admin")

    @routes.post("/admin/ai/pause", include_in_schema=False)
    async def pause(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        await ctx.form()
        target = ctx.back("/admin/advanced")
        try:
            await ctx.act("transcription-pause", {"paused": True})
        except StoreError as exc:
            return await ctx.refused(exc, target)
        return ctx.go(target)

    @routes.post("/admin/ai/resume", include_in_schema=False)
    async def resume(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        await ctx.form()
        try:
            await ctx.act("transcription-pause", {"paused": False})
        except StoreError as exc:
            return await ctx.refused(exc, "/admin")
        return ctx.go(ctx.back("/admin"))

    @routes.post("/admin/ai/wait", include_in_schema=False)
    async def wait(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        target = ctx.back("/admin/settings/advanced")
        try:
            result = await ctx.act("transcription-wait", {"seconds": int(form.get("seconds", ""))})
        except (StoreError, ValueError) as exc:
            return await ctx.refused(exc, target)
        ctx.say(
            "ok",
            f"Saved. Coppermind waits up to {dict(WAITS).get(str(result['seconds']), '')} "
            "for one recording.",
        )
        return ctx.go(target)

    @routes.post("/admin/ai/tries", include_in_schema=False)
    async def tries(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        target = ctx.back("/admin/settings/advanced")
        try:
            result = await ctx.act("transcription-tries", {"tries": int(form.get("tries", ""))})
        except (StoreError, ValueError) as exc:
            return await ctx.refused(exc, target)
        ctx.say(
            "ok", f"Saved. Coppermind tries {plural(result['tries'], 'time')} for each recording."
        )
        return ctx.go(target)

    return routes
