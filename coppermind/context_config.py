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


# The most one recording may take to transcribe, however steadily the service answers. An
# hour of audio can take 10 to 20 minutes on a CPU; this leaves room for several hours.
MAX_TRANSCRIBE_SECONDS = 4 * 3600


# The most one section's directions may hold. Long enough for a careful brief, short enough
# that the transcript still has the room.
SUMMARY_DIRECTIONS_MAX = 1500
# How many sections may carry directions. Far more than the summary has today.
SUMMARY_SECTIONS_MAX = 40


class FeatureAssignment(ContextModel):
    connection: str = ""
    model: str = Field(default="", max_length=200)
    enabled: bool = False
    paused: bool = False
    # How long the provider may stay silent (before the first byte, or mid-reply) before the
    # call is given up. It is not a cap on total time: a streamed reply may run longer.
    timeout_s: int = Field(default=120, ge=5, le=1800)
    max_output_tokens: int = Field(default=2048, ge=256, le=8192)
    max_input_chars: int = Field(default=60000, ge=2000, le=200000)
    # Recording summaries read the complete transcript in a separate call.
    summary_max_input_chars: int = Field(default=200000, ge=2000, le=2000000)
    summary_timeout_s: int = Field(default=900, ge=5, le=1800)
    # The owner's own directions for each section of a meeting summary, by the section's
    # key. A section that is absent or empty uses its default. The sections themselves and
    # the rules that keep a summary safe to file are fixed in code, not in this text.
    summary_directions: dict[str, str] = Field(default_factory=dict)

    @field_validator("summary_directions")
    @classmethod
    def _directions_fit(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > SUMMARY_SECTIONS_MAX:
            raise ValueError("too many summary sections")
        for key, text in value.items():
            if not key or len(key) > 64:
                raise ValueError("a summary section key must be 1 to 64 characters")
            if len(text) > SUMMARY_DIRECTIONS_MAX:
                raise ValueError(f"directions for {key} are longer than {SUMMARY_DIRECTIONS_MAX}")
        return value

    # One worker serializes model calls and approvals. No unbounded fan-out.
    attempts: int = Field(default=2, ge=1, le=3)
    fields: list[str] = Field(default_factory=list, max_length=20)
    attendees_field: str = ""
    invitees_field: str = ""
    event_field: str = ""
    contacts_field: str = ""
    automatic_after: str = ""
    # Enrichment only: lets calendar descriptions go to the model, and only while it works
    # out which of several matching meetings a recording belongs to.
    send_calendar_descriptions: bool = False
    # Transcription only: the reply shape asked of the service. `diarized_json` carries a
    # speaker on every segment; `verbose_json` is for services that add one to Whisper's shape.
    response_format: Literal["diarized_json", "verbose_json"] = "diarized_json"
    # Transcription only: how long the service may take to answer one recording. A service
    # that does not stream sends nothing until it has finished, so this is the whole job.
    wait_s: int = Field(default=3600, ge=5, le=MAX_TRANSCRIBE_SECONDS)

    @field_validator("timeout_s", mode="before")
    @classmethod
    def legacy_default_timeout(cls, value: object) -> object:
        # 45 s was the old default, written into every saved configuration. It cannot cover
        # queueing plus prefill on a local model, so it reads as the new default.
        return 120 if value == 45 else value


class EmbeddingAssignment(ContextModel):
    """Which embeddings model search by meaning uses, and how patiently.

    Points at a connection like the enrichment assignment does, but names an
    embeddings model (an OpenAI-compatible `/v1/embeddings` endpoint). Turned
    off, search is full-text only and nothing is sent anywhere.
    """

    connection: str = ""
    model: str = Field(default="", max_length=200)
    enabled: bool = False
    # How long the service may stay silent before one request is given up on.
    timeout_s: int = Field(default=60, ge=5, le=600)
    attempts: int = Field(default=3, ge=1, le=5)


class ContextConfig(ContextModel):
    revision: int = 0
    dropbox_folder: str = "Dropbox"
    lifecycle_enabled: bool = False

    @field_validator("dropbox_folder")
    @classmethod
    def safe_dropbox(cls, value: str) -> str:
        return CalendarSource.folder_path(value)

    sources: list[CalendarSource] = Field(default_factory=list)
    connections: list[ModelConnection] = Field(default_factory=list)
    # Generated connection ID -> original storage identity for migrated connections.
    connection_aliases: dict[str, str] = Field(default_factory=dict)
    automatic_connection_ids: list[str] = Field(default_factory=list)
    assignments: dict[str, FeatureAssignment] = Field(
        default_factory=lambda: {"enrichment": FeatureAssignment()}
    )

    embeddings: EmbeddingAssignment = Field(default_factory=EmbeddingAssignment)

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
        if "enrichment" not in self.assignments or not set(self.assignments) <= {
            "enrichment",
            "transcription",
        }:
            raise ValueError("only enrichment and transcription are implemented")
        return self


def read_context_config(state_dir: Path) -> ContextConfig:
    path = state_dir / "context" / "config.json"
    return (
        ContextConfig.model_validate_json(path.read_bytes()) if path.exists() else ContextConfig()
    )
