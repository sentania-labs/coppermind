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


async def call(client: HttpStoreClient, action: str, payload: dict[str, Any]) -> JSONResponse:
    try:
        return JSONResponse(await client.entity_action(action, payload))
    except StoreError as exc:
        return failure(exc)


@router.get("")
async def list_entities(
    query: Annotated[EntityQuery, Query()],
    client: HttpStoreClient = Depends(store),
    _: Principal = Depends(require_scopes("entities:read", "notes:read")),
) -> JSONResponse:
    return await call(client, "list", query.model_dump())


@router.get("/{entity_id}")
async def get_entity(
    entity_id: str,
    client: HttpStoreClient = Depends(store),
    _: Principal = Depends(require_scopes("entities:read", "notes:read")),
) -> JSONResponse:
    return await call(client, "get", {"id": entity_id})


@router.post("")
async def create_entity(
    payload: EntityInput,
    client: HttpStoreClient = Depends(store),
    _: Principal = Depends(require_scopes("entities:write", "entities:read", "notes:read")),
) -> JSONResponse:
    return await call(client, "create", payload.model_dump())


@router.post("/{entity_id}")
async def change_entity(
    entity_id: str,
    payload: EntityChange,
    client: HttpStoreClient = Depends(store),
    _: Principal = Depends(require_scopes("entities:write", "entities:read", "notes:read")),
) -> JSONResponse:
    return await call(client, "change", {"id": entity_id, "change": payload.model_dump()})
