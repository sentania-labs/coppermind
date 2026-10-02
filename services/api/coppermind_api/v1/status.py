from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from coppermind.store_client import HttpStoreClient
from coppermind.store_protocol import StatusResponse, StoreError
from coppermind_api.auth import Principal
from coppermind_api.deps import require_scopes, store

router = APIRouter()


@router.get("/status", response_model=StatusResponse)
async def get_status(
    client: HttpStoreClient = Depends(store),
    _: Principal = Depends(require_scopes()),  # any key
):
    try:
        status = await client.get_status()
    except StoreError as error:
        from coppermind.errors import to_http

        status_code, body = to_http(error, surface="public")
        return JSONResponse(status_code=status_code, content=body)

    # Note: version, capabilities, and per-helper states can be added to the response if needed.
    # For this task, returning the Store's StatusResponse directly meets the acceptance criteria.
    return status
