"""Durable suggestions and decisions, separate from authored note bodies."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from coppermind.context_config import ContextModel


class Suggestion(ContextModel):
    kind: Literal["filename", "field", "event", "contact", "attendee", "invitee"]
    field: str = ""
    value: Any = None
    event_ids: list[str] = Field(default_factory=list, max_length=20)
    contact_ids: list[str] = Field(default_factory=list, max_length=50)
    quote: str = Field(default="", max_length=2000)
    reason: str = Field(min_length=1, max_length=2000)


class Suggestions(ContextModel):
    suggestions: list[Suggestion] = Field(default_factory=list, max_length=30)
    unresolved: list[str] = Field(default_factory=list, max_length=20)


class EnrichmentJob(ContextModel):
    id: str
    note_id: str
    purpose: Literal["initial", "calendar"] = "initial"
    note_hash: str
    evidence_hash: str
    config_hash: str
    schema_hash: str
    path: str
    source_versions: dict[str, int] = Field(default_factory=dict)
    state: Literal[
        "queued", "running", "suggested", "failed", "stale", "rejected", "applying", "applied"
    ] = "queued"
    attempts: int = 0
    attempt_limit: int | None = Field(default=None, ge=1, le=3)
    error: str = ""
    # Stable name of the rule that failed the job, and for size failures the measured
    # size against the limit. Never holds model output.
    error_rule: str = ""
    input_size: int | None = None
    input_limit: int | None = None
    created_at: str
    result: Suggestions | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)
    decisions: list[dict[str, Any]] = Field(default_factory=list)
    pending_approval: dict[str, Any] | None = None

    def set_error(
        self, message: str = "", rule: str = "", size: int | None = None, limit: int | None = None
    ) -> None:
        self.error, self.error_rule, self.input_size, self.input_limit = message, rule, size, limit
