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
from dataclasses import dataclass
from datetime import date, datetime, time
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

    def _date_satisfied_by_time(self, frontmatter: dict[str, Any]) -> bool:
        return parse_note_time(frontmatter.get("time")).date_value is not None

    def required_keys(self, frontmatter: dict[str, Any]) -> set[str]:
        """The keys this frontmatter must carry, conditional ones included.

        A key is required outright or because another key currently holds a
        value that asks for it, which is the same rule
        `validate_frontmatter` reports on.
        """
        date_key = self.role("date_key")
        has_time_date = self._date_satisfied_by_time(frontmatter)
        return {
            definition.name
            for definition in self.keys
            if (definition.required or _requires(definition, frontmatter))
            and not (definition.name == date_key and has_time_date)
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
        date_key = self.role("date_key")
        has_time_date = self._date_satisfied_by_time(frontmatter)
        account_key = self.role("account_key")
        for definition in self.keys:
            present = definition.name in frontmatter
            value = frontmatter.get(definition.name)
            if definition.name == account_key and present and value is not None:
                # One list field: a string is read as a one-item list, and an
                # empty list or blank text counts as no account at all.
                if not isinstance(value, str | list):
                    problems.append((definition.name, f"{definition.name}: expected a list"))
                    continue
                if any(
                    not isinstance(item, str)
                    for item in ([value] if isinstance(value, str) else value)
                ):
                    problems.append((definition.name, f"{definition.name}: expected text entries"))
                    continue
                if not account_list(value):
                    value, present = None, False
                else:
                    continue
            if definition.required and (not present or value is None):
                if definition.name == date_key and has_time_date:
                    continue
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


def account_list(value: Any) -> list[str]:
    """The customer names an `account` value carries.

    A plain string is a one-item list, a list keeps its non-blank text entries
    in order without repeats, and anything else carries none.
    """
    items = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    names: list[str] = []
    for item in items:
        if isinstance(item, str) and item.strip() and item.strip() not in names:
            names.append(item.strip())
    return names


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
    if definition.kind == "date":
        parsed = parse_note_time(value)
        if parsed.has_offset:
            return [f"{name}: must be a local date or date-and-time without a UTC offset"]
        if not _is_date(value):
            return [f"{name}: expected a date as YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS"]
    if definition.kind in {"string", "enum"} and isinstance(value, datetime):
        value = value.isoformat()
    if definition.kind in {"string", "enum"} and not isinstance(value, str):
        return [f"{name}: expected text"]
    if definition.kind == "enum" and definition.vocabulary and value not in definition.vocabulary:
        allowed = ", ".join(definition.vocabulary)
        return [f"{name}: {value!r} is not one of {allowed}"]
    return []


def _is_date(value: Any) -> bool:
    parsed = parse_note_time(value)
    return parsed.valid and parsed.date_value is not None


_LEGACY_TIME_PATTERN = re.compile(r"^\d{2}:\d{2}(?::\d{2})?$")
_LOCAL_DATETIME_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}(?::\d{2})?$")


@dataclass(frozen=True)
class ParsedNoteTime:
    """Parsed view of a note's `date` property, or of a legacy `time` property.

    `date` carries a note's day and, as a date-and-time, its local start time
    as `YYYY-MM-DDTHH:MM:SS` without a UTC offset, matching Obsidian's Date and
    Date & time properties. A legacy `time` of `HH:MM` (or `HH:MM:SS`) paired
    with a separate `date` remains accepted. A value carrying a UTC offset is
    flagged with `has_offset=True` and `valid=False` so review validation
    rejects it.
    """

    present: bool
    valid: bool
    date_value: date | None = None
    time_value: time | None = None
    has_offset: bool = False


