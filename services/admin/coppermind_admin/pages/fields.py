"""Fields and tags: the frontmatter schema's guidance and tag rules, edited by revision.

Fields come from the schema file's key definitions: add a key, edit its kind,
whether it is required (outright or when another key holds certain values),
its allowed values each with a one-line meaning, its default and its guidance
paragraph, or retire it. A retired key leaves the schema and nothing else:
notes that carry it keep the value, which the store then passes through as an
unknown key. Keys that play a role cannot be retired here, and the role map is
carried over from the file untouched; renaming a key is a separate migration.

Tags are data: every tag the notes mirror records is listed with how many
notes carry it, beside the tags the operator has listed with a meaning and
aliases, and the open or closed setting. Counts come from the store over its
internal contract, because Admin has no database; when the store cannot
answer, the rules stay editable and the page says the counts are missing.

A save states the revision it read, exactly as Settings does, so a second tab
cannot silently overwrite the first. The schema file under `/data/state` stays
the one source of truth and the store reads it fresh on every write.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from html import escape
from typing import Any, get_args

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import ValidationError

from coppermind.schema import (
    KEY_NAME_PATTERN,
    FrontmatterSchema,
    KeyDefinition,
    KeyKind,
    TagMode,
    default_schema,
)
from coppermind.settings import Wiring
from coppermind.statefiles import RevisionConflict, StateFile, StateStore
from coppermind.store_client import HttpStoreClient
from coppermind.store_protocol import StoreError, TagCount

router = APIRouter()

TagCounter = Callable[[], Awaitable[list[TagCount]]]
KINDS: tuple[str, ...] = get_args(KeyKind)
TAG_MODES: tuple[str, ...] = get_args(TagMode)
# Kinds whose default is typed as plain text rather than as JSON.
TEXT_DEFAULTS = {"string", "enum", "date"}
# Rows a submission may name. The form body is already bounded in bytes; this
# keeps a hand-built post from asking for a loop over millions of indexes.
MAX_ROWS = 2000

COUNTS_UNAVAILABLE = (
    "Note counts are unavailable because the store did not answer. "
    "Tags, meanings and aliases can still be edited."
)


def store_tag_counter(wiring: Wiring) -> TagCounter:
    """Ask the store for tag counts over the internal contract."""

    async def counts() -> list[TagCount]:
        client = HttpStoreClient(wiring.store_url, wiring.read_internal_token(), timeout=5.0)
        try:
            return await client.list_tags()
        finally:
            await client.aclose()

    return counts


async def tag_counts(request: Request) -> dict[str, int] | None:
    """Notes per tag from the mirror, or None when the store cannot say."""
    counter: TagCounter = request.app.state.tag_counts
    try:
        return {count.tag: count.count for count in await counter()}
    except (StoreError, OSError):
        return None


def read_schema(store: StateStore) -> tuple[StateFile, FrontmatterSchema]:
    # Bootstrap and the store both write the shipped schema before Admin
    # starts; ensure keeps a fresh state directory working all the same.
    state = store.ensure("schema", default_schema().model_dump(mode="json"))
    body = {key: value for key, value in state.body.items() if key != "revision"}
    return state, FrontmatterSchema.model_validate(body)


def shown_default(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value)


def key_rows(schema: FrontmatterSchema) -> list[dict[str, Any]]:
    """One editable row per key, then a blank row for adding one."""
    rows: list[dict[str, Any]] = []
    for definition in schema.keys:
        values = "\n".join(
            f"{value}: {definition.vocabulary_meanings[value]}"
            if definition.vocabulary_meanings.get(value)
            else value
            for value in definition.vocabulary
        )
        rows.append(
            {
                "name": definition.name,
                "new": False,
                "kind": definition.kind,
                "required": definition.required,
                "required_when": definition.required_when or "",
                "required_when_values": ", ".join(definition.required_when_values),
                "values": values,
                "default": shown_default(definition.default),
                "description": definition.description,
                "guidance": definition.guidance,
                "retire": False,
            }
        )
    rows.append(blank_key_row())
    return rows


def blank_key_row() -> dict[str, Any]:
    return {
        "name": "",
        "new": True,
        "kind": "string",
        "required": False,
        "required_when": "",
        "required_when_values": "",
        "values": "",
        "default": "",
        "description": "",
        "guidance": "",
        "retire": False,
    }


def tag_rows(schema: FrontmatterSchema, counts: dict[str, int] | None) -> list[dict[str, Any]]:
    """Listed tags in their order, then unlisted tags in use, then a blank row.

    A tag in use that is an alias is not a row of its own: its notes are shown
    against the tag it normalizes to.
    """
    names = list(schema.tags)
    names += [tag for tag in dict.fromkeys(schema.tag_aliases.values()) if tag not in names]
    names += sorted(
        tag for tag in counts or {} if tag not in names and tag not in schema.tag_aliases
    )
    rows = [
        {
            "name": tag,
            "new": False,
            # A tag an alias points at is listed, so a save keeps the alias.
            "listed": tag in schema.tags or bool(schema.aliases_of(tag)),
            "meaning": schema.tags.get(tag, ""),
            "aliases": ", ".join(schema.aliases_of(tag)),
        }
        for tag in names
    ]
    rows.append(blank_tag_row())
    return rows


def blank_tag_row() -> dict[str, Any]:
    return {"name": "", "new": True, "listed": True, "meaning": "", "aliases": ""}


def submitted_rows(body: dict[str, str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The key and tag rows exactly as a form sent them."""
    keys = [
        {
            "name": body.get(f"key-{i}-name", "").strip(),
            "new": body.get(f"key-{i}-new") == "1",
            "kind": body.get(f"key-{i}-kind", "string"),
            "required": body.get(f"key-{i}-required") == "on",
            "required_when": body.get(f"key-{i}-required_when", "").strip(),
            "required_when_values": body.get(f"key-{i}-required_when_values", ""),
            "values": body.get(f"key-{i}-values", ""),
            "default": body.get(f"key-{i}-default", ""),
            "description": body.get(f"key-{i}-description", ""),
            "guidance": body.get(f"key-{i}-guidance", ""),
            "retire": body.get(f"key-{i}-retire") == "on",
        }
        for i in range(row_count(body, "key-count"))
    ]
    tags = [
        {
            "name": body.get(f"tag-{i}-name", "").strip(),
            "new": body.get(f"tag-{i}-new") == "1",
            "listed": body.get(f"tag-{i}-listed") == "on",
            "meaning": body.get(f"tag-{i}-meaning", ""),
            "aliases": body.get(f"tag-{i}-aliases", ""),
        }
        for i in range(row_count(body, "tag-count"))
    ]
    return keys, tags


