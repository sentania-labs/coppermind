"""People and voices: what Coppermind knows about someone, corrected where it is listed.

Voices are kept as signatures only, never as audio. Each saved voice can be seen
and removed, a person can be marked "never save", and one switch turns voice recognition
off altogether. Who a voice is gets chosen on the card in Review.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse

from coppermind.settings import Wiring
from coppermind.store_protocol import StoreError
from coppermind_admin import ui
from coppermind_admin.pages.common import obsidian
from coppermind_admin.shell import Ctx, explain
from coppermind_admin.ui import e, plural, segment

LEDE = (
    "People and companies Coppermind has learned from notes you reviewed. Open one to see "
    "what it knows, correct it, or manage a saved voice."
)


async def stream_artifact(ctx: Ctx, artifact_id: str) -> Response:
    """Pass one stored file through to the browser without holding it."""
    blob = await ctx.store.get_artifact_bytes(artifact_id)
    return StreamingResponse(
        blob.content,
        media_type=blob.mime_type,
        headers={"Content-Length": str(blob.size_bytes), "Accept-Ranges": "none"},
    )


def person_path(identity: str) -> str:
    return "/admin/people/" + segment(identity)


def first_name(name: str) -> str:
    return e(name.split(" ")[0])


def router(wiring: Wiring) -> APIRouter:
    routes = APIRouter()

    @routes.get("/admin/people", include_in_schema=False)
    async def people(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        try:
            data = await ctx.act("people", {})
        except StoreError:
            return await ctx.unavailable("People and voices", LEDE, "people")
        listed, companies = data["people"], data["companies"]
        if not listed and not companies:
            body = ui.head("People and voices", LEDE) + ui.card(
                ui.empty(
                    "No people yet",
                    "People appear here after you review notes that mention them. Voices are "
                    "learned from recordings you review.",
                )
            )
            return await ctx.page("People and voices", body, "people")
        tab = ctx.q("tab") or "people"
        if tab not in {"people", "companies", "voices"}:
            tab = "people"
        voiced = [person for person in listed if person["samples"]]
        dialog = ""
        if tab == "people":
            inner = "".join(
                ui.item(
                    ui.a(e(person["name"]), person_path(person["id"]))
                    + (" " + ui.badge("accent", "Voice saved") if person["samples"] else "")
                    + (" " + ui.badge("", "Voice never saved") if person["never"] else ""),
                    (e(person["company"]) + ". " if person["company"] else "")
                    + f"In {plural(person['notes'], 'note')}.",
                    ui.go("Open", person_path(person["id"])),
                )
                for person in listed
            ) or ui.empty("No people yet", "They appear after you review notes that mention them.")
        elif tab == "companies":
            inner = "".join(
                ui.item(
                    ui.a(e(company["name"]), person_path(company["id"])),
                    f"{plural(company['people'], 'person', 'people')}. "
                    f"In {plural(company['notes'], 'note')}.",
                    ui.go("Open", person_path(company["id"])),
                )
                for company in companies
            ) or ui.empty("No companies yet", "")
        else:
            here = "/admin/people?tab=voices"
            on = data["recognition"]
            switch = ui.rows(
                ui.row(
                    "Voice recognition",
                    "On. Coppermind keeps a signature of each voice it hears, never the audio, "
                    "and uses it to recognise who is speaking."
                    if on
                    else "Off. No voice is learned or recognised. Signatures already saved are "
                    "kept but not used; remove them below if you want them gone.",
                    ui.go("Turn off", ui.url(here, ask="off"))
                    if on
                    else ui.btn(
                        "Turn on", "/admin/people/voices/recognition", "primary", {"enabled": "1"}
                    ),
                )
            )
            saved = "".join(
                ui.item(
                    ui.a(e(person["name"]), person_path(person["id"])),
                    f"{plural(person['samples'], 'voice sample')}. Last heard "
                    f"{ui.ago(person['heard'], ctx.zone)}.",
                    ui.go("Remove voice", ui.url(here, ask="voice:" + person["id"])),
                )
                for person in voiced
            ) or ui.empty("No saved voices", "They are learned from recordings you review.")
            inner = (
                switch
                + '<p class="muted" style="margin-top:8px">Coppermind learns a person\'s voice '
                "from the voices you name on a card in Review, so it can tell who is speaking "
                "next time.</p>" + saved
            )
            asked = ctx.q("ask")
            if asked == "off":
                dialog = ui.dialog(
                    "Turn voice recognition off?",
                    "Coppermind stops learning voices and stops recognising who is speaking, "
                    "for everyone. Signatures already saved are kept but not used, so turning "
                    "it back on later picks up where it left off. To delete them, remove each "
                    "saved voice.",
                    "/admin/people/voices/recognition",
                    "Turn off",
                    here,
                    danger=True,
                    fields={"enabled": ""},
                )
            elif asked.startswith("voice:"):
                target = next((p for p in voiced if p["id"] == asked[6:]), None)
                if target:
                    dialog = voice_dialog(target, here)

        tabs = ui.seg(
            "/admin/people",
            [
                ("people", f"People ({len(listed)})"),
                ("companies", f"Companies ({len(companies)})"),
                ("voices", f"Saved voices ({len(voiced)})"),
            ],
            tab,
        )
        body = f"""{ui.head("People and voices", LEDE)}<div class="stack">{ctx.flash()}
{tabs}{ui.card(inner)}</div>"""
        return await ctx.page("People and voices", body, "people", dialog=dialog)

    def voice_dialog(person: dict[str, Any], back: str) -> str:
        first = first_name(person["name"])
        samples = person["samples"]
        return ui.dialog(
            f"Remove {e(person['name'])}'s saved voice?",
            f"The {plural(samples, 'saved sample is', 'saved samples are')} deleted for good "
            f"and Coppermind stops recognising {first} in new recordings. Notes already written "
            "do not change. Unless you tick the box below, Coppermind learns the voice again "
            f"the next time you name {first} as a speaker in a recording."
            + '<div style="margin-top:8px;color:var(--ink)">'
            + ui.check("never", f"Also never save {first}'s voice again")
            + "</div>",
            "/admin/people/voices/remove",
            "Remove voice",
            back,
            danger=True,
            fields={"id": person["id"], "back": back},
        )

    # -- voice actions -------------------------------------------------------------------

    async def done(
        ctx: Ctx, action: str, payload: dict[str, Any], to: str
    ) -> dict[str, Any] | None:
        try:
            result = await ctx.act(action, payload)
        except StoreError as exc:
            ctx.say("bad", explain(exc, "That could not be done."))
            return None
        if "refused" in result:
            ctx.say("bad", e(result["refused"]["message"]))
            return None
        return result

    @routes.post("/admin/people/voices/recognition", include_in_schema=False)
    async def recognition(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        target = "/admin/people?tab=voices"
        result = await done(
            ctx, "voice-recognition", {"enabled": bool(form.get("enabled"))}, target
        )
        if result is not None:
            ctx.say(
                "ok",
                "Voice recognition is on. Voices are learned from the recordings you review "
                "from now on."
                if result["enabled"]
                else "Voice recognition is off. No voice is learned or recognised until you "
                "turn it on again.",
            )
        return ctx.go(target)

    @routes.post("/admin/people/voices/remove", include_in_schema=False)
    async def voice_remove(request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        target = ctx.back("/admin/people?tab=voices")
        result = await done(
            ctx, "voice-remove", {"id": form.get("id", ""), "never": "never" in form}, target
        )
        if result is not None:
            ctx.say(
                "ok",
                f"Removed {e(result['name'])}'s saved voice "
                f"({plural(result['samples'], 'sample')}). "
                "Notes already written were not changed."
                + (
                    f" Coppermind will not save {first_name(result['name'])}'s voice again."
                    if result["never"]
                    else ""
                ),
            )
        return ctx.go(target)

    # -- one person or company -----------------------------------------------------------

    @routes.get("/admin/people/{identity}", include_in_schema=False)
    async def person(identity: str, request: Request) -> Response:
        ctx = Ctx(request, wiring)
        view = await ctx.view()
        try:
            data = await ctx.act("person", {"id": identity})
        except StoreError:
            return ctx.go("/admin/people")
        here = person_path(identity)
        company = data["kind"] == "company"
        first = first_name(data["name"])
        editing = ctx.q("edit")
        rows = ""
        for fact in data["facts"]:
            source = (
                ""
                if fact.get("fixed")
                else f"Corrected by you, {ui.date_only(fact['at'], ctx.zone)}."
                if fact["by_you"]
                else f'From "{e(fact["from"])}", {ui.date_only(fact["at"], ctx.zone)}.'
            )
            if fact.get("fixed"):
                rows += ui.item(
                    f"{e(fact['label'])}: {e(fact['value'])}", "From your calendar and notes."
                )
            elif editing == fact["id"]:
                rows += ui.item(
                    e(fact["label"]),
                    f"""<form class="act" method="post" action="{here}/facts/correct">
{ui.hidden({"fact_id": fact["id"]})}<input class="lat-input inline" style="max-width:320px"
name="value" value="{e(fact["value"])}" aria-label="{e(fact["label"])}" autofocus>
{ui.submit("Save")}{ui.go("Cancel", here)}</form>""",
                )
            else:
                rows += ui.item(
                    f"{e(fact['label'])}: {e(fact['value'])}",
                    source,
                    ui.go("Correct", ui.url(here, edit=fact["id"]))
                    + ui.go("Remove", ui.url(here, ask="fact:" + fact["id"])),
                )
        members = ""
        if company and data.get("people"):
            members = (
                " People: "
                + ", ".join(
                    ui.a(e(member["name"]), person_path(member["id"])) for member in data["people"]
                )
                + "."
            )
        knows = f"""<p class="muted">Appears in {plural(data["notes"], "note")}.{members} The same
