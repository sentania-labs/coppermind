"""Folder tree endpoint for the API.

GET /v1/folders -- return the folder tree with note counts.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, status
from fastapi.responses import JSONResponse

from coppermind.store_client import HttpStoreClient
from coppermind.store_protocol import FolderTree, StoreError
from coppermind_api.auth import Principal
from coppermind_api.deps import require_scopes, store
from coppermind_api.errors import failure

router = APIRouter(tags=["folders"])


@router.get(
    "/folders",
    responses={
        200: {"model": FolderTree, "description": "Folder tree with note counts"},
        503: {"description": "Store unavailable"},
    },
)
async def get_folders(
    if_match: str | None = Header(default=None),
    client: HttpStoreClient = Depends(store),
    _: Annotated[Principal, Depends(require_scopes("notes:read"))] = None,
) -> JSONResponse:
    """Return the folder tree with note counts.

    Requires ``notes:read`` scope on the API key.
    """
    try:
        result = await client.list_folders()
    except StoreError as error:
        return failure(error)

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=result.model_dump(),
    )
