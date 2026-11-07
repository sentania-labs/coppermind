"""Dossier records and the agent-facing correction contract."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

EntityKind = Literal["person", "company"]


class EntityInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: EntityKind
    name: str = Field(min_length=1, max_length=200)
    aliases: list[str] = Field(default_factory=list, max_length=100)
    emails: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("name")
    @classmethod
    def single_line(cls, value: str) -> str:
        if not value.strip() or any(c in value for c in "\n\r\0"):
            raise ValueError("use a nonblank single line")
        return value.strip()

    @field_validator("aliases", "emails")
    @classmethod
    def labels(cls, values: list[str]) -> list[str]:
        if any(len(v) > 200 for v in values):
            raise ValueError("aliases and emails must be at most 200 characters")
        return sorted({cls.single_line(v) for v in values})


class Evidence(BaseModel):
    note_id: str | None = None
    note_hash: str
    path: str
    date: str = ""
    quote: str = ""
    extraction_version: str = "operator-v1"


class Fact(BaseModel):
    id: str
    predicate: str
    value: str
    target: str | None = None
    state: Literal["confirmed", "withdrawn"]
    evidence: Evidence
    origin: Literal["metadata", "prose", "correction"]
    supersedes: str | None = None
    created_at: datetime


class Entity(EntityInput):
    id: str
    path: str
    version: int = 1
    facts: list[Fact] = Field(default_factory=list)
    retired_at: datetime | None = None
    redirect: str | None = None
    automatic: bool = False
    # Page receipts allow a restart between ledger commit and projection replacement.
    generated: str | None = None
    pending: str | None = None
    personal: bool = False
    problem: str | None = None


class EntityQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: EntityKind | None = None
    q: str = Field(default="", max_length=500)
    alias: str | None = Field(default=None, max_length=200)
    relationship: str | None = Field(default=None, max_length=200)
    target: str | None = None
    cursor: str | None = None
    limit: int = Field(default=50, ge=1, le=200)
    retired: bool = False


class EntityChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    action: Literal[
        "rename",
        "aliases",
        "claim",
        "correct",
        "withdraw",
        "restore",
        "retire",
        "merge-preview",
        "merge",
        "split-preview",
        "split",
    ]
    name: str | None = Field(default=None, min_length=1, max_length=200)
    aliases: list[str] | None = Field(default=None, max_length=100)
    emails: list[str] | None = Field(default=None, max_length=100)
    fact_id: str | None = None
    predicate: str = Field(default="note", min_length=1, max_length=200)
    value: str = Field(default="", max_length=2000)
    target: str | None = None
    note_id: str | None = None
    quote: str = Field(default="", max_length=2000)
    fact_ids: list[str] = Field(default_factory=list, max_length=1000)
    preview: str | None = None

    @model_validator(mode="after")
    def valid_claim(self) -> EntityChange:
        if self.action in {"claim", "correct"}:
            self.predicate = EntityInput.single_line(self.predicate)
            if not self.value.strip() or "\0" in self.value or "\0" in self.quote:
                raise ValueError("facts need a nonblank value without NUL characters")
        return self


class EntityLedger(BaseModel):
    schema_version: Literal[1] = 1
    revision: int = 0
    entities: dict[str, Entity] = Field(default_factory=dict)
    receipts: list[dict] = Field(default_factory=list)
    # Minimal hashes prevent re-creating deliberately purged identities from old metadata.
    suppressed: list[str] = Field(default_factory=list)