facts show on {"its" if company else "their"} page in Obsidian; a correction made in either
place shows in both.</p>
{rows or ui.empty("Nothing recorded yet", "")}
<form method="post" action="{here}/facts/add">
<div class="two" style="margin-top:10px"><div>
{ui.text_input("label", "Add a fact: what it is", hint="For example: Phone, Time zone, Assistant")}
</div><div>{ui.text_input("value", "The fact")}</div></div>
<div class="actions">{ui.submit("Add fact", "")}{obsidian(view, data["path"])}</div></form>"""
        voice = ""
        if not company:
            samples = data["samples"]
            if samples:
                listing = "".join(
                    ui.item(
                        f'"{e(sample["title"])}"',
                        f"Recorded {ui.date_only(sample['at'], ctx.zone)}.",
                    )
                    for sample in samples
                )
                top = (
                    '<p class="muted">Learned from these recordings you reviewed. Only a '
                    "signature of the voice is kept; the audio of a recording is deleted once "
                    f"its note is reviewed, so there is nothing to play.</p>{listing}"
                    f'<div class="actions">{ui.go("Remove saved voice", ui.url(here, ask="voice"))}'
                    "</div>"
                )
            else:
                top = (
                    '<p class="muted">'
                    + (
                        f"Coppermind does not save {first}'s voice."
                        if data["never"]
                        else f"No voice saved. Coppermind learns it after you review a recording "
                        f"where you name {first} as a speaker."
                    )
                    + "</p>"
                )
            if data["never"]:
                switch = (
                    f'<form class="act" method="post" action="{here}/never">'
                    + ui.check(
                        "never",
                        "Never save this person's voice",
                        f"Coppermind will not learn or keep {first}'s voice, even from "
                        "recordings you review. Untick to let it learn the voice again.",
                        checked=True,
                    ).replace("<input ", "<input data-auto ", 1)
                    + '<noscript><button class="lat-btn">Save</button></noscript></form>'
                )
            else:
                ask = ui.go("Never save this person's voice", ui.url(here, ask="never"))
                switch = (
                    f'<p class="muted">Coppermind may learn {first}\'s voice from recordings '
                    f'you review.</p><div class="actions">{ask}</div>'
                )
            never = (
                '<div style="border-top:1px solid var(--line-soft);margin-top:10px;'
                f'padding-top:8px">{switch}</div>'
            )
            voice = ui.card(top + never, "Saved voice")
        dialog = ""
        asked = ctx.q("ask")
        if asked.startswith("fact:"):
            fact = next((f for f in data["facts"] if f["id"] == asked[5:]), None)
            if fact:
                dialog = ui.dialog(
                    f'Remove "{e(fact["label"])}: {e(fact["value"])}"?',
                    f"Coppermind forgets this fact about {e(data['name'])} and removes it from "
                    "the page in Obsidian. Notes are not changed. If a later note says it "
                    "again, it comes back.",
                    f"{here}/facts/remove",
                    "Remove",
                    here,
                    danger=True,
                    fields={"fact_id": fact["id"]},
                )
        elif asked == "never" and not company and not data["never"]:
            kept = len(data["samples"])
            dialog = ui.dialog(
                f"Never save {e(data['name'])}'s voice?",
                f"Coppermind will not learn or keep {first}'s voice, even from recordings "
                "you review. "
                + (
                    f"The {plural(kept, 'saved sample is', 'saved samples are')} deleted for good. "
                    if kept
                    else ""
                )
                + "Notes already written do not change.",
                f"{here}/never",
                "Never save this voice",
                here,
                danger=True,
                fields={"never": "on"},
            )
        elif asked == "voice" and not company and data["samples"]:
            dialog = voice_dialog(
                {"id": identity, "name": data["name"], "samples": len(data["samples"])}, here
            )
        elif asked == "remove":
            dialog = ui.dialog(
                f"Remove {e(data['name'])}?",
                f"Coppermind forgets what it knows about {e(data['name'])}"
                + ("" if company else ", including any saved voice,")
                + " and removes the page in Obsidian. Notes are not changed. If a later note "
                f"mentions {'it' if company else 'them'}, {e(data['name'])} is learned again.",
                f"{here}/remove",
                "Remove",
                here,
                danger=True,
            )
        lede = (
            "What Coppermind knows about this company. Correct or remove anything here."
            if company
            else "What Coppermind knows about this person, and their saved voice. Correct or "
            "remove anything here."
        )
        back_tab = "/admin/people?tab=companies" if company else "/admin/people"
        body = f"""{ui.head(e(data["name"]), lede, (back_tab, "People and voices"))}