def row_count(body: dict[str, str], name: str) -> int:
    try:
        count = int(body.get(name, ""))
    except ValueError:
        return 0
    return max(0, min(count, MAX_ROWS))


def split_list(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


def parsed_default(row: dict[str, Any]) -> Any:
    text = row["default"].strip()
    if not text:
        return None
    if row["kind"] in TEXT_DEFAULTS:
        return text
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ValueError(
            f"{row['name']}: default: enter a JSON number, true or false, or a list"
        ) from exc


def allowed_values(row: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    """`value: meaning` lines, one per allowed value; the meaning is optional."""
    vocabulary: list[str] = []
    meanings: dict[str, str] = {}
    for line in row["values"].splitlines():
        value, _, meaning = line.partition(":")
        value, meaning = value.strip(), " ".join(meaning.split())
        if not value:
            if meaning:
                raise ValueError(f"{row['name']}: a meaning was given without a value")
            continue
        if value in vocabulary:
            raise ValueError(f"{row['name']}: allowed value {value!r} is listed twice")
        vocabulary.append(value)
        if meaning:
            meanings[value] = meaning
    return vocabulary, meanings


def built_schema(
    current: FrontmatterSchema,
    keys: list[dict[str, Any]],
    tags: list[dict[str, Any]],
    tag_mode: str,
) -> tuple[FrontmatterSchema | None, list[str]]:
    """The schema a submission describes, or the reasons it cannot be saved."""
    problems: list[str] = []
    roles_by_key: dict[str, list[str]] = {}
    for role, name in current.roles.items():
        roles_by_key.setdefault(name, []).append(role)
    known = {definition.name for definition in current.keys}

    definitions: list[KeyDefinition] = []
    for row in keys:
        name = row["name"]
        if row["new"] and not name:
            continue
        if row["retire"]:
            if name in roles_by_key:
                problems.append(
                    f"{name}: plays the role {', '.join(roles_by_key[name])}, "
                    "so it cannot be retired"
                )
            continue
        if row["new"] and not KEY_NAME_PATTERN.fullmatch(name):
            problems.append(
                f"{name}: a new key name starts with a letter or underscore and holds "
                "only letters, digits, underscores and hyphens"
            )
            continue
        if not row["new"] and name not in known:
            problems.append(f"{name}: is not a key in the current schema; reload the page")
            continue
        if row["kind"] not in KINDS:
            problems.append(f"{name}: kind must be one of {', '.join(KINDS)}")
            continue
        try:
            vocabulary, meanings = allowed_values(row)
            default = parsed_default(row)
        except ValueError as exc:
            problems.append(str(exc))
            continue
        try:
            definitions.append(
                KeyDefinition(
                    name=name,
                    kind=row["kind"],
                    required=row["required"],
                    default=default,
                    vocabulary=vocabulary,
                    vocabulary_meanings=meanings,
                    description=" ".join(row["description"].split()),
                    guidance=row["guidance"].strip(),
                    required_when=row["required_when"] or None,
                    required_when_values=split_list(row["required_when_values"]),
                )
            )
        except ValidationError as exc:
            problems.extend(f"{name}: {error['msg']}" for error in exc.errors())

    listed: dict[str, str] = {}
    aliases: dict[str, str] = {}
    for row in tags:
        name, meaning, row_aliases = row["name"], " ".join(row["meaning"].split()), row["aliases"]
        if not name:
            if row["new"] and (meaning or split_list(row_aliases)):
                problems.append("a tag meaning or aliases were given without a tag name")
            continue
        if not row["listed"]:
            if meaning or split_list(row_aliases):
                problems.append(f"tag {name}: tick Listed to keep its meaning or aliases")
            continue
        if name in listed:
            problems.append(f"tag {name}: listed twice")
            continue
        listed[name] = meaning
        for alias in split_list(row_aliases):
            if alias in aliases:
                problems.append(f"tag alias {alias}: given for both {aliases[alias]} and {name}")
            aliases[alias] = name

    if tag_mode not in TAG_MODES:
        problems.append(f"tag mode must be one of {', '.join(TAG_MODES)}")
    if problems:
        return None, problems
    try:
        schema = FrontmatterSchema(
            schema_version=current.schema_version,
            keys=definitions,
            roles=dict(current.roles),
            tags=listed,
            tag_aliases=aliases,
            tag_mode=tag_mode,  # type: ignore[arg-type]  # checked against TAG_MODES above
        )
    except ValidationError as exc:
        return None, [str(error["msg"]).removeprefix("Value error, ") for error in exc.errors()]
    problems = schema.edit_problems()
    return (None, problems) if problems else (schema, [])


def checkbox(name: str, checked: bool, label: str) -> str:
    state = " checked" if checked else ""
    return f'<label><input type="checkbox" name="{name}" value="on"{state}>{escape(label)}</label>'


def text_input(name: str, label: str, value: str, help_text: str = "") -> str:
    hint = f'<span class="muted">{escape(help_text)}</span>' if help_text else ""
    return (
        f'<label for="{name}">{escape(label)}</label>{hint}'
        f'<input id="{name}" name="{name}" value="{escape(value, quote=True)}">'
    )


def text_area(name: str, label: str, value: str, help_text: str = "", rows: int = 3) -> str:
    hint = f'<span class="muted">{escape(help_text)}</span>' if help_text else ""
    return (
        f'<label for="{name}">{escape(label)}</label>{hint}'
        f'<textarea id="{name}" name="{name}" rows="{rows}">{escape(value)}</textarea>'
    )


def select(name: str, label: str, options: tuple[str, ...], chosen: str) -> str:
    choices = "".join(
        f'<option value="{option}"{" selected" if option == chosen else ""}>{option}</option>'
        for option in options
    )
    return (
        f'<label for="{name}">{escape(label)}</label>'
        f'<select id="{name}" name="{name}">{choices}</select>'
    )


def render_key(index: int, row: dict[str, Any], roles: dict[str, list[str]]) -> str:
    prefix = f"key-{index}"
    name = row["name"]
    if row["new"]:
        heading = "Add a key"
        identity = f'<input type="hidden" name="{prefix}-new" value="1">' + text_input(
            f"{prefix}-name", "Name", name, "Leave blank to add nothing."
        )
    else:
        heading = name
        identity = f'<input type="hidden" name="{prefix}-name" value="{escape(name, quote=True)}">'
        if name in roles:
            identity += f'<p class="muted">Role: {escape(", ".join(roles[name]))}.</p>'
    retire = ""
    if not row["new"] and name not in roles:
        retire = checkbox(
            f"{prefix}-retire",
            row["retire"],
            "Retire this key (notes keep their values; the schema stops checking it)",
        )
    return f"""<fieldset><legend>{escape(heading)}</legend>{identity}
{select(f"{prefix}-kind", "Kind", KINDS, row["kind"])}
{checkbox(f"{prefix}-required", row["required"], "Required on every note")}
{
        text_input(
            f"{prefix}-required_when",
            "Required when key",
            row["required_when"],
            "Another key's name; leave blank when not conditional.",
        )
    }
{
        text_input(
            f"{prefix}-required_when_values",
            "holds one of",
            row["required_when_values"],
            "Comma separated values.",
        )
    }
{
        text_area(
            f"{prefix}-values",
            "Allowed values",
            row["values"],
            "One per line as value: meaning. Leave empty to accept any value of the kind.",
            4,
        )
    }
{
        text_input(
            f"{prefix}-default",
            "Default",
            row["default"],
            "Text for string, enum and date keys; JSON otherwise. Blank for none.",
        )
    }
{text_input(f"{prefix}-description", "Description", row["description"])}
{
        text_area(
            f"{prefix}-guidance",
            "Guidance",
            row["guidance"],
            "A paragraph for whoever fills this key in: a person, an agent or the enricher.",
        )
    }
{retire}</fieldset>"""


def render_tag(
    index: int, row: dict[str, Any], counts: dict[str, int] | None, aliases: dict[str, str]
) -> str:
    prefix = f"tag-{index}"
    name = row["name"]
    if row["new"]:
        identity = f'<input type="hidden" name="{prefix}-new" value="1">' + text_input(
            f"{prefix}-name", "Add a tag", name, "Leave blank to add nothing."
        )
        usage = ""
    else:
        identity = (
            f'<input type="hidden" name="{prefix}-name" value="{escape(name, quote=True)}">'
            f"<h3>{escape(name)}</h3>"
        )
        if counts is None:
            usage = '<p class="muted">Notes: unknown.</p>'
        else:
            under_aliases = sum(count for tag, count in counts.items() if aliases.get(tag) == name)
            extra = f", and {under_aliases} still under an alias" if under_aliases else ""
            usage = f'<p class="tag-count">Notes: {counts.get(name, 0)}{extra}.</p>'
    return f"""<div class="tag">{identity}{usage}
{checkbox(f"{prefix}-listed", row["listed"], "Listed")}
{text_input(f"{prefix}-meaning", "Meaning", row["meaning"])}
{
        text_input(
            f"{prefix}-aliases",
            "Aliases",
            row["aliases"],
            "Comma separated; each is replaced by this tag on every write.",
        )
    }</div>"""


def render(
    *,
    state: StateFile,
    schema: FrontmatterSchema,
    keys: list[dict[str, Any]],
    tags: list[dict[str, Any]],
    tag_mode: str,
    revision: str,
    counts: dict[str, int] | None,
    message: str = "",
    status: int = 200,
) -> HTMLResponse:
    from coppermind_admin import main

    roles: dict[str, list[str]] = {}
    for role, name in schema.roles.items():
        roles.setdefault(name, []).append(role)
    body = f"""<h1>Fields and tags</h1><p><a href="/admin">Overview</a></p>{message}
<p>Revision: {state.revision}</p>
<p class="muted">The frontmatter schema every write is checked against, and the guidance agents
read from <code>GET /v1/schema</code>. Nothing here rewrites a note.</p>
<form method="post" action="/v1/admin/fields">
<input type="hidden" name="revision" value="{escape(revision, quote=True)}">
<input type="hidden" name="key-count" value="{len(keys)}">
<input type="hidden" name="tag-count" value="{len(tags)}">
<h2>Fields</h2>"""
    body += "".join(render_key(index, row, roles) for index, row in enumerate(keys))
    body += f"""<h2>Tags</h2>
{select("tag_mode", "Tag mode", TAG_MODES, tag_mode)}
<p class="muted">Open: any tag is accepted and counted. Closed: a write that adds a tag that is
not listed (or an alias of one) is refused; tags a note already carries are never refused, and
notes made on a device are still adopted.</p>"""
    if counts is None:
        body += f'<p class="error">{escape(COUNTS_UNAVAILABLE)}</p>'
    body += "".join(
        render_tag(index, row, counts, schema.tag_aliases) for index, row in enumerate(tags)
    )
    body += "<button>Save fields and tags</button></form>"
    return HTMLResponse(main.page("Fields and tags", body, wide=True), status_code=status)


def failure(error: Exception, status: int = 500) -> HTMLResponse:
    from coppermind_admin import main

    return HTMLResponse(
        main.page(
            "Fields and tags",
            f'<h1>Fields and tags</h1><p class="error">{escape(main.rejection_detail(error))}</p>'
            '<a href="/admin/fields">Reload fields and tags</a>',
        ),
        status_code=status,
    )


def refusal(problems: list[str]) -> str:
    items = "".join(f"<li>{escape(problem)}</li>" for problem in problems)
    return f'<div class="error"><p>Nothing was saved.</p><ul>{items}</ul></div>'


@router.get("/admin/fields", include_in_schema=False)
async def fields_page(request: Request) -> Response:
    try:
        state, schema = read_schema(request.app.state.control)
    except (OSError, ValueError) as exc:
        return failure(exc)
    counts = await tag_counts(request)
    return render(
        state=state,
        schema=schema,
        keys=key_rows(schema),
        tags=tag_rows(schema, counts),
        tag_mode=schema.tag_mode,
        revision=str(state.revision),
        counts=counts,
    )


@router.post("/v1/admin/fields", include_in_schema=False)
async def save(request: Request) -> Response:
    from coppermind_admin import main

    body = await main.submitted(request)
    store: StateStore = request.app.state.control
    try:
        state, current = read_schema(store)
    except (OSError, ValueError) as exc:
        return failure(exc)
    keys, tags = submitted_rows(body)
    tag_mode = body.get("tag_mode", "")
    try:
        revision: int | None = int(body.get("revision", ""))
    except ValueError:
        revision = None
    schema, problems = built_schema(current, keys, tags, tag_mode)
    if revision is None:
        problems = ["revision: a revision is required; reload the page", *problems]
    if schema is None or revision is None:
        return render(
            state=state,
            schema=current,
            keys=keys or key_rows(current),
            tags=tags or tag_rows(current, None),
            tag_mode=tag_mode if tag_mode in TAG_MODES else current.tag_mode,
            revision=body.get("revision", ""),
            counts=await tag_counts(request),
            message=refusal(problems),
            status=422,
        )
    try:
        saved = store.write("schema", schema.model_dump(mode="json"), if_revision=revision)
    except RevisionConflict as exc:
        return failure(exc, 409)
    except OSError as exc:
        return failure(exc)
    counts = await tag_counts(request)
    return render(
        state=saved,
        schema=schema,
        keys=key_rows(schema),
        tags=tag_rows(schema, counts),
        tag_mode=schema.tag_mode,
        revision=str(saved.revision),
        counts=counts,
        message=f"<p>Saved revision {saved.revision}.</p>",
    )
