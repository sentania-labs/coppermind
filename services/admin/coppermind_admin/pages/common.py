"""Words and pieces that more than one page shows, kept in one place so they agree."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from coppermind_admin import model, ui
from coppermind_admin.ui import e, plural, segment

# The three things that use an AI service, and what each needs from one.
FEATS: dict[str, dict[str, str]] = {
    "sugg": {
        "name": "Suggestions",
        "lower": "suggestions",
        "to": "/admin/suggestions",
        "need": "text",
    },
    "trans": {
        "name": "Transcription",
        "lower": "transcription",
        "to": "/admin/transcription",
        "need": "speech",
    },
    "search": {
        "name": "Search by meaning",
        "lower": "search by meaning",
        "to": "/admin/settings/search",
        "need": "embed",
    },
}
JOB = {"text": "write suggestions", "speech": "write up recordings", "embed": "do this"}


def service_path(service: dict[str, Any], **params: Any) -> str:
    return ui.url("/admin/services/" + segment(service["id"]), **params)


def health_badge(service: dict[str, Any]) -> str:
    if service["answering"]:
        return ui.badge("ok", "Answering")
    return ui.badge("bad", "Not answering")


def where_words(service: dict[str, Any]) -> str:
    return "outside your network" if service["outside"] else "on your network"


def where_text(service: dict[str, Any]) -> str:
    address = f"At {e(service['address'])}, " if service["address"] else ""
    text = address + where_words(service) + "."
    return text[0].upper() + text[1:]


def outside_note(service: dict[str, Any], need: str) -> str:
    """What leaves the owner's network if this service is chosen, and that it costs money."""
    if not service["outside"]:
        return ""
    sent = "Recordings" if need == "speech" else "Note text"
    return f" {sent} would be sent to it, and it charges your account for each use."


def can(service: dict[str, Any], need: str) -> bool:
    return bool(service[need]["ok"])


def caps_text(service: dict[str, Any]) -> str:
    jobs = []
    if can(service, "text"):
        jobs.append("suggestions")
    if can(service, "embed"):
        jobs.append("search by meaning")
    if can(service, "speech"):
        jobs.append("transcription")
    return ui.sentence_list(jobs) or "nothing yet"


def used_text(service: dict[str, Any]) -> str:
    return ", ".join(FEATS[key]["name"] for key in service["used_by"]) or "nothing at the moment"


def since(service: dict[str, Any], zone: ZoneInfo) -> str:
    return ui.since(service["down_since"], zone)


def tested(service: dict[str, Any], zone: ZoneInfo) -> str:
    return ui.ago(service["tested_at"], zone) if service["tested_at"] else "never"


def down_help(service: dict[str, Any]) -> str:
    """What to do about a service that is not answering, in the order to try it."""
    address = f" ({e(service['address'])})" if service["address"] else ""
    if service.get("key_refused"):
        return (
            f" <b>What to do:</b> {e(service['name'])} answered, but it refused the key. "
            "Replace the key with one that works, then press Test now."
        )
    if service.get("address_before"):
        # The address was just changed, so that is the first thing to doubt, not the computer.
        return (
            f" <b>What to do:</b> the address of {e(service['name'])} was changed, and nothing "
            f"has answered at the new one{address}. Check it for a slip. The address before "
            f"was {e(service['address_before'])}: to go back to it, edit this service, type it "
            "in and save. If the new address is right, start the service there, then press "
            "Test now."
        )
    if service["outside"]:
        return (
            f" <b>What to do:</b> check that this server can reach the internet and that your "
            f"{e(service['name'])} account is in good standing, then press Test now. If the "
            "key has changed, edit it."
        )
    return (
        f" <b>What to do:</b> almost always the computer that runs {e(service['name'])}"
        f"{address} is switched off or asleep, or the service on it has stopped. Start it "
        "again, then press Test now. If its address or key has changed, edit it."
    )


def stopped_help(service: dict[str, Any], waiting: int, thing: str) -> str:
    text = down_help(service).replace(
        " If its address or key has changed, edit it.",
        " If its address or key has changed, press Edit this service.",
    )
    if waiting and "press Test now." in text:
        done = "itself" if waiting == 1 else "themselves"
        text = text.replace(
            "press Test now.",
            f"press Test now. The {plural(waiting, 'waiting ' + thing)} will then be done by "
            f"{done}; nothing is lost.",
            1,
        )
    return text + " If it has been replaced, press Use a different service."