def parse_date_value(value: Any) -> date | None:
    """Return a `date` when `value` is a valid `YYYY-MM-DD` date, else `None`."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        text = value.strip()
        if len(text) != 10:
            return None
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None
    return None


def parse_note_time(value: Any) -> ParsedNoteTime:
    """Parse a `time` frontmatter value in canonical datetime or legacy `HH:MM` form."""
    if value is None:
        return ParsedNoteTime(present=False, valid=False)
    if isinstance(value, datetime):
        tz_meta = getattr(value, "_yaml", None)
        has_tz = value.tzinfo is not None or (
            isinstance(tz_meta, dict) and tz_meta.get("tz") is not None
        )
        if has_tz:
            return ParsedNoteTime(
                present=True,
                valid=False,
                date_value=value.date(),
                time_value=value.timetz().replace(tzinfo=None, microsecond=0),
                has_offset=True,
            )
        return ParsedNoteTime(
            present=True,
            valid=True,
            date_value=value.date(),
            time_value=value.time().replace(microsecond=0),
        )
    if isinstance(value, date):
        return ParsedNoteTime(present=True, valid=True, date_value=value, time_value=None)
    if not isinstance(value, str):
        return ParsedNoteTime(present=True, valid=False)
    text = value.strip()
    if not text:
        return ParsedNoteTime(present=False, valid=False)
    if len(text) == 10 and text[4:5] == "-" and text[7:8] == "-":
        day = parse_date_value(text)
        return ParsedNoteTime(present=True, valid=day is not None, date_value=day, time_value=None)
    if len(text) > 10 and text[4:5] == "-" and text[7:8] == "-" and text[10:11] in {"T", "t", " "}:
        try:
            parsed_dt = datetime.fromisoformat(text)
        except ValueError:
            return ParsedNoteTime(present=True, valid=False)
        if parsed_dt.tzinfo is not None or text.endswith(("Z", "z")):
            return ParsedNoteTime(
                present=True,
                valid=False,
                date_value=parsed_dt.date(),
                time_value=parsed_dt.timetz().replace(tzinfo=None, microsecond=0),
                has_offset=True,
            )
        if not _LOCAL_DATETIME_PREFIX.fullmatch(text):
            return ParsedNoteTime(present=True, valid=False)
        return ParsedNoteTime(
            present=True,
            valid=True,
            date_value=parsed_dt.date(),
            time_value=parsed_dt.time().replace(microsecond=0),
        )
    try:
        parsed_time = time.fromisoformat(text)
    except ValueError:
        return ParsedNoteTime(present=True, valid=False)
    if parsed_time.tzinfo is not None or text.endswith(("Z", "z")):
        return ParsedNoteTime(
            present=True,
            valid=False,
            time_value=parsed_time.replace(tzinfo=None, microsecond=0),
            has_offset=True,
        )
    if not _LEGACY_TIME_PATTERN.fullmatch(text):
        return ParsedNoteTime(present=True, valid=False)
    return ParsedNoteTime(
        present=True,
        valid=True,
        date_value=None,
        time_value=parsed_time.replace(microsecond=0),
    )


def note_date(frontmatter: dict[str, Any], schema: FrontmatterSchema) -> date | None:
    """Return the note's effective day.

    `date` carries the day, and the start time too when it is a date-and-time.
    A legacy `time` that carries its own date (written by v0.6.0) is only the
    fallback for a note whose `date` is missing or unusable.
    """
    explicit = parse_note_time(frontmatter.get(schema.role("date_key")))
    if explicit.valid and explicit.date_value is not None:
        return explicit.date_value
    parsed = parse_note_time(frontmatter.get("time"))
    return parsed.date_value if parsed.valid else None


def note_date_mismatch(
    frontmatter: dict[str, Any], schema: FrontmatterSchema
) -> tuple[date, date] | None:
    """Return `(date_field, time_date)` when both are valid and disagree."""
    parsed = parse_note_time(frontmatter.get("time"))
    explicit = parse_note_time(frontmatter.get(schema.role("date_key")))
    if (
        parsed.valid
        and parsed.date_value is not None
        and explicit.valid
        and explicit.date_value is not None
        and explicit.date_value != parsed.date_value
    ):
        return explicit.date_value, parsed.date_value
    return None


def note_start_time(frontmatter: dict[str, Any], schema: FrontmatterSchema) -> time | None:
    """The note's local start time: from `date` when it is a date-and-time.

    A legacy `time` (`HH:MM`, or a full datetime) is the fallback, so notes
    written before `date` carried the time keep their start time.
    """
    explicit = parse_note_time(frontmatter.get(schema.role("date_key")))
    if explicit.valid and explicit.date_value is not None and explicit.time_value is not None:
        return explicit.time_value
    legacy = parse_note_time(frontmatter.get("time"))
    return legacy.time_value if legacy.valid else None


def date_field_problem(frontmatter: dict[str, Any], schema: FrontmatterSchema) -> str | None:
    """Why the note's `date` or legacy `time` cannot be used at review, or None."""
    date_key = schema.role("date_key")
    raw = frontmatter.get(date_key)
    if raw is not None and (not isinstance(raw, str) or raw.strip()):
        parsed = parse_note_time(raw)
        if parsed.has_offset:
            return f"{date_key} must be a local date or date-and-time without a UTC offset"
        if not parsed.valid or parsed.date_value is None:
            return "expected a valid date"
    if parse_note_time(frontmatter.get("time")).has_offset:
        return "time must be a local datetime without a UTC offset"
    return None


