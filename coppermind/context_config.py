"""Versioned calendar sources and feature-to-model assignments. No credentials."""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from coppermind.naming import sanitize_folder

Slug = str


class ContextModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CalendarSource(ContextModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=120)
    folder: str = Field(min_length=1, max_length=200)
    timezone: str = "America/Chicago"
    enabled: bool = True

    @field_validator("folder")
    @classmethod
    def folder_path(cls, value: str) -> str:
        if sanitize_folder(value) != value or value.startswith("."):
            raise ValueError("use a relative vault folder without hidden or parent segments")
        if any(part.startswith(".") for part in value.split("/")):
            raise ValueError("hidden folders cannot be drop locations")
        return value

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (KeyError, ValueError) as exc:
            raise ValueError("unknown timezone") from exc
        return value


class ModelConnection(ContextModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=120)
    provider: Literal["anthropic", "openai", "compatible"]
    base_url: str = ""
    # Changing credentials increments this counter, invalidating capability tests.
    credential_version: int = 0

    @model_validator(mode="after")
    def endpoint(self) -> ModelConnection:
        if self.provider != "compatible":
            if self.base_url:
                raise ValueError("only compatible providers accept a custom URL")
        else:
            parsed = urlsplit(self.base_url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("use an HTTP(S) base URL without credentials, query or fragment")
            self.base_url = self.base_url.rstrip("/")
        return self

    @property
    def endpoint_url(self) -> str:
        return {
            "anthropic": "https://api.anthropic.com/v1",
            "openai": "https://api.openai.com/v1",
            "compatible": self.base_url,
        }[self.provider]


class FeatureAssignment(ContextModel):
    connection: str = ""
    model: str = Field(default="", max_length=200)
    enabled: bool = False
    paused: bool = False
    timeout_s: int = Field(default=45, ge=5, le=120)
    max_output_tokens: int = Field(default=2048, ge=256, le=8192)
    max_input_chars: int = Field(default=60000, ge=2000, le=200000)
    # One worker serializes model calls and approvals. No unbounded fan-out.
    attempts: int = Field(default=2, ge=1, le=3)
    fields: list[str] = Field(default_factory=list, max_length=20)
    attendees_field: str = ""
    invitees_field: str = ""
    event_field: str = ""
    contacts_field: str = ""
    automatic_after: str = ""


class ContextConfig(ContextModel):
    revision: int = 0
    dropbox_folder: str = "Dropbox"
    timezone: str = "America/Chicago"
    lifecycle_enabled: bool = False

    @field_validator("dropbox_folder")
    @classmethod
    def safe_dropbox(cls, value: str) -> str:
        return CalendarSource.folder_path(value)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        return CalendarSource.known_timezone(value)

    sources: list[CalendarSource] = Field(default_factory=list)
    connections: list[ModelConnection] = Field(default_factory=list)
    # Generated connection ID -> original storage identity for migrated connections.
    connection_aliases: dict[str, str] = Field(default_factory=dict)
    automatic_connection_ids: list[str] = Field(default_factory=list)
    assignments: dict[str, FeatureAssignment] = Field(
        default_factory=lambda: {"enrichment": FeatureAssignment()}
    )

    @model_validator(mode="after")
    def unique(self) -> ContextConfig:
        for records in (self.sources, self.connections):
            if len({item.id for item in records}) != len(records):
                raise ValueError("duplicate identity")
        for i, source in enumerate(self.sources):
            for other in self.sources[i + 1 :]:
                a, b = source.folder.casefold(), other.folder.casefold()
                if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
                    raise ValueError("calendar drop folders cannot overlap")
        if set(self.assignments) != {"enrichment"}:
            raise ValueError("only enrichment is implemented")
        return self


def read_context_config(state_dir: Path) -> ContextConfig:
    path = state_dir / "context" / "config.json"
    return (
        ContextConfig.model_validate_json(path.read_bytes()) if path.exists() else ContextConfig()
    )
