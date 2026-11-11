"""Read and correct people/company dossiers from an agent or companion."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

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


class VoiceConsent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


@router.get("/{entity_id}/voice")
async def get_voice(
    entity_id: str,
    client: HttpStoreClient = Depends(store),
    principal: Principal = Depends(require_scopes("entities:read", "notes:read")),
) -> JSONResponse:
    return await call(client, "voice-get", {"id": entity_id}, principal)


@router.put("/{entity_id}/voice")
async def set_voice(
    entity_id: str,
    payload: VoiceConsent,
    client: HttpStoreClient = Depends(store),
    principal: Principal = Depends(require_scopes("entities:write", "entities:read", "notes:read")),
) -> JSONResponse:
    return await call(client, "voice-set", {"id": entity_id, "enabled": payload.enabled}, principal)


@router.delete("/{entity_id}/voice")
async def delete_voice(
    entity_id: str,
    client: HttpStoreClient = Depends(store),
    principal: Principal = Depends(require_scopes("entities:write", "entities:read", "notes:read")),
) -> JSONResponse:
    return await call(client, "voice-delete", {"id": entity_id}, principal)


class VoiceThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strong: float
    maybe: float


@router.get("/voice/settings")
async def voice_settings(
    client: HttpStoreClient = Depends(store),
    principal: Principal = Depends(require_scopes("entities:read", "notes:read")),
) -> JSONResponse:
    return await call(client, "voice-settings", {}, principal)


@router.put("/voice/settings")
async def save_voice_settings(
    payload: VoiceThresholds,
    client: HttpStoreClient = Depends(store),
    principal: Principal = Depends(require_scopes("entities:write", "entities:read", "notes:read")),
) -> JSONResponse:
    return await call(client, "voice-settings-save", payload.model_dump(), principal)


@router.get("/voice/recognition")
async def voice_recognition(
    client: HttpStoreClient = Depends(store),
    principal: Principal = Depends(require_scopes("entities:read", "notes:read")),
) -> JSONResponse:
    return await call(client, "voice-recognition", {}, principal)


@router.put("/voice/recognition")
async def save_voice_recognition(
    payload: VoiceConsent,
    client: HttpStoreClient = Depends(store),
    principal: Principal = Depends(require_scopes("entities:write", "entities:read", "notes:read")),
) -> JSONResponse:
    return await call(client, "voice-recognition-save", payload.model_dump(), principal)


@router.get("/voice/unknowns")
async def list_unknown_voices(
    client: HttpStoreClient = Depends(store),
    principal: Principal = Depends(require_scopes("entities:read", "notes:read")),
) -> JSONResponse:
    return await call(client, "voice-unknown-list", {}, principal)


@router.delete("/voice/unknowns/{voice_id}")
async def delete_unknown_voice(
    voice_id: str,
    client: HttpStoreClient = Depends(store),
    principal: Principal = Depends(require_scopes("entities:write", "entities:read", "notes:read")),
) -> JSONResponse:
    return await call(client, "voice-unknown-delete", {"id": voice_id}, principal)


class VoiceCombine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str


class VoiceSplit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note_id: str
    label: str


@router.post("/voice/unknowns/{voice_id}/combine")
async def combine_voice(
    voice_id: str,
    payload: VoiceCombine,
    client: HttpStoreClient = Depends(store),
    principal: Principal = Depends(
        require_scopes(
            "entities:write", "entities:read", "notes:read", "notes:write", "sources:write"
        )
    ),
) -> JSONResponse:
    return await call(client, "voice-combine", {"id": voice_id, **payload.model_dump()}, principal)


@router.post("/voice/voices/{voice_id}/split")
async def split_voice(
    voice_id: str,
    payload: VoiceSplit,
    client: HttpStoreClient = Depends(store),
    principal: Principal = Depends(
        require_scopes(
            "entities:write", "entities:read", "notes:read", "notes:write", "sources:write"
        )
    ),
) -> JSONResponse:
    return await call(client, "voice-split", {"id": voice_id, **payload.model_dump()}, principal)