def format_note_time(value: datetime) -> str:
    """Format a datetime as canonical local `YYYY-MM-DDTHH:MM:SS` with no offset."""
    return value.strftime("%Y-%m-%dT%H:%M:%S")


def normalize_note_time(value: Any, *, fallback_date: date | None = None) -> str:
    """Return canonical `YYYY-MM-DDTHH:MM:SS` when `value` is a valid local time/datetime."""
    parsed = parse_note_time(value)
    if not parsed.valid:
        return str(value).strip() if value is not None else ""
    day = parsed.date_value or fallback_date
    clock = parsed.time_value or time(0, 0, 0)
    if day is not None and (parsed.date_value is not None or parsed.time_value is not None):
        return datetime.combine(day, clock).strftime("%Y-%m-%dT%H:%M:%S")
    if parsed.time_value is not None:
        return parsed.time_value.strftime("%H:%M:%S")
    return str(value).strip()


def field_suggestion_changes(
    frontmatter: dict[str, Any], schema: FrontmatterSchema, field: str, value: Any
) -> tuple[dict[str, Any], list[str]]:
    """The `(set, unset)` patch that applying a suggested field value makes.

    A date or legacy time suggestion writes `date` only and removes the legacy
    `time` and `timezone`. An account suggestion is added to the note's list:
    entries already there keep their place and none is removed.
    """
    date_key = schema.role("date_key")
    if field in {date_key, "time"}:
        parsed = parse_note_time(value)
        written = (
            normalize_note_time(value, fallback_date=note_date(frontmatter, schema))
            if parsed.valid and parsed.time_value is not None
            else value
        )
        return {date_key: written}, ["time", "timezone"]
    account_key = schema.role("account_key")
    if field == account_key:
        names = account_list(frontmatter.get(account_key))
        names += [name for name in account_list(value) if name not in names]
        return {account_key: names}, []
    return {field: value}, []


def previous_default_keys() -> dict[str, KeyDefinition]:
    """The `account` and `date` definitions earlier releases shipped, by key name."""
    return {
        "account": KeyDefinition(
            name="account",
            kind="string",
            required=False,
            required_when="context",
            required_when_values=["external"],
            description="Customer name. Required when context is external.",
        ),
        "date": KeyDefinition(
            name="date",
            kind="date",
            required=False,
            description="Optional note date (YYYY-MM-DD); derived from time when omitted.",
        ),
    }


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
                required=False,
                description=(
                    "Note date, YYYY-MM-DD, or date and local start time, "
                    "YYYY-MM-DDTHH:MM:SS with no UTC offset."
                ),
            ),
            KeyDefinition(
                name="type",
                kind="enum",
                required=True,
                default="reference",
                vocabulary=["work", "personal", "reference", "journal"],
                description="What the note is.",
            ),
            KeyDefinition(
                name="context",
                kind="enum",
                required=True,
                default="internal",
                vocabulary=["internal", "external", "personal"],
                description="Where the note files.",
            ),
            KeyDefinition(
                name="account",
                kind="list",
                required=False,
                required_when="context",
                required_when_values=["external"],
                description=(
                    "Customer names, a list. A plain name is read as a one-item list. "
                    "Needs at least one entry when context is external."
                ),
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
