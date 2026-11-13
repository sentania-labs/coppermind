"""Suggestions and Transcription: choose a service, turn the feature on, pause it, change it.

Neither page holds an address or a key. Each is three short steps the first time,
and one page of what is set now afterwards. A change is a draft carried in the
address of the page: nothing is saved until the last step's button, and Cancel
simply leaves.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from coppermind.settings import Wiring
from coppermind.store_protocol import StoreError
from coppermind_admin import ui
from coppermind_admin.pages.common import (
    can,
    health_badge,
    service_choices,
    service_path,
    setup_strip,
    since,
    stopped_help,
    support_detail,
    tested,
    where_text,
    where_words,
)
from coppermind_admin.shell import Ctx
from coppermind_admin.ui import e, plural, segment

SUGG_LEDE = (
    "An AI model reads each new note and fills in blanks such as the date, type, people and "
    "tags. You check everything in Obsidian before a note is filed."
)
TRANS_LEDE = (
    "Turns a recording into a written note with each speaker named. The note then waits for "
    "your review like any other."
)
WAITS_SUGG = (("60", "1 minute"), ("120", "2 minutes"), ("300", "5 minutes"), ("600", "10 minutes"))
WAITS_TRANS = (
    ("1800", "30 minutes"),
    ("3600", "1 hour"),
    ("7200", "2 hours"),
    ("14400", "4 hours"),
)


def rounded_wait(choices: tuple[tuple[str, str], ...], seconds: int) -> str:
    """The listed wait to show as chosen: the one in use, or the next longer one."""
    values = [int(value) for value, _ in choices]
    return str(next((value for value in values if value >= seconds), values[-1]))


def wait_words(choices: tuple[tuple[str, str], ...], value: str) -> str:
    return dict(choices).get(value, plural(max(1, round(int(value) / 60)), "minute"))


def fill_checks(data: dict[str, Any], ticked: dict[str, bool] | None = None) -> str:
    chosen = data["fill"] if ticked is None else ticked
    boxes = "".join(
        ui.check(
            "fill_" + option["key"],
            e(option["label"]),
            e(option["hint"]),
            checked=bool(chosen.get(option["key"])),
        )
        for option in data["fill_options"]
    )
    return (
        f'<div class="checks">{boxes}</div><div style="margin-top:8px">'
        + ui.check(
            "descr",
            "Also let the model read meeting descriptions from my calendar",
            "Helps it work out which meeting a recording belongs to. Off unless you tick it.",
            checked=bool(data["descr"]),
        )
        + "</div>"
    )


def fill_text(data: dict[str, Any]) -> str:
    names = [
        option["label"].lower()
        for option in data["fill_options"]
        if data["fill"].get(option["key"])
    ]
    return ", ".join(names) or "nothing yet"


def picked_fill(form: dict[str, str]) -> list[str]:
    return [name.removeprefix("fill_") for name in form if name.startswith("fill_")]


def router(wiring: Wiring) -> APIRouter:
    routes = APIRouter()

    async def services(ctx: Ctx) -> list[dict[str, Any]]:
        return (await ctx.act("services", {}))["services"]

    def find(listed: list[dict[str, Any]], identity: str) -> dict[str, Any] | None:
        return next((service for service in listed if service["id"] == identity), None)

    # ====================================================================================
    # Suggestions
    # ====================================================================================

    @routes.get("/admin/suggestions", include_in_schema=False)
    async def suggestions(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        try:
            data = await ctx.act("suggestions", {})
            listed = await services(ctx)
        except StoreError:
            return await ctx.unavailable("Suggestions", SUGG_LEDE, "suggestions")
        change = ctx.q("change")
        use = ctx.q("use")
        wizard = data["state"] == "none" or change in {"service", "model"} or bool(use)
        if not wizard:
            return await suggestions_now(ctx, data)
        changing = data["state"] != "none"
        model_only = change == "model" and changing
        current = data["service"]["id"] if data["service"] else ""
        chosen_id = use or ctx.q("svc") or (current if model_only else "")
        chosen = find(listed, chosen_id)
        step = 2 if model_only else int(ctx.q("step") or (2 if use else 1))
        error = ""
        if step > 1 and (chosen is None or not can(chosen, "text") or not chosen["answering"]):
            if chosen is None:
                error = "Choose a service first."
            elif not chosen["answering"]:
                error = (
                    f"{e(chosen['name'])} is not answering right now, so it cannot be used. "
                    "Open it under Manage services to test or edit it, or choose another."
                )
            else:
                error = f"{e(chosen['name'])} cannot write suggestions. Choose another."
            step = 1
        carry = {"change": "service" if changing and (not model_only or step == 1) else ""}
        cancel = ui.go("Cancel the change", "/admin/suggestions?cancelled=1") if changing else ""
        labels = ["Choose an AI service", "Pick a model", "Choose what it fills in"]
        if step == 1:
            body = f"""<form method="get" action="/admin/suggestions">
{ui.hidden({"step": 2, **carry})}
<h3>Which AI service should write suggestions?</h3>
{
                service_choices(
                    "svc",
                    "text",
                    "sugg",
                    listed,
                    ctx.zone,
                    current=current,
                    chosen=chosen_id,
                    error=error,
                )
            }
