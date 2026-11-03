"""Public retention purge requests and durable receipt summaries."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PurgeCandidate(BaseModel):
    kind: Literal["note", "source"]
    id: str
    title: str
    path: str
    origins: list[str]
    providers: list[str]
    deleted_at: datetime | None = None


class PurgeCounts(BaseModel):
    notes: int = 0
    sources: int = 0
    by_origin: dict[str, int] = Field(default_factory=dict)
    by_provider: dict[str, int] = Field(default_factory=dict)


class PurgeResult(BaseModel):
    token: str
    state: Literal["preview", "applying", "failed", "complete"]
    created_at: datetime
    retention_days: int
    candidates: list[PurgeCandidate]
    counts: PurgeCounts
    completed: int = 0
    error: str | None = None


class ConfirmPurge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmation: Literal["Purge listed items"]
