"""`/v1/schema`: the frontmatter rules every write is held to.

Agents, and later the enricher, read the fields, their allowed values and
guidance, the listed tags with their meanings and aliases, and the tag mode
from here, so whoever proposes a value reads the same rules the store
enforces. The API has no state volume: the schema comes from the store, which
reads it from `/data/state/schema.yaml`, the file Admin edits.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from coppermind.schema import FrontmatterSchema, KeyKind, TagMode
from coppermind.store_client import HttpStoreClient
from coppermind.store_protocol import StoreError
from coppermind_api.auth import Principal
from coppermind_api.deps import require_scopes, store
from coppermind_api.errors import failure

router = APIRouter(prefix="/v1/schema", tags=["schema"])


class AllowedValue(BaseModel):
    value: str
    meaning: str


class RequiredWhen(BaseModel):
    """The field is required when `key` holds one of `values`."""

    key: str
    values: list[str]


class FieldRules(BaseModel):
    name: str
    kind: KeyKind
    required: bool
    required_when: RequiredWhen | None
    default: Any
    description: str
    guidance: str
    # Empty when any value of the kind is accepted.
    allowed_values: list[AllowedValue]


class TagRules(BaseModel):
    tag: str
    meaning: str
    aliases: list[str]


class TagSettings(BaseModel):
    # `open` accepts and counts an unlisted tag; `closed` refuses one a write adds.
    mode: TagMode
    listed: list[TagRules]
    # alias -> canonical tag. Every write replaces an alias with its tag.
    aliases: dict[str, str]


class SchemaRules(BaseModel):
    revision: int
    schema_version: int
    fields: list[FieldRules]
    # Which field plays which role, such as `tags_key`.
    roles: dict[str, str]
    tags: TagSettings


def rules(revision: int, schema: FrontmatterSchema) -> SchemaRules:
    return SchemaRules(
        revision=revision,
        schema_version=schema.schema_version,
        fields=[
            FieldRules(
                name=definition.name,
                kind=definition.kind,
                required=definition.required,
                required_when=RequiredWhen(
                    key=definition.required_when, values=definition.required_when_values
                )
                if definition.required_when
                else None,
                default=definition.default,
                description=definition.description,
                guidance=definition.guidance,
                allowed_values=[
                    AllowedValue(value=value, meaning=definition.vocabulary_meanings.get(value, ""))
                    for value in definition.vocabulary
                ],
            )
            for definition in schema.keys
        ],
        roles=dict(schema.roles),
        tags=TagSettings(
            mode=schema.tag_mode,
            listed=[
                TagRules(tag=tag, meaning=meaning, aliases=schema.aliases_of(tag))
                for tag, meaning in schema.tags.items()
            ],
            aliases=dict(schema.tag_aliases),
        ),
    )


@router.get("", response_model=SchemaRules)
async def get_schema(
    client: HttpStoreClient = Depends(store),
    _: Principal = Depends(require_scopes("notes:read")),
) -> Response:
    """The fields, allowed values, guidance, tags and aliases writes are held to."""
    try:
        document = await client.get_schema()
    except StoreError as error:
        return failure(error)
    return JSONResponse(
        content=rules(document.revision, document.frontmatter_schema).model_dump(mode="json")
    )
