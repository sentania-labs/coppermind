"""`/v1/status`: counters computed by the store from its mirror.

Any valid key may read it. Every number comes from the metadata mirror and the
recorded rejections; nothing here or behind it writes into a note.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from coppermind.store_client import HttpStoreClient
from coppermind.store_protocol import StatusResponse, StoreError
from coppermind_api.auth import Principal
from coppermind_api.deps import require_scopes, store
from coppermind_api.errors import failure

router = APIRouter(prefix="/v1", tags=["status"])


@router.get("/status", response_model=StatusResponse)
async def get_status(
    client: HttpStoreClient = Depends(store),
    _: Principal = Depends(require_scopes()),
) -> StatusResponse | JSONResponse:
    try:
        return await client.get_status()
    except StoreError as error:
        return failure(error)