<div class="actions">{ui.submit("Continue") if any(can(s, "text") for s in listed) else ""}
{cancel}</div></form>"""
        elif step == 2:
            assert chosen is not None
            results = chosen["text"]["models"]
            working = [r for r in results if r["ok"]]
            best = chosen["text"]["best"]
            picked = ctx.q("model") or (data["model"] if model_only else "") or best
            if picked not in {r["name"] for r in working}:
                picked = best
            worked = (
                ("Both" if len(results) == 2 else "All" if len(results) > 1 else "It") + " worked."
                if len(working) == len(results)
                else f"{len(working)} of {len(results)} worked."
            )
            choices = "".join(
                ui.choice(
                    "model",
                    r["name"],
                    e(r["name"])
                    + (" " + ui.badge("ok", "Recommended") if r["name"] == best else ""),
                    (
                        f"Worked. Answered in {plural(r['secs'], 'second')}."
                        + (" Fastest of the ones that worked." if r["name"] == best else "")
                    )
                    if r["ok"]
                    else "Did not give a usable answer.",
                    r["name"] == picked,
                )
                for r in working
            ) + "".join(
                f'<p class="muted">{e(r["name"])}: did not give a usable answer, so it cannot be '
                "chosen.</p>"
                for r in results
                if not r["ok"]
            )
            quotes = "".join(
                f'<div class="quote" data-if="model={e(r["name"])}">What {e(r["name"])} wrote '
                f'from the sample: "{e(r["sample"])}"</div>'
                for r in working
                if r.get("sample")
            )
            hint = (
                '<p class="lat-hint">Not sure? Keep the recommended one. A slower model '
                "sometimes writes a little better. You can switch later and nothing is "
                "lost.</p>"
            )
            intro = (
                f'<h3>Which model?</h3><p class="muted">When {e(chosen["name"])} was tested, '
                "Coppermind tried each of its models with a made-up meeting. "
                f"{worked}</p>{choices}{quotes}{hint}"
            )
            if model_only:
                body = f"""<form method="post" action="/admin/suggestions/model">{intro}
