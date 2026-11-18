"""API key records and the fixed public scope vocabulary.

The recoverable credential is shown once when a key is created. Persistent
control state keeps only an Argon2 hash of its secret, so a backup of
``keys.json`` cannot be used as an API credential.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterable
from datetime import UTC, datetime

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from coppermind.statefiles import RevisionConflict, StateStore

API_SCOPES = (
    "entities:read",
    "entities:write",
    "sources:read",
    "sources:write",
    "notes:read",
    "notes:write",
    "notes:move",
    "notes:delete",
    "artifacts:read",
    "artifacts:write",
    "journal:read",
    "journal:write",
    "search:read",
    "admin:read",
    "admin:write",
    "admin:purge",
)
DEFAULT_SCOPES = tuple(
    scope for scope in API_SCOPES if scope not in {"admin:purge", "entities:read", "entities:write"}
)

# What a connected app may do, in the words Admin shows, and the scopes each one grants.
# One list, so the screen that grants a permission and the screen that shows it agree.
PERMISSIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("read", "Read notes", ("notes:read", "sources:read", "artifacts:read", "journal:read")),
    ("write", "Add and change notes", ("notes:write", "notes:move", "journal:write")),
    ("delete", "Delete notes", ("notes:delete",)),
    ("hand", "Hand in recordings and files", ("sources:write", "artifacts:write")),
    ("search", "Search", ("search:read",)),
    ("people", "Read and correct people and companies", ("entities:read", "entities:write")),
    ("admin", "Change Coppermind settings", ("admin:read", "admin:write")),
)
# What each kind of app is given when it is connected.
COMPANION_PERMISSIONS = ("read", "write", "hand", "search", "people")
PRESETS: dict[str, tuple[str, ...]] = {
    "companion": COMPANION_PERMISSIONS,
    "agent": ("read", "search", "people"),
    "intake": ("read", "write", "hand"),
}


def scopes_for(permissions: Iterable[str]) -> list[str]:
    """The scopes a set of permissions grants, in the fixed order of the vocabulary."""
    wanted = set(permissions)
    granted = {scope for key, _, scopes in PERMISSIONS if key in wanted for scope in scopes}
    return [scope for scope in API_SCOPES if scope in granted]


def changed_scopes(current: Iterable[str], permissions: Iterable[str]) -> list[str]:
    """The scopes a key holds after its ticks are saved: a tick adds its scopes, an untick
    removes only a permission the key held in full, and every other scope is left alone."""
    held = set(current)
    full = set(permissions_of(held))
    wanted = set(permissions)
    for key, _, scopes in PERMISSIONS:
        if key in wanted:
            held |= set(scopes)
        elif key in full:
            held -= set(scopes)
    return [scope for scope in API_SCOPES if scope in held]


def permissions_of(scopes: Iterable[str]) -> list[str]:
    """The permissions a key holds in full. A scope outside every permission adds nothing."""
    held = set(scopes)
    return [key for key, _, needed in PERMISSIONS if set(needed) <= held]


def describe(scopes: Iterable[str]) -> str:
    """What a key may do, as one sentence fragment."""
    scopes = list(scopes)
    held = permissions_of(scopes)
    if len(held) == len(PERMISSIONS):
        return "Everything, including settings"
    if set(held) == set(COMPANION_PERMISSIONS):
        return "Read and change notes, hand in recordings, search"
    labels = [label for key, label, _ in PERMISSIONS if key in held]
    if not labels:
        return "Limited access set outside Admin" if scopes else "Nothing yet"
    return labels[0] + "".join(", " + label[0].lower() + label[1:] for label in labels[1:])


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
    # The durable mark of the key bootstrap owns. A display name is an
    # operator's to choose, so it can never stand in for this.
    bootstrap_default: bool = False
    # Set on a key the Obsidian companion was given when it paired with a code, with the
    # name of the device it paired on. Such a key was never shown to anyone.
    companion: bool = False
    device: str = ""

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
    bootstrap_default: bool = False,
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
        bootstrap_default=bootstrap_default,
    )
    return record, f"{KEY_PREFIX}{actual_key_id}_{actual_secret}"


def verify_secret(encoded_hash: str, secret: str) -> bool:
    """Verify a presented secret without allowing a malformed hash to escape."""
    try:
        return _HASHER.verify(encoded_hash, secret)
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return False


def load_keys(store: StateStore) -> ApiKeySet:
    """Read current key state; revision zero denotes an absent file."""
    try:
        return ApiKeySet.model_validate(store.read("keys").body)
    except FileNotFoundError:
        return ApiKeySet(revision=0)


def add_key(
    store: StateStore,
    name: str,
    scopes: list[str],
    *,
    if_revision: int,
    companion: bool = False,
    device: str = "",
) -> str:
    """Append a key at the displayed revision and reveal its credential once."""
    key_set = load_keys(store)
    if key_set.revision != if_revision:
        raise RevisionConflict(key_set.revision)
    record, credential = create_key(name, scopes)
    record.companion, record.device = companion, device
    key_set.keys.append(record)
    store.write("keys", key_set.model_dump(mode="json"), if_revision=if_revision or None)
    return credential


def change_key(
    store: StateStore,
    key_id: str,
    *,
    name: str | None = None,
    scopes: list[str] | None = None,
    new_secret: bool = False,
) -> str:
    """Rename a live key, change what it may do, or replace its secret, in place.

    Returns the new credential when the secret was replaced, and an empty string
    otherwise. The key keeps its id, so the app it belongs to keeps its line.
    """
    key_set = load_keys(store)
    record = next((key for key in key_set.keys if key.key_id == key_id), None)
    if record is None or record.revoked_at is not None:
        raise ValueError(f"no live API key with id {key_id}")
    credential = ""
    if name is not None:
        if not name.strip():
            raise ValueError("a key needs a name")
        record.name = name.strip()
    if scopes is not None:
        record.scopes = ApiKeyRecord.validate_scopes(scopes)
    if new_secret:
        secret = secrets.token_urlsafe(32)
        record.hash = _HASHER.hash(secret)
        credential = f"{KEY_PREFIX}{record.key_id}_{secret}"
    store.write("keys", key_set.model_dump(mode="json"), if_revision=key_set.revision)
    return credential


def remove_key(store: StateStore, key_id: str) -> ApiKeyRecord:
    """Revoke a key now, whatever revision the caller last saw, and return its record."""
    key_set = load_keys(store)
    record = next((key for key in key_set.keys if key.key_id == key_id), None)
    if record is None:
        raise ValueError(f"no API key with id {key_id}")
    if record.revoked_at is None:
        record.revoked_at = datetime.now(tz=UTC)
        store.write("keys", key_set.model_dump(mode="json"), if_revision=key_set.revision)
    return record


def revoke_key(store: StateStore, key_id: str, *, if_revision: int) -> None:
    """Revoke a key at the displayed revision, retaining its audit record."""
    key_set = load_keys(store)
    if key_set.revision != if_revision:
        raise RevisionConflict(key_set.revision)
    record = next((key for key in key_set.keys if key.key_id == key_id), None)
    if record is None:
        raise ValueError(f"no API key with id {key_id}")
    if record.revoked_at is None:
        record.revoked_at = datetime.now(tz=UTC)
        store.write("keys", key_set.model_dump(mode="json"), if_revision=if_revision)
