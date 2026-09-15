"""Create-only source ingest at `/v1/ingest`."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse

from coppermind.store_client import HttpStoreClient
from coppermind.store_protocol import IngestRequest, IngestResult, StoreError
from coppermind_api.auth import Principal
from coppermind_api.deps import require_scopes, store
from coppermind_api.errors import failure

router = APIRouter(prefix="/v1", tags=["sources"])


@router.post("/ingest", status_code=201, response_model=IngestResult)
async def ingest(
    payload: IngestRequest,
    client: HttpStoreClient = Depends(store),
    _: Principal = Depends(require_scopes("sources:write", "notes:write")),
) -> Response:
    try:
        result = await client.ingest(payload)
    except StoreError as error:
        return failure(error)
    return JSONResponse(status_code=201, content=result.model_dump(mode="json"))