<div class="actions">{ui.submit("Save the change")}{cancel}</div></form>"""
            else:
                back = ui.go(
                    "Back", ui.url("/admin/suggestions", step=1, svc=chosen["id"], **carry)
                )
                body = f"""<form method="get" action="/admin/suggestions">
{ui.hidden({"step": 3, "svc": chosen["id"], **carry})}{intro}
<div class="actions">{back}{ui.submit("Continue")}{cancel}</div></form>"""
        else:
            assert chosen is not None
            model = ctx.q("model")
            if model not in {r["name"] for r in chosen["text"]["models"] if r["ok"]}:
                model = chosen["text"]["best"]
            back = ui.go(
                "Back",
                ui.url("/admin/suggestions", step=2, svc=chosen["id"], model=model, **carry),
            )
            ticked = (
                data["fill"]
                if changing
                else {option["key"]: True for option in data["fill_options"]}
            )
            body = f"""<form method="post" action="/admin/suggestions/save">
{ui.hidden({"svc": chosen["id"], "model": model})}
<h3>What may it fill in?</h3>{fill_checks(data, ticked)}
<p class="muted" style="margin-top:10px">Note text is sent to {e(chosen["name"])}
({where_words(chosen)}). It only fills blanks, only on notes that are still waiting for your
review, and never changes what you wrote. Notes you have already reviewed and filed are never
touched.</p>
<div class="actions">{back}
{ui.submit("Save the change" if changing else "Turn on suggestions", busy="Saving…")}
{cancel}</div></form>"""
        if not changing:
            top = ui.status(
                "off", "Suggestions are off", "Optional. Three short steps to turn them on."
            )
        else:
            using = e(data["model"] or "the usual model")
            top = ui.banner(
                "info",
                f"<b>Changing the model.</b> Nothing changes until you press Save the change. "
                f"Until then suggestions keep using {using}."
                if model_only
                else "<b>Changing the AI service.</b> Nothing changes until you press Save the "
                f"change on the last step. Until then suggestions keep using {using} on "
                f"{e(data['service']['name'] if data['service'] else 'the service in use')}.",
            )
        picked_note = (
            ui.banner("ok", f'"{e(chosen["name"])}" is chosen. Now pick a model.')
            if use and chosen and step == 2
            else ""
        )
        strip = setup_strip(await ctx.view(), "sugg")
        page = f"""{ui.head("Suggestions", SUGG_LEDE)}<div class="stack">{strip}{ctx.flash()}
{picked_note}{top}{ui.card(("" if model_only else ui.stepper(labels, step)) + body)}</div>"""
        return await ctx.page("Suggestions", page, "suggestions")

    async def suggestions_now(ctx: Ctx, data: dict[str, Any]) -> Response:
        service = data["service"]
        state = data["state"]
        view = await ctx.view()
        name = e(service["name"]) if service else "the service"
        model = e(data["model"] or "its usual model")
        using = f"Using {model} on {name}."
        test_action = f"/admin/services/{segment(service['id'])}/test" if service else ""
        here = {"back": "/admin/suggestions"}
        edit = (
            ui.go("Edit this service", service_path(service, **{"for": "sugg"})) if service else ""
        )
        different = ui.go("Use a different service", "/admin/suggestions?change=service")
        if state == "on":
            last = data["last"]
            sub = using + (
                f' Last suggestion {ui.ago(last["at"], ctx.zone)}, for "{e(last["title"])}".'
                if last
                else " No note has had suggestions yet. The next new note will."
            )
            top = ui.status(
                "ok",
                "Suggestions are on",
                sub,
                ui.btn("Pause", "/admin/suggestions/pause", "", {"paused": "1"}),
            )
        elif state == "paused":
            waiting = data["waiting"]
            top = ui.status(
                "warn",
                "Suggestions are paused",
                "New notes wait until you resume. "
                + (
                    plural(waiting, "note is", "notes are") + " waiting."
                    if waiting
                    else "Nothing is waiting yet."
                ),
                ui.btn("Resume", "/admin/suggestions/pause", "primary", {"paused": ""}),
            )
        else:
            waiting = data["waiting"]
            if service is None:
                top = ui.status(
                    "bad",
                    "Suggestions have stopped",
                    "The AI service they used is no longer in your list. "
                    f"{plural(waiting, 'note is', 'notes are')} waiting. Choose another service.",
                    ui.go(
                        "Use a different service", "/admin/suggestions?change=service", "primary"
                    ),
                )
            else:
                top = ui.status(
                    "bad",
                    "Suggestions have stopped",
                    f"{name} has not answered since {since(service, ctx.zone)}. "
                    f"{plural(waiting, 'note is', 'notes are')} waiting."
                    + stopped_help(service, waiting, "note"),
                    ui.btn("Test now", test_action, "primary", here, busy="Testing…")
                    + edit
                    + different,
                )
        older = data["older"]
        rows = [
            ui.row(
                "AI service",
                (
                    f"{name} {health_badge(service)} {where_text(service)}"
                    + (f" Last tested {tested(service, ctx.zone)}." if service["answering"] else "")
                )
                if service
                else "None chosen.",
                ""
                if state == "failing" or not service
                else ui.btn("Test now", test_action, "", here, busy="Testing…") + edit + different,
            ),
            ui.row(
                "Model",
                e(data["model"] or "Its usual model"),
                "" if state == "failing" else ui.go("Change", "/admin/suggestions?change=model"),
            ),
            ui.row(
                "What it fills in",
                (fill_text(data)[0].upper() + fill_text(data)[1:]) + ".",
                ui.go("Change", "/admin/suggestions/fill"),
            ),
            ui.row(
                "How the meeting summary is written",
                (
                    "Your own directions for at least one section."
                    if data.get("summary_custom")
                    else "The usual directions for every section."
                )
                + " Used when a recording is written up.",
                ui.go("Change", "/admin/suggestions/summary"),
            ),
            ui.row(
                "Longest wait for an answer",
                ui.quick_select(
                    "/admin/suggestions/wait",
                    "seconds",
                    "Longest wait for an answer",
                    WAITS_SUGG,
                    rounded_wait(WAITS_SUGG, data["wait_s"]),
                )
                + ' <span class="lat-hint">for one note, before Coppermind gives up and tries '
                "later.</span>",
            ),
        ]
        if older:
            count = len(older)
            failed = sum(1 for note in older if note.get("why") == "failed")
            if not failed:
                reason = (
                    f", because {'it' if count == 1 else 'they'} arrived before "
                    "suggestions were turned on."
                )
            elif failed == count:
                reason = ", because the model's answer could not be used."
            else:
                reason = (
                    ": some arrived before suggestions were turned on, and for others the "
                    "model's answer could not be used."
                )
            text = (
                f"{count:,} of the notes waiting for review "
                f"{'has' if count == 1 else 'have'} no suggestions"
                + reason
                + ("" if state == "on" else " You can run them once suggestions are on again.")
            )
            rows.append(
                ui.row(
                    "Notes without suggestions",
                    text,
                    ui.go(
                        "Suggest for it now" if count == 1 else "Suggest for them now",
                        "/admin/suggestions?ask=older",
                    )
                    if state == "on"
                    else "",
                )
            )
        week = data["week"]
        rows.append(
            ui.row(
                "This week",
                f"{plural(week['count'], 'note')} got suggestions. "
                + (
                    plural(data["waiting"], "is", "are") + " waiting."
                    if data["waiting"]
                    else plural(week["failed"], "failed") + "."
                    if week["failed"]
                    else "None failed."
                )
                if week["count"] or data["waiting"] or week["failed"]
                else "None yet.",
            )
        )
        dialog = ""
        if ctx.q("ask") == "older" and older and service:
            names = ui.sentence_list([f'"{e(note["title"])}"' for note in older[:5]])
            more = f" and {len(older) - 5} more" if len(older) > 5 else ""
            dialog = ui.dialog(
                f"Suggest for {plural(len(older), 'note')}?",
                f"{names}{more}: still waiting for your review, with no suggestions yet. "
                f"{'Its' if len(older) == 1 else 'Their'} text is sent to {name} "
                f"({where_words(service)}). Only blanks are filled. It takes a minute or two.",
                "/admin/suggestions/older",
                "Start",
                "/admin/suggestions",
            )
        cancelled = (
            ui.banner(
                "ok",
                f"Change cancelled. Nothing was changed: suggestions still use {model} on {name}.",
                fresh=True,
            )
            if ctx.q("cancelled")
            else ""
        )
        page = f"""{ui.head("Suggestions", SUGG_LEDE)}<div class="stack">
{setup_strip(view, "sugg")}{ctx.flash()}{cancelled}{top}
{support_detail(service, ctx.zone) if state == "failing" and service else ""}
{ui.card(ui.rows(*rows))}</div>"""
        return await ctx.page("Suggestions", page, "suggestions", dialog=dialog)

    async def paused_note(ctx: Ctx, view: str) -> str:
        try:
            data = await ctx.act(view, {})
        except StoreError:
            return ""
        if data.get("state") != "paused":
            return ""
        return " It is still paused, so nothing is sent until you press Resume."

    async def saved(
        ctx: Ctx, action: str, payload: dict[str, Any], to: str
    ) -> dict[str, Any] | Response:
        try:
            result = await ctx.act(action, payload)
        except StoreError as exc:
            return await ctx.refused(exc, to)
        if "refused" in result:
            ctx.say("bad", e(result["refused"]["message"]))
            return ctx.go(to)
        return result

    @routes.post("/admin/suggestions/save", include_in_schema=False)
    async def suggestions_save(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        result = await saved(
            ctx,
            "suggestions-save",
            {
                "connection": form.get("svc", ""),
                "model": form.get("model", ""),
                "fill": picked_fill(form),
                "descr": "descr" in form,
            },
            "/admin/suggestions",
        )
        if isinstance(result, Response):
            return result
        model, name = e(result["model"]), e(result["service"])
        notes = result["resumed"].get("notes", 0)
        lead = "Saved. " if result["was"] != "none" else "Suggestions are on. "
        ctx.say(
            "ok",
            lead
            + (
                f"The {plural(notes, 'note that was', 'notes that were')} waiting "
                f"{'is' if notes == 1 else 'are'} being done now by {model} on {name}. New "
                "notes will use it too."
                if notes
                else f"The next new note will get suggestions from {model} on {name}."
            )
            + await paused_note(ctx, "suggestions"),
        )
        return ctx.go("/admin/suggestions")

    @routes.post("/admin/suggestions/model", include_in_schema=False)
    async def suggestions_model(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        try:
            data = await ctx.act("suggestions", {})
        except StoreError as exc:
            return await ctx.refused(exc, "/admin/suggestions")
        if not data["service"]:
            return ctx.go("/admin/suggestions")
        result = await saved(
            ctx,
            "suggestions-save",
            {"connection": data["service"]["id"], "model": form.get("model", "")},
            "/admin/suggestions?change=model",
        )
        if isinstance(result, Response):
            return result
        ctx.say(
            "ok",
            f"Saved. The next new note will get suggestions from {e(result['model'])} on "
            f"{e(result['service'])}." + await paused_note(ctx, "suggestions"),
        )
        return ctx.go("/admin/suggestions")

    @routes.post("/admin/suggestions/pause", include_in_schema=False)
    async def suggestions_pause(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        paused = bool(form.get("paused"))
        result = await saved(ctx, "suggestions-pause", {"paused": paused}, "/admin/suggestions")
        if isinstance(result, Response):
            return result
        waiting = result["waiting"]
        ctx.say(
            "ok",
            "Suggestions are paused. Nothing is sent to the AI service until you resume."
            if paused
            else "Suggestions are on again."
            + (
                f" The {plural(waiting, 'waiting note is', 'waiting notes are')} being done now."
                if waiting
                else " Nothing was waiting."
            ),
        )
        return ctx.go("/admin/suggestions")

    @routes.post("/admin/suggestions/wait", include_in_schema=False)
    async def suggestions_wait(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        value = form.get("seconds", "")
        result = await saved(
            ctx, "suggestions-wait", {"seconds": int(value or 0)}, "/admin/suggestions"
        )
        if isinstance(result, Response):
            return result
        ctx.say(
            "ok",
            f"Saved. Coppermind now waits up to {wait_words(WAITS_SUGG, value)} for an answer "
            "about one note.",
        )
        return ctx.go("/admin/suggestions")

    @routes.post("/admin/suggestions/older", include_in_schema=False)
    async def suggestions_older(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        target = ctx.back("/admin/suggestions")
        payload = {"note_ids": [form["note"]]} if form.get("note") else {}
        result = await saved(ctx, "suggestions-older", payload, target)
        if isinstance(result, Response):
            return result
        started, refused = result["started"], result["left_out"]
        if not started:
            why = e(refused[0]["why"]) if refused else "Nothing was waiting."
            ctx.say("warn", f"No note was sent for suggestions. {why}")
        elif form.get("note"):
            ctx.say(
                "ok",
                f'Asked for suggestions on "{e(started[0])}". They appear on the note within a '
                "minute or two. It is still waiting for your review.",
            )
        else:
            ctx.say(
                "ok",
                f"Started on {plural(len(started), 'note')}. This page shows when "
                f"{'it is' if len(started) == 1 else 'they are'} done."
                + (
                    f" {plural(len(refused), 'note was', 'notes were')} left out: "
                    + e(refused[0]["why"])
                    if refused
                    else ""
                ),
            )
        return ctx.go(target)

    @routes.get("/admin/suggestions/fill", include_in_schema=False)
    async def fill(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        lede = (
            "Tick what the model may fill in when it is blank. It never overwrites something "
            "you typed."
        )
        try:
            data = await ctx.act("suggestions", {})
        except StoreError:
            return await ctx.unavailable("What suggestions fill in", lede, "suggestions")
        top = ui.head("What suggestions fill in", lede, ("/admin/suggestions", "Suggestions"))
        page = f"""{top}
