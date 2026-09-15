"""API key records and the fixed public scope vocabulary.

The recoverable credential is shown once when a key is created. Persistent
control state keeps only an Argon2 hash of its secret, so a backup of
``keys.json`` cannot be used as an API credential.
"""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

API_SCOPES = (
    "sources:read",
    "sources:write",
    "notes:read",
    "notes:write",
    "notes:move",
    "notes:delete",
    "journal:read",
    "journal:write",
    "search:read",
    "admin:read",
    "admin:write",
)

KEY_ID_PATTERN = re.compile(r"^[a-z0-9]{16}$")
KEY_PREFIX = "cm_"
_HASHER = PasswordHasher()


class ApiKeyRecord(BaseModel):
    """One non-recoverable key record from ``keys.json``."""

    model_config = ConfigDict(extra="forbid")

    key_id: str
    name: str = Field(min_length=1)
    hash: str
    scopes: list[str]
    created_at: datetime
    revoked_at: datetime | None = None

    @field_validator("key_id")
    @classmethod
    def validate_key_id(cls, value: str) -> str:
        if not KEY_ID_PATTERN.fullmatch(value):
            raise ValueError("key_id must be 16 lowercase letters or digits")
        return value

    @field_validator("scopes")
    @classmethod
    def validate_scopes(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - set(API_SCOPES))
        if unknown:
            raise ValueError(f"unknown API scopes: {', '.join(unknown)}")
        if len(value) != len(set(value)):
            raise ValueError("API scopes must not be repeated")
        return value


class ApiKeySet(BaseModel):
    """The revisioned contents of ``keys.json``."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    revision: int = 1
    keys: list[ApiKeyRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_key_ids(self) -> ApiKeySet:
        key_ids = [record.key_id for record in self.keys]
        if len(key_ids) != len(set(key_ids)):
            raise ValueError("API key ids must be unique")
        return self


def split_credential(credential: str) -> tuple[str, str] | None:
    """Return the key id and secret when a credential has the public form."""
    if not credential.startswith(KEY_PREFIX):
        return None
    parts = credential.split("_", 2)
    if len(parts) != 3 or not KEY_ID_PATTERN.fullmatch(parts[1]) or not parts[2]:
        return None
    return parts[1], parts[2]


def create_key(
    name: str,
    scopes: list[str],
    *,
    key_id: str | None = None,
    secret: str | None = None,
    created_at: datetime | None = None,
) -> tuple[ApiKeyRecord, str]:
    """Create a key record and the credential that can be shown once."""
    actual_key_id = key_id or secrets.token_hex(8)
    actual_secret = secret or secrets.token_urlsafe(32)
    record = ApiKeyRecord(
        key_id=actual_key_id,
        name=name,
        hash=_HASHER.hash(actual_secret),
        scopes=scopes,
        created_at=created_at or datetime.now(tz=UTC),
    )
    return record, f"{KEY_PREFIX}{actual_key_id}_{actual_secret}"


def verify_secret(encoded_hash: str, secret: str) -> bool:
    """Verify a presented secret without allowing a malformed hash to escape."""
    try:
        return _HASHER.verify(encoded_hash, secret)
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return False
