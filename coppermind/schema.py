"""The frontmatter schema: which keys a note carries and what they may hold.

The schema is product data, not code. It ships with the defaults below, it is
stored as `/data/state/schema.yaml`, and Admin edits it. Code never refers to
a key by its literal name; it asks the schema for the key that plays a role
("which key holds the identifier?"). Renaming `account` to `customer` is then
a change to the role map plus a migration job over the notes filesystem, not a
code change.

Tags are data here too. `tags` lists the tags the operator has described,
each with a one-line meaning; `tag_aliases` maps a spelling to the canonical
tag it stands for, and every store write path replaces an alias with its
canonical tag. `tag_mode` decides what an unlisted tag means: `open` (the
default) accepts and counts it, `closed` refuses it on a write that adds it.
Tags are organic and standardized after the fact, so a closed mode never
refuses a note for a tag it already carried.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, model_validator

KeyKind = Literal["string", "int", "date", "bool", "enum", "list"]
TagMode = Literal["open", "closed"]

# A tag or an alias: no whitespace (Obsidian ends a tag there), no comma (the
# Admin form separates aliases with them), and no leading `#`, which is how a
# tag is written in a note body rather than in frontmatter.
TAG_PATTERN = re.compile(r"[^\s,#][^\s,]*")
# A key an operator adds from Admin. Existing keys keep whatever name they have.
KEY_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")

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
    # One line per allowed value saying what choosing it means.
    vocabulary_meanings: dict[str, str] = Field(default_factory=dict)
    description: str = ""
    # A paragraph for whoever fills the key in: a person, an agent, or the
    # enricher. It changes nothing about validation.
    guidance: str = ""
    # Name of another key that must be present when this key has a value in
    # `required_when_values`. This is how "account is required for a customer
    # note" is expressed as data instead of code.
    required_when: str | None = None
    required_when_values: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_meanings(self) -> Self:
        stray = [value for value in self.vocabulary_meanings if value not in self.vocabulary]
        if stray:
            raise ValueError(
                f"{self.name}: meanings given for values that are not allowed: {', '.join(stray)}"
            )
        return self


class FrontmatterSchema(BaseModel):
    """The shipped schema and its Admin editable form."""

    schema_version: int = 1
    keys: list[KeyDefinition]
    roles: dict[str, str]
    # Listed tags and the meaning of each.
    tags: dict[str, str] = Field(default_factory=dict)
    # alias -> canonical tag.
    tag_aliases: dict[str, str] = Field(default_factory=dict)
    tag_mode: TagMode = "open"

    @model_validator(mode="after")
    def validate_tags(self) -> Self:
        bad = [tag for tag in [*self.tags, *self.tag_aliases] if not TAG_PATTERN.fullmatch(tag)]
        bad += [tag for tag in self.tag_aliases.values() if not TAG_PATTERN.fullmatch(tag)]
        if bad:
            raise ValueError(
                "tags may not be empty, contain spaces or commas, or start with #: "
                + ", ".join(repr(tag) for tag in bad)
            )
        for alias, canonical in self.tag_aliases.items():
            if alias == canonical:
                raise ValueError(f"tag alias {alias!r} names itself")
            if alias in self.tags:
                raise ValueError(f"tag alias {alias!r} is also a listed tag")
            if canonical in self.tag_aliases:
                raise ValueError(
                    f"tag alias {alias!r} points at {canonical!r}, which is itself an alias"
                )
            if self.tag_mode == "closed" and canonical not in self.tags:
                raise ValueError(
                    f"tag alias {alias!r} points at {canonical!r}, "
                    "which closed tags would refuse because it is not listed"
                )
        return self

    @model_validator(mode="after")
    def validate_roles(self) -> Self:
        missing = [role for role in ROLE_NAMES if role not in self.roles]
        if missing:
            raise ValueError(f"schema is missing required roles: {', '.join(missing)}")
        key_names = {definition.name for definition in self.keys}
        unknown = [
            f"{role}={self.roles[role]}" for role in ROLE_NAMES if self.roles[role] not in key_names
        ]
        if unknown:
            raise ValueError(f"schema roles reference undefined keys: {', '.join(unknown)}")
        return self

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

    def required_keys(self, frontmatter: dict[str, Any]) -> set[str]:
        """The keys this frontmatter must carry, conditional ones included.

        A key is required outright or because another key currently holds a
        value that asks for it, which is the same rule
        `validate_frontmatter` reports on.
        """
        return {
            definition.name
            for definition in self.keys
            if definition.required or _requires(definition, frontmatter)
        }

    def validate_frontmatter(self, frontmatter: dict[str, Any]) -> list[str]:
        """Return a list of human readable problems, empty when the note is valid.

        A problem quotes the note's own value, so it belongs where note content
        belongs: an answer to whoever sent the note, never an operational log.
        `invalid_keys` is the form that may be logged.

        Unknown keys are not problems. They are passed through untouched, so a
        person can keep their own keys in a note without Coppermind objecting.
        """
        return [problem for _, problem in self._problems(frontmatter)]

    def invalid_keys(self, frontmatter: dict[str, Any]) -> list[str]:
        """The names of the keys a note fails on, carrying none of its content.

        Key names come from this schema rather than from the note, which is what
        makes them safe to log next to a note's path.
        """
        return sorted({name for name, _ in self._problems(frontmatter)})

    def _problems(self, frontmatter: dict[str, Any]) -> list[tuple[str, str]]:
        problems: list[tuple[str, str]] = []
        for definition in self.keys:
            present = definition.name in frontmatter
            value = frontmatter.get(definition.name)
            if definition.required and (not present or value is None):
                problems.append((definition.name, f"{definition.name}: required"))
                continue
            if not present or value is None:
                if definition.required_when and _requires(definition, frontmatter):
                    problems.append(
                        (
                            definition.name,
                            f"{definition.name}: required when "
                            f"{definition.required_when} is one of "
                            f"{', '.join(definition.required_when_values)}",
                        )
                    )
                continue
            problems.extend(
                (definition.name, problem) for problem in _check_value(definition, value)
            )
        return problems

    def edit_problems(self) -> list[str]:
        """Problems an Admin edit must not save, beyond what the model refuses.

        These are not model validators because a schema file written before
        they existed must keep loading: the store reads it on every write.
        """
        problems: list[str] = []
        names = [definition.name for definition in self.keys]
        problems += [
            f"{name}: defined more than once"
            for name in sorted(set(names))
            if names.count(name) > 1
        ]
        for definition in self.keys:
            name = definition.name
            if definition.kind == "enum" and not definition.vocabulary:
                problems.append(f"{name}: an enum needs at least one allowed value")
            if definition.vocabulary and definition.kind not in {"enum", "string"}:
                problems.append(f"{name}: allowed values apply to enum and string keys only")
            if definition.required_when is None:
                if definition.required_when_values:
                    problems.append(f"{name}: required when values given without a key to watch")
                continue
            if definition.required_when == name:
                problems.append(f"{name}: cannot be required because of itself")
            elif definition.required_when not in names:
                problems.append(
                    f"{name}: required when names {definition.required_when!r}, which is not a key"
                )
            if not definition.required_when_values:
                problems.append(f"{name}: required when needs at least one value")
        return problems

    def canonical_tag(self, tag: Any) -> Any:
        """The canonical tag for `tag`: itself unless it is an alias."""
        return self.tag_aliases.get(tag, tag) if isinstance(tag, str) else tag

    def normalize_tags(self, tags: list[Any]) -> list[Any]:
        """Replace each alias with its canonical tag and drop repeats, keeping order.

        Order is kept because it is the person's: a tag list already free of
        aliases and repeats comes back equal, so normalizing it writes nothing.
        A value that is not text is left as it is.
        """
        normalized: list[Any] = []
        for tag in tags:
            canonical = self.canonical_tag(tag)
            if canonical not in normalized:
                normalized.append(canonical)
        return normalized

    def tag_problems(
        self, frontmatter: dict[str, Any], current: dict[str, Any] | None = None
    ) -> list[str]:
        """Closed tags: the unlisted tags a write would add, as problems.

        `current` is the frontmatter the note already has. A tag it carries is
        never refused, so a closed mode cannot stop a person from changing an
        unrelated key of a note tagged before the mode was closed. Open tags
        accept everything.
        """
        if self.tag_mode != "closed":
            return []
        key = self.role("tags_key")
        tags = frontmatter.get(key)
        if not isinstance(tags, list):
            return []
        carried = current.get(key) if current else None
        already = (
            {self.canonical_tag(tag) for tag in carried} if isinstance(carried, list) else set()
        )
        return [
            f"{key}: {tag!r} is not a listed tag, and tags are closed"
            for tag in self.normalize_tags(tags)
            if tag not in self.tags and tag not in already
        ]

    def aliases_of(self, tag: str) -> list[str]:
        """The aliases that normalize to `tag`, in the order they were defined."""
        return [alias for alias, canonical in self.tag_aliases.items() if canonical == tag]


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