<div class="stack"><form method="post" action="/admin/suggestions/fill" class="stack">
{ui.card(fill_checks(data))}
<div class="actions">{ui.submit("Save")}{ui.go("Cancel", "/admin/suggestions")}</div>
</form></div>"""
        return await ctx.page("What suggestions fill in", page, "suggestions")

    @routes.post("/admin/suggestions/fill", include_in_schema=False)
    async def fill_save(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        result = await saved(
            ctx,
            "suggestions-fill",
            {"fill": picked_fill(form), "descr": "descr" in form},
            "/admin/suggestions/fill",
        )
        if isinstance(result, Response):
            return result
        try:
            data = await ctx.act("suggestions", {})
        except StoreError as exc:
            return await ctx.refused(exc, "/admin/suggestions")
        ctx.say(
            "ok",
            f"Saved. From the next note on, suggestions fill in: {fill_text(data)}."
            + await paused_note(ctx, "suggestions"),
        )
        return ctx.go("/admin/suggestions")

    # -- how the meeting summary is written ----------------------------------------------

    SUMMARY_TITLE = "How the meeting summary is written"
    SUMMARY_LEDE = (
        "When a recording is written up, Coppermind also writes a summary in fixed sections "
        "for you to check. Each section has its own directions, and those are yours to change."
    )

    def summary_page(
        data: dict[str, Any], typed: dict[str, str], errors: dict[str, str], note: str = ""
    ) -> str:
        """One box of directions for each section the store says a summary has."""
        boxes = ""
        for section in data["sections"]:
            key = section["key"]
            state = "Your own directions." if section["custom"] else "The usual directions."
            boxes += (
                ui.text_area(
                    "d_" + key,
                    f"Directions for {e(section['name'])}",
                    typed.get(key, section["text"]),
                    rows=5,
                    maxlength=data["max"],
                    hint=f"{state} Up to {data['max']:,} characters. An empty box means the "
                    "usual directions.",
                    error=errors.get(key, ""),
                )
                + f'<div class="actions" style="margin:-4px 0 14px"><button class="lat-btn" '
                f'name="how" value="reset:{e(key)}" formnovalidate>Reset to default</button>'
                "</div>"
            )
        form = f"""<form method="post" action="/admin/suggestions/summary" class="stack">
{boxes}
<p class="muted">Takes effect with the next summary Coppermind writes. Summaries already
written are not changed. To have one written again with new directions, open a note that
is waiting for review and press Write the summary again.</p>
<div class="actions"><button class="lat-btn lat-btn--primary" name="how" value="save"
data-busy="Saving…">Save</button>{ui.go("Cancel", "/admin/suggestions")}</div></form>"""
        locked = "".join(f"<li>{e(rule)}</li>" for rule in data["locked"])
        names = ui.sentence_list([e(section["name"]) for section in data["sections"]])
        rules = f"""<h3>Always the same, whatever you write</h3>
