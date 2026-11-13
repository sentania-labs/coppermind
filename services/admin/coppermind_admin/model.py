"""What needs the owner, worked out once from what the store observed.

Home's list, the count beside Home in the menu, and the count beside each menu
entry are all read from `attention`, so one problem is counted under one menu
entry and the menu adds up to Home.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from coppermind_admin.layout import Menu
from coppermind_admin.ui import e, plural, segment

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


@dataclass
class Item:
    """One thing that needs the owner: a sentence, and the one button that deals with it."""

    title: str
    detail: str
    button: str
    menu: str
    to: str = ""
    post: str = ""
    fields: dict[str, str] = field(default_factory=dict)
    more: str = ""
    bad: bool = False
    busy: str = ""


def sync_state(status: dict[str, Any]) -> str:
    """One word for sync: none, on, paused or signin (stopped and waiting for a sign-in)."""
    if not status.get("configured"):
        return "none"
    if status.get("paused") or status.get("state") == "paused":
        return "paused"
    if status.get("needs_signin") or status.get("state") in {"error", "restarting", "stopped"}:
        return "signin"
    return "on"


def note_path(identity: str) -> str:
    return "/admin/notes/" + segment(identity)


def incoming_path(identity: str) -> str:
    return "/admin/incoming/" + segment(identity)


def _weekday(stamp: str) -> str:
    try:
        return WEEKDAYS[datetime.fromisoformat(stamp).weekday()]
    except ValueError:
        return "one day"


def attention(view: dict[str, Any]) -> list[Item]:
    """Everything that needs the owner, most urgent first."""
    out: list[Item] = []
    state = sync_state(view["sync"])
    if state == "signin":
        out.append(
            Item(
                "Sync has stopped",
                "Obsidian is asking Coppermind to sign in again. Notes are not reaching "
                "your devices.",
                "Sign in again",
                "sync",
                to="/admin/sync",
                bad=True,
            )
        )
    sugg, trans = view["sugg"], view["trans"]
    sugg_down, trans_down = sugg["state"] == "failing", trans["state"] == "failing"
    waiting_recordings = trans["week"]["waiting"]
    if (
        sugg_down
        and trans_down
        and sugg["service"]
        and trans["service"]
        and sugg["service"]["id"] == trans["service"]["id"]
    ):
        service = sugg["service"]
        out.append(
            Item(
                f"{e(service['name'])} is not answering",
                "Suggestions and transcription have both stopped. "
                f"{plural(sugg['waiting'], 'note')} and "
                f"{plural(waiting_recordings, 'recording')} are waiting.",
                "Test now",
                "services",
                post=f"/admin/services/{segment(service['id'])}/test",
                fields={"back": "/admin"},
                more=f"/admin/services/{segment(service['id'])}",
                bad=True,
                busy="Testing…",
            )
        )
    else:
        for down, feature, name, noun, count, page in (
            (sugg_down, sugg, "Suggestions have stopped", "note", sugg["waiting"], "suggestions"),
            (
                trans_down,
                trans,
                "Transcription has stopped",
                "recording",
                waiting_recordings,
                "transcription",
            ),
        ):
            if not down:
                continue
            service = feature["service"]
            if service is None:
                out.append(
                    Item(
                        name,
                        "The AI service it used is no longer in your list. "
                        f"{plural(count, noun + ' is', noun + 's are')} waiting. "
                        "Choose another service.",
                        "Use a different service",
                        page,
                        to=f"/admin/{page}?change=service",
                        bad=True,
                    )
                )
                continue
            out.append(
                Item(
                    name,
                    f"Coppermind cannot reach {e(service['name'])}. "
                    f"{plural(count, noun + ' is', noun + 's are')} waiting. "
                    "Open it for what to do, or test it from here.",
                    "Test now",
                    "services",
                    post=f"/admin/services/{segment(service['id'])}/test",
                    fields={"back": "/admin"},
                    more=f"/admin/{page}",
                    bad=True,
                    busy="Testing…",
                )
            )
    for note in view["notes"]["stuck"]:
        title = e(note["title"])
        if note["kind"] == "copy":
            out.append(
                Item(
                    f'"{title}" has a copy, and Coppermind needs you to say what to do with it',
                    "Both files are untouched. The usual answer is to keep both.",
                    "See both",
                    "notes",
                    to=note_path(note["id"]),
                )
            )
        elif note["kind"] == "notfiled":
            out.append(
                Item(
                    f'"{title}" could not be filed',
                    "You marked it reviewed, but a file with its name is already in the folder."
                    if note.get("name_taken")
                    else "You marked it reviewed, but it could not be filed. " + e(note["reason"]),
                    "Fix it",
                    "notes",
                    to=note_path(note["id"]),
                )
            )
        elif note["kind"] == "missing":
            out.append(
                Item(
                    f'"{title}" is missing',
                    "Its file is no longer where it was, and Coppermind has no record of it "
                    "being deleted.",
                    "See what to do",
                    "notes",
                    to=note_path(note["id"]),
                )
            )
        elif note["kind"] == "unreadable":
            out.append(
                Item(
                    f'"{title}" cannot be read',
                    "Coppermind found the file but could not read it as a note.",
                    "See why",
                    "notes",
                    to=note_path(note["id"]),
                )
            )
        else:
            out.append(
                Item(
                    f'"{title}" could not be taken on as a note',
                    e(note["reason"]),
                    "See why",
                    "notes",
                    to="/admin/notes?tab=notfiled",
                )
            )
    for failed in view["incoming"]["failed"]:
        title = e(failed["title"])
        if failed["kind"] == "recording":
            out.append(
                Item(
                    f'Recording "{title}" was not written up',
                    e(failed.get("error") or "The transcription service did not finish it."),
                    "Try again",
                    "incoming",
                    post=f"{incoming_path(failed['id'])}/retry",
                    fields={"back": "/admin"},
                    more=incoming_path(failed["id"]),
                    busy="Trying again…",
                )
            )
        else:
            out.append(
                Item(
                    f'"{title}" could not be read',
                    "Nothing was added from it. Open it to see what to do.",
                    "What to do",
                    "calendar" if failed["kind"] == "calendar" else "incoming",
                    to=incoming_path(failed["id"]),
                )
            )
    for conflict in view["calendar"]["conflicts"]:
        other = conflict.get("incoming")
        detail = (
            f"One says {_weekday(conflict['stored']['start'])}, the other says "
            f"{_weekday(other['start'])}. "
            if other and _weekday(other["start"]) != _weekday(conflict["stored"]["start"])
            else "The two files give different details. "
        )
        out.append(
            Item(
                f'Two calendar files disagree about "{e(conflict["title"])}"',
                detail + "Coppermind cannot tell which is right.",
                "Choose",
                "calendar",
                to="/admin/calendar",
            )
        )
    for note in view["notes"]["unsure"]:
        count = note["count"]
        out.append(
            Item(
                f'"{e(note["title"])}" has {plural(count, "suggestion")} '
                "Coppermind was not sure about",
                "They wait for your OK. They are never filled in by themselves."
                if count != 1
                else "It waits for your OK. It is never filled in by itself.",
                "Check them" if count != 1 else "Check it",
                "notes",
                to=note_path(note["id"]) + "/review",
            )
        )
    interrupted = view["storage"]["interrupted"]
    if interrupted:
        out.append(
            Item(
                "A clean-up was interrupted",
                f"{interrupted['done']} of {plural(interrupted['total'], 'item')} were deleted "
                f"before it stopped. The {interrupted['left']} left are untouched.",
                "Finish or cancel",
                "storage",
                to="/admin/storage/cleanup",
            )
        )
    for damaged in view["storage"]["damaged"]:
        out.append(
            Item(
                f'"{e(damaged["reference"])}" needs a look',
                e(damaged["reason"]),
                "See files",
                "storage",
                to="/admin/storage/files",
            )
        )
    if state == "paused":
        out.append(
            Item(
                "Sync is paused",
                "You paused it. Notes are not travelling to or from your devices.",
                "Resume sync",
                "sync",
                post="/admin/sync/resume",
                fields={"back": "/admin"},
                busy="Resuming…",
            )
        )
    return out


# -- guided setup ----------------------------------------------------------------------------


@dataclass
class Step:
    key: str
    title: str
    done: bool
    why: str = ""
    summary: str = "Done."
    to: str = ""
    needed: bool = False
    skipped: bool = False


def setup_steps(view: dict[str, Any]) -> list[Step]:
    skipped = view["setup"].get("skipped", {})
    sync = view["sync"]
    return [
        Step("pw", "Create your password", True),
        Step(
            "sync",
            "Connect Obsidian Sync",
            sync_state(sync) != "none",
            "So notes reach your phone and computer, and what you write there reaches Coppermind.",
            f'Connected to "{e(sync.get("vault_name") or "your vault")}".',
            "/admin/sync",
            needed=True,
        ),
        Step(
            "companion",
            "Add the companion to Obsidian",
            bool(view["companion"]["paired"]),
            "So you can review notes and hand in recordings from inside Obsidian.",
            "Connected.",
            "/admin/setup/companion",
            needed=True,
        ),
        Step(
            "sugg",
            "Turn on suggestions",
            view["sugg"]["state"] != "none",
            "An AI model fills in the date, type, people and tags on new notes for you to check.",
            "On.",
            "/admin/suggestions",
            skipped=bool(skipped.get("sugg")),
        ),
        Step(
            "trans",
            "Turn on transcription",
            view["trans"]["state"] != "none",
            "Turns recordings into written notes with each speaker named.",
            "On.",
            "/admin/transcription",
            skipped=bool(skipped.get("trans")),
        ),
        Step(
            "cal",
            "Add your calendar",
            view["calendar"]["files"] > 0,
            "So a recording or note can be matched to the meeting it came from.",
            "Added.",
            "/admin/calendar",
            skipped=bool(skipped.get("cal")),
        ),
    ]


def setup_next(steps: list[Step]) -> Step | None:
    return next((step for step in steps if not step.done and not step.skipped), None)


def setup_done(steps: list[Step]) -> int:
    return sum(1 for step in steps if step.done or step.skipped)


def menu(view: dict[str, Any] | None) -> Menu:
    """The menu's counts and words. Without an answer from the store it shows none."""
    if view is None:
        return Menu()
    items = attention(view)
    steps = setup_steps(view)
    counts: dict[str, int] = {}
    for item in items:
        if item.menu in {"notes", "incoming", "calendar", "storage"}:
            counts[item.menu] = counts.get(item.menu, 0) + 1
    return Menu(
        attention=len(items),
        stopped=any(item.bad for item in items),
        setup=(setup_done(steps), len(steps)) if setup_next(steps) else None,
        counts=counts,
        states={
            "sync": sync_state(view["sync"]),
            "sugg": view["sugg"]["state"],
            "trans": view["trans"]["state"],
        },
        services_down=sum(1 for service in view["services"] if not service["answering"]),
    )
