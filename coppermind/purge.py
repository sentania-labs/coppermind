"""Public retention purge requests and durable receipt summaries."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PurgeCandidate(BaseModel):
    kind: Literal["note", "source", "artifact"]
    id: str
    title: str
    path: str
    origins: list[str]
    providers: list[str]
    deleted_at: datetime | None = None
    leftover: bool = False
    reason: str | None = None


class PurgeBreakdown(BaseModel):
    notes: int = 0
    sources: int = 0
    artifacts: int = 0


class PurgeCounts(BaseModel):
    notes: int = 0
    sources: int = 0
    artifacts: int = 0
    by_origin: dict[str, PurgeBreakdown] = Field(default_factory=dict)
    by_provider: dict[str, PurgeBreakdown] = Field(default_factory=dict)


class PurgeKept(BaseModel):
    """What the dry run looked at and left alone, so a short list is not a mystery."""

    notes_within_retention: int = 0
    sources_in_use: int = 0
    artifacts_within_retention: int = 0


class PurgeResult(BaseModel):
    token: str
    state: Literal["preview", "applying", "failed", "complete", "abandoned"]
    created_at: datetime
    retention_days: int
    candidates: list[PurgeCandidate]
    counts: PurgeCounts
    kept: PurgeKept = Field(default_factory=PurgeKept)
    completed: int = 0
    remaining: list[str] = Field(default_factory=list)
    error: str | None = None


class ConfirmPurge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmation: Literal["Purge listed items"]


class AbandonPurge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(pattern=r"^[0-9a-f]{64}$")