<p class="muted">The sections themselves ({names}) are fixed, and Coppermind puts these rules
around your directions every time. They are what keeps a summary safe to file, so they are
not yours to edit and cannot be switched off from here.</p><ul class="plain">{locked}</ul>"""
        return f"""{ui.head(SUMMARY_TITLE, SUMMARY_LEDE, ("/admin/suggestions", "Suggestions"))}
<div class="stack">{note}{ui.card(form)}{ui.card(rules)}</div>"""

    @routes.get("/admin/suggestions/summary", include_in_schema=False)
    async def summary(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        try:
            data = await ctx.act("summary-directions", {})
        except StoreError:
            return await ctx.unavailable(SUMMARY_TITLE, SUMMARY_LEDE, "suggestions")
        return await ctx.page(SUMMARY_TITLE, summary_page(data, {}, {}), "suggestions")

    @routes.post("/admin/suggestions/summary", include_in_schema=False)
    async def summary_save(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        try:
            data = await ctx.act("summary-directions", {})
        except StoreError as exc:
            return await ctx.refused(exc, "/admin/suggestions/summary")
        typed = {
            section["key"]: form["d_" + section["key"]]
            for section in data["sections"]
            if "d_" + section["key"] in form
        }
        how = form.get("how", "save")
        if how.startswith("reset:"):
            # Putting a default back is a draft like any other: nothing is saved until Save.
            section = next((s for s in data["sections"] if s["key"] == how[6:]), None)
            if section is None:
                return ctx.go("/admin/suggestions/summary")
            typed[section["key"]] = section["default"]
            note = ui.banner(
                "info",
                f"The usual directions for {e(section['name'])} are back in the box. Nothing "
                "is saved until you press Save.",
                fresh=True,
            )
            return await ctx.page(SUMMARY_TITLE, summary_page(data, typed, {}, note), "suggestions")
        try:
            result = await ctx.act("summary-directions-save", {"directions": typed})
        except StoreError as exc:
            return await ctx.refused(exc, "/admin/suggestions/summary")
        if "refused" in result:
            # Too long: the page comes back with what was typed, so nothing has to be redone.
            refusal = result["refused"]
            page = summary_page(data, typed, {refusal["field"]: e(refusal["message"])})
            return await ctx.page(SUMMARY_TITLE, page, "suggestions", status=422)
        own = [s["name"] for s in data["sections"] if s["key"] in result["custom"]]
        said = (
            f"Saved. The next summary is written with your directions for "
            f"{ui.sentence_list([e(name) for name in own])}."
            if own
            else "Saved. Every section uses its usual directions."
        )
        if result["emptied"]:
            empty = ui.sentence_list([e(name) for name in result["emptied"]])
            said += (
                f" The box for {empty} was empty, so "
                f"{'it uses its' if len(result['emptied']) == 1 else 'they use their'} usual "
                "directions."
            )
        ctx.say("ok", said + " Summaries already written were not changed.")
        return ctx.go("/admin/suggestions")

    # ====================================================================================
    # Transcription
    # ====================================================================================

    def heard(speech: dict[str, Any]) -> str:
        return "<br>".join(
            f'{e(line["speaker"])}: "{e(line["text"])}"' for line in speech.get("lines", [])
        )

    def test_summary(service: dict[str, Any]) -> str:
        speech = service["speech"]
        apart = (
            "and told the two speakers apart"
            if speech.get("speakers", 0) >= 2
            else "and named the speaker, though it heard one voice where the sample has two"
        )
        return (
            f"{e(service['name'])} wrote up a made-up 10-second recording in "
            f"{plural(speech.get('secs', 1), 'second')} {apart}."
        )

    @routes.get("/admin/transcription", include_in_schema=False)
    async def transcription(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        try:
            data = await ctx.act("transcription", {})
            listed = await services(ctx)
        except StoreError:
            return await ctx.unavailable("Transcription", TRANS_LEDE, "transcription")
        use = ctx.q("use")
        changing = data["state"] != "none"
        wizard = data["state"] == "none" or ctx.q("change") == "service" or bool(use)
        if not wizard:
            return await transcription_now(ctx, data)
        current = data["service"]["id"] if data["service"] else ""
        chosen_id = use or ctx.q("svc")
        chosen = find(listed, chosen_id)
        step = int(ctx.q("step") or (2 if use else 1))
        error = ""
        if step > 1 and (chosen is None or not can(chosen, "speech") or not chosen["answering"]):
            if chosen is None:
                error = "Choose a service first."
            elif not chosen["answering"]:
                error = (
                    f"{e(chosen['name'])} is not answering right now, so it cannot be used. "
                    "Open it under Manage services to test or edit it, or choose another."
                )
            else:
                error = f"{e(chosen['name'])} cannot write up recordings. Choose another."
            step = 1
        carry = {"change": "service" if changing else ""}
        cancel = ui.go("Cancel the change", "/admin/transcription?cancelled=1") if changing else ""
        labels = ["Choose a service", "Check the test", "Turn it on"]
        if step == 1:
            body = f"""<form method="get" action="/admin/transcription">
{ui.hidden({"step": 2, **carry})}
<h3>Which AI service should write up recordings?</h3>
<p class="muted">These are your AI services that passed the recording test.</p>
{
                service_choices(
                    "svc",
                    "speech",
                    "trans",
                    listed,
                    ctx.zone,
                    current=current,
                    chosen=chosen_id,
                    error=error,
                )
            }
