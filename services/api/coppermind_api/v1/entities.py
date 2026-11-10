"""Read and correct people/company dossiers from an agent or companion."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from coppermind.entities import EntityChange, EntityInput, EntityQuery
from coppermind.store_client import HttpStoreClient
from coppermind.store_protocol import StoreError
from coppermind_api.auth import Principal
from coppermind_api.deps import require_scopes, store
from coppermind_api.errors import failure

router = APIRouter(prefix="/v1/entities", tags=["entities"])


def withhold_source_quotes(value: Any) -> Any:
    if isinstance(value, list):
        return [withhold_source_quotes(item) for item in value]
    if not isinstance(value, dict):
        return value
    if value.get("source_versions") and value.get("quote"):
        value = {**value, "quote": "", "quote_withheld": True}
    return {key: withhold_source_quotes(item) for key, item in value.items()}


async def call(
    client: HttpStoreClient, action: str, payload: dict[str, Any], principal: Principal
) -> JSONResponse:
    try:
        result = await client.entity_action(action, payload)
        if not principal.has("sources:read"):
            result = withhold_source_quotes(result)
        return JSONResponse(result)
    except StoreError as exc:
        return failure(exc)


@router.get("")
async def list_entities(
    query: Annotated[EntityQuery, Query()],
    client: HttpStoreClient = Depends(store),
    principal: Principal = Depends(require_scopes("entities:read", "notes:read")),
) -> JSONResponse:
    return await call(client, "list", query.model_dump(), principal)


@router.get("/{entity_id}")
async def get_entity(
    entity_id: str,
    client: HttpStoreClient = Depends(store),
    principal: Principal = Depends(require_scopes("entities:read", "notes:read")),
) -> JSONResponse:
    return await call(client, "get", {"id": entity_id}, principal)


@router.post("")
async def create_entity(
    payload: EntityInput,
    client: HttpStoreClient = Depends(store),
    principal: Principal = Depends(require_scopes("entities:write", "entities:read", "notes:read")),
) -> JSONResponse:
    return await call(client, "create", payload.model_dump(), principal)


@router.post("/{entity_id}")
async def change_entity(
    entity_id: str,
    payload: EntityChange,
    client: HttpStoreClient = Depends(store),
    principal: Principal = Depends(require_scopes("entities:write", "entities:read", "notes:read")),
) -> JSONResponse:
    return await call(
        client, "change", {"id": entity_id, "change": payload.model_dump()}, principal
    )