def support_detail(service: dict[str, Any], zone: ZoneInfo) -> str:
    if not service["detail"]:
        return ""
    tries = service["fails"]
    last = ui.when(service["last_failed_at"], zone) if service["last_failed_at"] else ""
    text = service["detail"] + (
        f" Tried {plural(tries, 'time')}, last {last}." if tries and last else ""
    )
    return ui.tech(text)


def service_choices(
    name: str,
    need: str,
    feature: str,
    services: list[dict[str, Any]],
    zone: ZoneInfo,
    *,
    current: str = "",
    chosen: str = "",
    error: str = "",
) -> str:
    """Step one of a feature: pick one of the services that can do the job.

    No address or key is ever typed on a feature page. An outside service is never
    the one already ticked: sending anything outside is always chosen on the screen.
    """
    able = [service for service in services if can(service, need)]
    unable = [service for service in services if not can(service, need)]
    usable = {service["id"] for service in able if service["answering"]}
    default = (
        chosen
        if chosen in usable
        else current
        if current in usable
        else next(
            (s["id"] for s in able if s["answering"] and not s["outside"]),
            "",
        )
    )
    job = JOB[need]
    out = ""
    for service in able:
        title = e(service["name"]) + " " + health_badge(service)
        if service["id"] == current:
            title += " " + ui.badge("", "In use now")
        sub = where_text(service)
        if not service["answering"]:
            sub += f" Not answering since {since(service, zone)}."
        out += ui.choice(
            name, service["id"], title, sub + outside_note(service, need), service["id"] == default
        )
    if not able:
        out += (
            '<p class="muted">'
            + (
                f"None of your AI services can {job}."
                if services
                else "No AI service has been added yet."
            )
            + (
                " Add one on the AI services page. Coppermind tests what it can do, and you come "
                "straight back here.</p>"
            )
        )
    out += ui.field_error(error)
    if unable and able:
        names = ", ".join(e(service["name"]) for service in unable)
        out += (
            f'<p class="lat-hint">Not listed, because the test showed '
            f"{'it' if len(unable) == 1 else 'they'} cannot {job}: {names}.</p>"
        )
    links = ui.go(
        "Add a new service",
        ui.url("/admin/services/new", **{"for": feature, "pick": 1}),
        "" if able else "primary",
    )
    if services:
        links += ui.go("Manage services", ui.url("/admin/services", **{"for": feature, "pick": 1}))
    return out + f'<div class="actions" style="margin-top:6px">{links}</div>'


def setup_strip(view: dict[str, Any] | None, key: str) -> str:
    """Stays on a setup step's page until setup is finished, so the way forward never goes."""
    if view is None:
        return ""
    steps = model.setup_steps(view)
    mine = next((step for step in steps if step.key == key), None)
    if mine is None:
        return ""
    following = model.setup_next(steps)
    if not mine.done:
        # An optional step that is still to do says so on its own page, with the way past it.
        if mine.needed or mine.skipped or following is None:
            return ""
        return ui.banner(
            "info",
            f"<b>Setup: {model.setup_done(steps)} of {len(steps)} done.</b> "
            f"This step ({mine.title}) is optional. "
            + ui.btn("Skip for now", "/admin/setup/skip", "", {"step": mine.key})
            + " "
            + ui.go("See all steps", "/admin/setup"),
        )
    if following is None:
        if view["setup"].get("finished_seen"):
            return ""
        return ui.banner(
            "ok",
            "<b>That was the last setup step. Coppermind is fully set up.</b> "
            + ui.go("Go to Home", "/admin", "primary"),
        )
    optional = "" if following.needed else " (optional)"
    return ui.banner(
        "info",
        f"<b>Setup: {model.setup_done(steps)} of {len(steps)} done.</b> "
        f"Next: {following.title}{optional}. "
        + ui.go("Continue", following.to, "primary")
        + " "
        + (
            ""
            if following.needed
            else ui.btn("Skip for now", "/admin/setup/skip", "", {"step": following.key}) + " "
        )
        + ui.go("See all steps", "/admin/setup"),
    )


def obsidian(
    view: dict[str, Any] | None, path: str, label: str = "Open in Obsidian", kind: str = ""
) -> str:
    """A link that opens one file in Obsidian on this device, in the connected vault."""
    vault = (view or {}).get("sync", {}).get("vault_name") or ""
    target = "obsidian://open?" + (f"vault={quote(vault, safe='')}&" if vault else "")
    target += "file=" + quote(path.removesuffix(".md"), safe="")
    return ui.go(label, target, kind)


def folder_words(path: str) -> str:
    """A folder path the way a person reads it: work › 2026 › 2026-11."""
    return " › ".join(e(part) for part in path.strip("/").split("/") if part)


def minutes(count: int | None) -> str:
    return "" if count is None else plural(count, "minute")