<div class="actions">{ui.submit("Continue") if any(can(s, "speech") for s in listed) else ""}
{cancel}</div></form>"""
        elif step == 2:
            assert chosen is not None
            speech = chosen["speech"]
            models = speech.get("models", [])
            named = ctx.q("named") == "1"
            when = (
                "Tested again just now"
                if ctx.q("again")
                else f"When it was tested ({tested(chosen, ctx.zone)})"
            )
            tail = (
                "Coppermind picked the model below from the service's own list. Leave it "
                "unless you know you want another."
                if models
                else "Nothing needs choosing. The box below is only for naming a particular model."
                if named
                else "There are no settings for you to choose."
            )
            result = ui.banner(
                "ok",
                f"<b>It works.</b> {when}, {test_summary(chosen)}"
                f'<div class="quote">{heard(speech)}</div>{tail}',
            )
            model_field = ""
            if models:
                model_field = ui.select(
                    "model",
                    "Model",
                    [(name, name) for name in models],
                    ctx.q("model") or speech.get("model", ""),
                    hint="The models on this service that can write up recordings and tell "
                    "speakers apart.",
                )
            elif named:
                model_field = ui.text_input(
                    "model",
                    "Model name (optional)",
                    ctx.q("model"),
                    hint="Only if your service runs several models and you want a particular "
                    "one. Blank is fine.",
                )
            here = ui.url("/admin/transcription", step=2, svc=chosen["id"], **carry)
            particular = (
                ""
                if models or named
                else ui.a(
                    "My service runs several models and I want a particular one",
                    ui.url(here, named=1),
                )
            )
            body = f"""<h3>{e(chosen["name"])} can do it</h3>{result}
