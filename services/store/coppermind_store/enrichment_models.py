"""Durable suggestions and decisions, separate from authored note bodies."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from coppermind.context_config import ContextModel
from coppermind.meeting_summary import MeetingSummary


class Suggestion(ContextModel):
    kind: Literal["filename", "field", "event", "contact", "attendee", "invitee", "speaker"]
    confidence: float = Field(default=0, ge=0, le=1)
    entity_ids: list[str] = Field(default_factory=list, max_length=20)
    field: str = ""
    value: Any = None
    event_ids: list[str] = Field(default_factory=list, max_length=20)
    contact_ids: list[str] = Field(default_factory=list, max_length=50)
    quote: str = Field(default="", max_length=2000)
    reason: str = Field(min_length=1, max_length=2000)


class Suggestions(ContextModel):
    meeting_summary: MeetingSummary | None = None
    suggestions: list[Suggestion] = Field(default_factory=list, max_length=30)
    unresolved: list[str] = Field(default_factory=list, max_length=20)


class EnrichmentJob(ContextModel):
    id: str
    note_id: str
    purpose: Literal["initial", "calendar", "dossier"] = "initial"
    extraction_version: str = ""
    note_hash: str
    evidence_hash: str
    config_hash: str
    schema_hash: str
    path: str
    source_versions: dict[str, int] = Field(default_factory=dict)
    state: Literal[
        "queued", "running", "suggested", "failed", "stale", "rejected", "applying", "applied"
    ] = "queued"
    # Set when the owner asked for fresh suggestions; automatic re-queues leave it off.
    fresh: bool = False
    # Fingerprint of the summary limits in force when the summary stage failed on them;
    # empty otherwise. A change to those limits retries only such a job.
    summary_limits: str = ""
    attempts: int = 0
    attempt_limit: int | None = Field(default=None, ge=1, le=3)
    error: str = ""
    # Stable name of the rule that failed the job, and for size failures the measured
    # size against the limit. Never holds model output.
    error_rule: str = ""
    input_size: int | None = None
    input_limit: int | None = None
    # The start of the model's reply (bounded) kept on a failed job, so Inspect can show
    # what the model said when its answer was refused. Empty when there was no reply.
    reply: str = Field(default="", max_length=4000)
    # When the answer was refused for its shape: each part that was wrong (`path`) and why
    # (`reason`), in words that never repeat what the model wrote.
    problems: list[dict[str, str]] = Field(default_factory=list, max_length=20)
    # The failure in whole sentences for the owner: no rule names, paths or error text.
    plain: str = Field(default="", max_length=1000)
    # What had to be tidied to read the answer (a code fence, a missing optional part), so a
    # model that needs it can be told apart from one that does not.
    repairs: list[str] = Field(default_factory=list, max_length=60)
    # After a timeout, no new attempt before this time (ISO-8601). Empty means no wait.
    retry_after: str = ""
    created_at: str
    # When the model last gave an answer this job accepted. Empty until it has.
    succeeded_at: str = ""
    # How the job ended, set once when it does: it produced suggestions (`succeeded`), ran out
    # of attempts (`failed`), was replaced by a newer request or by a change to the note
    # (`superseded`), or was not needed because the note was already reviewed (`skipped`).
    # Empty while it is still queued or running.
    outcome: Literal["", "succeeded", "failed", "superseded", "skipped"] = ""
    finished_at: str = ""
    result: Suggestions | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)
    decisions: list[dict[str, Any]] = Field(default_factory=list)
    pending_approval: dict[str, Any] | None = None
    notice: str = ""

    def settled(self) -> str:
        """The outcome this job's state amounts to now; empty if it can still change."""
        if self.state in {"suggested", "applying", "applied", "rejected"}:
            return "succeeded"
        if self.state == "stale":
            return "skipped" if self.error_rule == "skipped" else "superseded"
        if self.state == "failed" and self.attempt_limit and self.attempts >= self.attempt_limit:
            return "failed"
        return ""

    @property
    def ended(self) -> str:
        """How this job ended, whether or not the outcome was recorded when it did."""
        return self.outcome or self.settled()

    @property
    def decided(self) -> set[int]:
        return {d["index"] for d in self.decisions if "index" in d}

    def set_error(
        self,
        message: str = "",
        rule: str = "",
        size: int | None = None,
        limit: int | None = None,
        *,
        reply: str = "",
        retry_after: str = "",
        problems: list[dict[str, str]] | None = None,
        plain: str = "",
    ) -> None:
        self.error, self.error_rule, self.input_size, self.input_limit = message, rule, size, limit
        self.problems = list(problems or [])
        self.plain = plain[:1000]
        self.reply, self.retry_after = reply[:4000], retry_after
