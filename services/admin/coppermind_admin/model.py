"""Small facts about what the store observed, shared by more than one Admin page."""

from __future__ import annotations

from typing import Any

from coppermind_admin.ui import segment


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