<form method="get" action="/admin/transcription" id="next">
{ui.hidden({"step": 3, "svc": chosen["id"], **carry})}{model_field}</form>
<div class="actions">{
                ui.go("Back", ui.url("/admin/transcription", step=1, svc=chosen["id"], **carry))
            }
<button class="lat-btn lat-btn--primary" form="next">Continue</button>
{
                ui.btn(
                    "Run the test again",
                    f"/admin/services/{segment(chosen['id'])}/test",
                    "",
                    {"back": ui.url(here, again=1)},
                    busy="Sending the sample recording…",
                )
            }
<span class="subtle">{particular}</span>{cancel}</div>"""
        else:
            assert chosen is not None
            model = ctx.q("model")
            back = ui.go(
                "Back",
                ui.url("/admin/transcription", step=2, svc=chosen["id"], model=model, **carry),
            )
            body = f"""<form method="post" action="/admin/transcription/save">
{ui.hidden({"svc": chosen["id"], "model": model})}
<h3>Turn it on</h3>
<p>Recordings go to {e(chosen["name"])} ({where_words(chosen)}) and nowhere else. Coppermind
deletes the audio once you have reviewed the note.</p>
<div class="field"><label class="lat-label" for="wait_s">Longest time to wait for one
recording</label><select class="lat-select" style="max-width:160px" id="wait_s" name="wait_s">
{ui.options(WAITS_TRANS, rounded_wait(WAITS_TRANS, data["wait_s"]))}</select>
<div class="lat-hint">1 hour suits most people: an hour-long recording usually takes 10 to 20
minutes. Pick more only if your recordings run for several hours.</div></div>
<div class="actions">{back}
{ui.submit("Save the change" if changing else "Turn on transcription", busy="Saving…")}
{cancel}</div></form>"""
        if not changing:
            top = ui.status(
                "off", "Transcription is off", "Optional. Three short steps to turn it on."
            )
        else:
            top = ui.banner(
                "info",
                "<b>Changing the transcription service.</b> Nothing changes until you press "
                "Save the change on the last step. Until then recordings keep going to "
                f"{e(data['service']['name'] if data['service'] else 'the service in use')}.",
            )
        picked_note = (
            ui.banner("ok", f'"{e(chosen["name"])}" is chosen.')
            if use and chosen and step == 2
            else ""
        )
        strip = setup_strip(await ctx.view(), "trans")
        page = f"""{ui.head("Transcription", TRANS_LEDE)}<div class="stack">{strip}{ctx.flash()}
{picked_note}{top}{ui.card(ui.stepper(labels, step) + body)}</div>"""
        return await ctx.page("Transcription", page, "transcription")

    async def transcription_now(ctx: Ctx, data: dict[str, Any]) -> Response:
        service = data["service"]
        state = data["state"]
        view = await ctx.view()
        name = e(service["name"]) if service else "the service"
        week = data["week"]
        here = {"back": "/admin/transcription"}
        test_action = f"/admin/services/{segment(service['id'])}/test" if service else ""
        edit = (
            ui.go("Edit this service", service_path(service, **{"for": "trans"})) if service else ""
        )
        different = ui.go("Use a different service", "/admin/transcription?change=service")
        if state == "on":
            last = data["last"]
            if last:
                length = f", {plural(last['minutes'], 'minute')} long" if last["minutes"] else ""
                took = (
                    f", written up in {plural(last['took_minutes'], 'minute')}"
                    if last["took_minutes"]
                    else ""
                )
                tail = f' Last recording: "{e(last["title"])}"{length}{took}.'
            else:
                tail = " No recording has been written up yet. The next one you hand in will be."
            top = ui.status(
                "ok",
                "Transcription is on",
                f"Using {name} ({where_words(service) if service else ''})." + tail,
                ui.btn("Pause", "/admin/transcription/pause", "", {"paused": "1"}),
            )
        elif state == "paused":
            top = ui.status(
                "warn",
                "Transcription is paused",
                "Recordings are kept and wait until you resume.",
                ui.btn("Resume", "/admin/transcription/pause", "primary", {"paused": ""}),
            )
        else:
            waiting = week["waiting"]
            if service is None:
                top = ui.status(
                    "bad",
                    "Transcription has stopped",
                    "The AI service it used is no longer in your list. "
                    f"{plural(waiting, 'recording is', 'recordings are')} waiting. The "
                    "recordings are safe. Choose another service.",
                    ui.go(
                        "Use a different service", "/admin/transcription?change=service", "primary"
                    ),
                )
            else:
                top = ui.status(
                    "bad",
                    "Transcription has stopped",
                    f"{name} has not answered since {since(service, ctx.zone)}. "
                    f"{plural(waiting, 'recording is', 'recordings are')} waiting. The recordings "
                    "are safe." + stopped_help(service, waiting, "recording"),
                    ui.btn("Test now", test_action, "primary", here, busy="Sending the sample…")
                    + edit
                    + different,
                )
        done, waiting, failed = week["done"], week["waiting"], week["failed"]
        if done or waiting or failed:
            this_week = f"{plural(done, 'recording')} written up. "
            if waiting:
                this_week += plural(waiting, "is", "are") + " waiting. "
            if failed:
                this_week += plural(failed, "has", "have") + " failed. "
            if not waiting and not failed:
                this_week += "None failed. "
        else:
            this_week = "None yet. "
        rows = ui.rows(
            ui.row(
                "AI service",
                (
                    f"{name} {health_badge(service)} {where_text(service)} "
                    + (f"Model: {e(data['model'])}." if data["model"] else "It uses its own model.")
                    + (f" Last tested {tested(service, ctx.zone)}." if service["answering"] else "")
                )
                if service
                else "None chosen.",
                ""
                if state == "failing" or not service
                else ui.btn("Test now", test_action, "", here, busy="Sending the sample…")
                + edit
                + different,
            ),
            ui.row(
                "Longest wait",
                ui.quick_select(
                    "/admin/transcription/wait",
                    "seconds",
                    "Longest time to wait for one recording",
                    WAITS_TRANS,
                    rounded_wait(WAITS_TRANS, data["wait_s"]),
                )
                + " for one recording, before Coppermind gives up and tries later.",
            ),
            ui.row(
                "If a recording fails",
                "Try "
                + ui.quick_select(
                    "/admin/transcription/tries",
                    "tries",
                    "Tries per recording",
                    [("1", "1"), ("2", "2"), ("3", "3")],
                    str(data["attempts"]),
                )
                + " times in all, then stop and tell you. Each try sends the whole recording "
                "again.",
            ),
            ui.row(
                "This week",
                this_week + ui.a("See recordings", "/admin/incoming?tab=recording"),
                ui.btn(
                    "Try it again" if failed == 1 else "Try them again",
                    "/admin/transcription/retry",
                    busy="Trying again…",
                )
                if failed and state == "on"
                else "",
            ),
        )
        cancelled = (
            ui.banner(
                "ok",
                f"Change cancelled. Nothing was changed: recordings still go to {name}.",
                fresh=True,
            )
            if ctx.q("cancelled")
            else ""
        )
        page = f"""{ui.head("Transcription", TRANS_LEDE)}<div class="stack">
{setup_strip(view, "trans")}{ctx.flash()}{cancelled}{top}
{support_detail(service, ctx.zone) if state == "failing" and service else ""}
{ui.card(rows)}</div>"""
        return await ctx.page("Transcription", page, "transcription")

    @routes.post("/admin/transcription/save", include_in_schema=False)
    async def transcription_save(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        result = await saved(
            ctx,
            "transcription-save",
            {
                "connection": form.get("svc", ""),
                "model": form.get("model", ""),
                "wait_s": int(form.get("wait_s") or 3600),
            },
            "/admin/transcription",
        )
        if isinstance(result, Response):
            return result
        name = e(result["service"])
        recordings = result["resumed"].get("recordings", 0)
        ctx.say(
            "ok",
            ("Saved. " if result["was"] != "none" else "Transcription is on. ")
            + f"Recordings now go to {name}. "
            + (
                f"The {plural(recordings, 'recording that was', 'recordings that were')} "
                f"waiting {'is' if recordings == 1 else 'are'} being written up now."
                if recordings
                else "To try it, record with the companion in Obsidian or add a recording "
                "under " + ui.a("Recordings and imports", "/admin/incoming") + "."
            )
            + await paused_note(ctx, "transcription"),
        )
        return ctx.go("/admin/transcription")

    @routes.post("/admin/transcription/pause", include_in_schema=False)
    async def transcription_pause(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        paused = bool(form.get("paused"))
        result = await saved(ctx, "transcription-pause", {"paused": paused}, "/admin/transcription")
        if isinstance(result, Response):
            return result
        ctx.say(
            "ok",
            "Transcription is paused. Recordings are kept and wait until you resume."
            if paused
            else "Transcription is on again. Any waiting recordings are being written up now.",
        )
        return ctx.go("/admin/transcription")

    @routes.post("/admin/transcription/wait", include_in_schema=False)
    async def transcription_wait(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        value = form.get("seconds", "")
        result = await saved(
            ctx, "transcription-wait", {"seconds": int(value or 0)}, "/admin/transcription"
        )
        if isinstance(result, Response):
            return result
        ctx.say(
            "ok",
            f"Saved. Coppermind now waits up to {wait_words(WAITS_TRANS, value)} for one "
            "recording.",
        )
        return ctx.go("/admin/transcription")

    @routes.post("/admin/transcription/tries", include_in_schema=False)
    async def transcription_tries(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        value = form.get("tries", "")
        result = await saved(
            ctx, "transcription-tries", {"tries": int(value or 0)}, "/admin/transcription"
        )
        if isinstance(result, Response):
            return result
        ctx.say(
            "ok",
            f"Saved. A recording is now tried {'once' if value == '1' else value + ' times'} "
            "before Coppermind stops and tells you.",
        )
        return ctx.go("/admin/transcription")

    @routes.post("/admin/transcription/retry", include_in_schema=False)
    async def transcription_retry(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        await ctx.form()
        result = await saved(ctx, "transcription-retry-failed", {}, "/admin/transcription")
        if isinstance(result, Response):
            return result
        count = len(result["retried"])
        ctx.say(
            "ok" if count else "warn",
            f"Trying {plural(count, 'recording')} again. "
            f"{'It is' if count == 1 else 'They are'} back in the queue and will be written "
            "up in turn."
            if count
            else "No recording could be tried again.",
        )
        return ctx.go("/admin/transcription")

    return routes
