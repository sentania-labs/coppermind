"""The frontmatter schema: which keys a note carries and what they may hold.

The schema is product data, not code. It ships with the defaults below, it is
stored as `/data/state/schema.yaml`, and Admin edits it. Code never refers to
a key by its literal name; it asks the schema for the key that plays a role
("which key holds the identifier?"). Renaming `account` to `customer` is then
a change to the role map plus a migration job over the notes filesystem, not a
code change.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

KeyKind = Literal["string", "int", "date", "bool", "enum", "list"]

# Roles the rest of the system asks about by name.
ROLE_NAMES = (
    "id_key",
    "date_key",
    "type_key",
    "context_key",
    "account_key",
    "reviewed_key",
    "sources_key",
    "tags_key",
    "schema_version_key",
)


class KeyDefinition(BaseModel):
    """One frontmatter key."""

    name: str
    kind: KeyKind = "string"
    required: bool = False
    default: Any = None
    vocabulary: list[str] = Field(default_factory=list)
    description: str = ""
    # Name of another key that must be present when this key has a value in
    # `required_when_values`. This is how "account is required for a customer
    # note" is expressed as data instead of code.
    required_when: str | None = None
    required_when_values: list[str] = Field(default_factory=list)


class FrontmatterSchema(BaseModel):
    """The shipped schema and its Admin editable form."""

    schema_version: int = 1
    keys: list[KeyDefinition]
    roles: dict[str, str]

    def key(self, name: str) -> KeyDefinition | None:
        return next((k for k in self.keys if k.name == name), None)

    def role(self, role: str) -> str:
        """Return the key name playing `role`."""
        try:
            return self.roles[role]
        except KeyError as exc:  # pragma: no cover - a malformed schema file
            raise KeyError(f"schema has no key for role {role!r}") from exc

    def defaults(self) -> dict[str, Any]:
        """Frontmatter defaults for a newly created note."""
        out: dict[str, Any] = {}
        for definition in self.keys:
            if definition.default is not None:
                out[definition.name] = (
                    list(definition.default)
                    if isinstance(definition.default, list)
                    else definition.default
                )
        return out

    def validate_frontmatter(self, frontmatter: dict[str, Any]) -> list[str]:
        """Return a list of human readable problems, empty when the note is valid.

        Unknown keys are not problems. They are passed through untouched, so a
        person can keep their own keys in a note without Coppermind objecting.
        """
        problems: list[str] = []
        for definition in self.keys:
            present = definition.name in frontmatter
            value = frontmatter.get(definition.name)
            if definition.required and (not present or value is None):
                problems.append(f"{definition.name}: required")
                continue
            if not present or value is None:
                if definition.required_when and _requires(definition, frontmatter):
                    problems.append(
                        f"{definition.name}: required when "
                        f"{definition.required_when} is one of "
                        f"{', '.join(definition.required_when_values)}"
                    )
                continue
            problems.extend(_check_value(definition, value))
        return problems


def _requires(definition: KeyDefinition, frontmatter: dict[str, Any]) -> bool:
    if not definition.required_when:
        return False
    other = frontmatter.get(definition.required_when)
    return isinstance(other, str) and other in definition.required_when_values


def _check_value(definition: KeyDefinition, value: Any) -> list[str]:
    name = definition.name
    if definition.kind == "bool" and not isinstance(value, bool):
        return [f"{name}: expected true or false"]
    if definition.kind == "int" and (isinstance(value, bool) or not isinstance(value, int)):
        return [f"{name}: expected a whole number"]
    if definition.kind == "list" and not isinstance(value, list):
        return [f"{name}: expected a list"]
    if definition.kind == "date" and not _is_date(value):
        return [f"{name}: expected a date as YYYY-MM-DD"]
    if definition.kind in {"string", "enum"} and not isinstance(value, str):
        return [f"{name}: expected text"]
    if definition.kind == "enum" and definition.vocabulary and value not in definition.vocabulary:
        allowed = ", ".join(definition.vocabulary)
        return [f"{name}: {value!r} is not one of {allowed}"]
    return []


def _is_date(value: Any) -> bool:
    if isinstance(value, date) and not isinstance(value, datetime):
        return True
    if isinstance(value, str):
        try:
            date.fromisoformat(value)
        except ValueError:
            return False
        return True
    return False


def default_schema() -> FrontmatterSchema:
    """The schema a fresh install runs with. Admin can change every part of it."""
    return FrontmatterSchema(
        schema_version=1,
        keys=[
            KeyDefinition(
                name="schema_version",
                kind="int",
                required=True,
                default=1,
                description="Note file format version.",
            ),
            KeyDefinition(
                name="id",
                kind="string",
                required=True,
                description="Permanent identifier. Never edit this by hand.",
            ),
            KeyDefinition(
                name="date",
                kind="date",
                required=True,
                description="The day the note is about.",
            ),
            KeyDefinition(
                name="type",
                kind="enum",
                required=True,
                default="note",
                vocabulary=["meeting", "journal", "reference", "note"],
                description="What the note is.",
            ),
            KeyDefinition(
                name="context",
                kind="enum",
                required=True,
                default="internal",
                vocabulary=["customer", "internal", "external", "personal"],
                description="Where the note files.",
            ),
            KeyDefinition(
                name="account",
                kind="string",
                required=False,
                required_when="context",
                required_when_values=["customer"],
                description="Customer name. Required when context is customer.",
            ),
            KeyDefinition(
                name="reviewed",
                kind="bool",
                required=True,
                default=False,
                description="Set to true on a device when the note has been read and corrected.",
            ),
            KeyDefinition(
                name="sources",
                kind="list",
                required=True,
                default=[],
                description="Identifiers of the source bundles this note came from.",
            ),
            KeyDefinition(
                name="tags",
                kind="list",
                required=False,
                default=[],
                description="Free tags.",
            ),
        ],
        roles={
            "schema_version_key": "schema_version",
            "id_key": "id",
            "date_key": "date",
            "type_key": "type",
            "context_key": "context",
            "account_key": "account",
            "reviewed_key": "reviewed",
            "sources_key": "sources",
            "tags_key": "tags",
        },
    )
