"""Bounded binary calendar uploads use the same queue as vault drops."""

from __future__ import annotations

import base64

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse

from coppermind.store_client import HttpStoreClient
from coppermind.store_protocol import StoreError
from coppermind_api.auth import Principal
from coppermind_api.deps import require_scopes, store
from coppermind_api.errors import failure

router = APIRouter(prefix="/v1/calendars", tags=["calendars"])


@router.get("")
async def calendars(
    offset: int = Query(default=0, ge=0),
    client: HttpStoreClient = Depends(store),
    _: Principal = Depends(require_scopes("sources:read")),
) -> Response:
    try:
        result = await client.context_action("snapshot", {"offset": offset})
    except StoreError as exc:
        return failure(exc)
    return JSONResponse(
        {
            key: result[key]
            for key in ("calendar", "events", "contacts", "offset", "next_offset", "totals")
        }
    )


@router.post("/{calendar_id}/imports", status_code=202)
async def upload(
    calendar_id: str,
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
                {"error": "payload_too_large", "message": "Calendar limit: 10 MiB"}, status_code=413
            )
    try:
        result = await client.context_action(
            "upload",
            {
                "calendar_id": calendar_id,
                "filename": filename,
                "content_base64": base64.b64encode(data).decode(),
            },
        )
    except StoreError as exc:
        return failure(exc)
    return JSONResponse(result, status_code=202)
