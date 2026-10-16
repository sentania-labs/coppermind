"""Scoped workflow operations for the Obsidian companion and binary intake."""

from __future__ import annotations

import base64
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from coppermind.store_client import HttpStoreClient
from coppermind.store_protocol import StoreError
from coppermind_api.auth import Principal
from coppermind_api.deps import require_scopes, store
from coppermind_api.errors import failure

router = APIRouter(prefix="/v1", tags=["workflow"])


class ReviewCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    if_match: str = Field(min_length=1, max_length=100)
    confirmation: str
    job_id: str | None = None
    selected: list[int] = Field(default_factory=list, max_length=30)
    accept_link_risk: bool = False


async def call(client: HttpStoreClient, action: str, payload: dict[str, Any]) -> Response:
    try:
        return JSONResponse(await client.context_action(action, payload))
    except StoreError as exc:
        return failure(exc)


@router.get("/notes/{note_id}/review")
async def review(
    note_id: str,
    client: HttpStoreClient = Depends(store),
    _: Principal = Depends(require_scopes("notes:read", "sources:read")),
) -> Response:
    return await call(client, "lifecycle", {"note_id": note_id})


@router.post("/notes/{note_id}/review")
async def finish(
    note_id: str,
    payload: ReviewCompletion,
    client: HttpStoreClient = Depends(store),
    _: Principal = Depends(require_scopes("notes:write", "notes:read", "sources:read")),
) -> Response:
    return await call(client, "complete-review", {"note_id": note_id, **payload.model_dump()})


@router.post("/notes/{note_id}/suggestions")
async def refresh(
    note_id: str,
    client: HttpStoreClient = Depends(store),
    _: Principal = Depends(require_scopes("notes:write", "notes:read", "sources:read")),
) -> Response:
    return await call(
        client,
        "enqueue",
        {
            "note_ids": [note_id],
            "regenerate": True,
            "confirmation": "Send selected notes for enrichment",
        },
    )


@router.post("/dropbox", status_code=202)
async def drop(
    request: Request,
    filename: str = Query(min_length=1, max_length=255),
    client: HttpStoreClient = Depends(store),
    _: Principal = Depends(require_scopes("sources:write")),
) -> Response:
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 10 * 1024 * 1024:
            return JSONResponse(
                {"error": "payload_too_large", "message": "Dropbox limit: 10 MiB"}, 413
            )
    result = await call(
        client,
        "upload-drop",
        {"filename": filename, "content_base64": base64.b64encode(data).decode()},
    )
    if result.status_code == 200:
        result.status_code = 202
    return result


class CalendarResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    if_match: str
    calendar_hash: str
    event_id: str = ""


@router.post("/notes/{note_id}/calendar-update")
async def calendar_update(
    note_id: str,
    payload: CalendarResolution,
    client: HttpStoreClient = Depends(store),
    _: Principal = Depends(require_scopes("notes:write", "notes:read", "sources:read")),
) -> Response:
    return await call(client, "resolve-calendar", {"note_id": note_id, **payload.model_dump()})
