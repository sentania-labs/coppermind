"""Idempotent source ingest at `/v1/ingest`."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from coppermind.store_client import HttpStoreClient
from coppermind.store_protocol import IngestRequest, IngestResult, StoreError
from coppermind_api.auth import Principal
from coppermind_api.deps import require_scopes, store
from coppermind_api.errors import failure

router = APIRouter(prefix="/v1", tags=["sources"])


@router.post(
    "/ingest",
    status_code=201,
    response_model=IngestResult,
    responses={200: {"model": IngestResult, "description": "Source replayed or revised"}},
)
async def ingest(
    payload: IngestRequest,
    request: Request,
    client: HttpStoreClient = Depends(store),
    _: Principal = Depends(require_scopes("sources:write", "notes:write")),
) -> Response:
    try:
        result = await client.ingest(payload, payload_size_bytes=len(await request.body()))
    except StoreError as error:
        return failure(error)
    status_code = 201 if result.note.created else 200
    return JSONResponse(status_code=status_code, content=result.model_dump(mode="json"))