<div class="stack">{ctx.flash()}{ui.card(knows, "What Coppermind knows")}{voice}
<p>{
            ui.go(
                "Remove this company…" if company else "Remove this person…",
                ui.url(here, ask="remove"),
                "ghost",
            )
        }</p></div>"""
        return await ctx.page(data["name"], body, "people", dialog=dialog)

    @routes.post("/admin/people/{identity}/facts/correct", include_in_schema=False)
    async def fact_correct(identity: str, request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        here = person_path(identity)
        result = await done(
            ctx,
            "fact-correct",
            {"id": identity, "fact_id": form.get("fact_id", ""), "value": form.get("value", "")},
            here,
        )
        if result is not None:
            ctx.say(
                "ok",
                f'Corrected. {e(result["label"])} is now "{e(result["value"])}" (was '
                f'"{e(result["was"])}"). The page in Obsidian shows the same.',
            )
        return ctx.go(here)

    @routes.post("/admin/people/{identity}/facts/add", include_in_schema=False)
    async def fact_add(identity: str, request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        here = person_path(identity)
        result = await done(
            ctx,
            "fact-add",
            {"id": identity, "label": form.get("label", ""), "value": form.get("value", "")},
            here,
        )
        if result is not None:
            ctx.say(
                "ok",
                f'Added "{e(result["label"])}: {e(result["value"])}". The page in Obsidian '
                "shows it too.",
            )
        return ctx.go(here)

    @routes.post("/admin/people/{identity}/facts/remove", include_in_schema=False)
    async def fact_remove(identity: str, request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        here = person_path(identity)
        result = await done(
            ctx, "fact-remove", {"id": identity, "fact_id": form.get("fact_id", "")}, here
        )
        if result is not None:
            ctx.say("ok", f'Removed "{e(result["label"])}: {e(result["value"])}".')
        return ctx.go(here)

    @routes.post("/admin/people/{identity}/never", include_in_schema=False)
    async def never(identity: str, request: Request) -> Response:
        ctx = Ctx(request, wiring)
        form = await ctx.form()
        here = person_path(identity)
        result = await done(ctx, "voice-never", {"id": identity, "never": "never" in form}, here)
        if result is not None:
            ctx.say(
                "ok",
                f"Saved. Coppermind will never save {e(result['name'])}'s voice. Any saved "
                "samples were deleted."
                if result["never"]
                else f"Saved. Coppermind may learn {e(result['name'])}'s voice again from "
                "recordings you review.",
            )
        return ctx.go(here)

    @routes.post("/admin/people/{identity}/remove", include_in_schema=False)
    async def remove(identity: str, request: Request) -> Response:
        ctx = Ctx(request, wiring)
        await ctx.form()
        result = await done(ctx, "person-remove", {"id": identity}, person_path(identity))
        if result is None:
            return ctx.go(person_path(identity))
        ctx.say("ok", f"Removed {e(result['name'])}.")
        return ctx.go(
            "/admin/people?tab=companies" if result["kind"] == "company" else "/admin/people"
        )

    return routes
