"""Retention purge requires an explicitly granted, dedicated admin scope."""

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse

from coppermind.purge import AbandonPurge, ConfirmPurge
from coppermind.store_client import HttpStoreClient
from coppermind.store_protocol import StoreError
from coppermind_api.auth import Principal
from coppermind_api.deps import require_scopes, store
from coppermind_api.errors import failure

router = APIRouter(prefix="/v1/admin/purge", tags=["retention"])


@router.post("/preview")
async def preview(
    client: HttpStoreClient = Depends(store),
    _: Principal = Depends(require_scopes("admin:purge")),
) -> Response:
    try:
        return JSONResponse((await client.preview_purge()).model_dump(mode="json"))
    except StoreError as error:
        return failure(error)


@router.post("/confirm")
async def confirm(
    payload: ConfirmPurge,
    client: HttpStoreClient = Depends(store),
    _: Principal = Depends(require_scopes("admin:purge")),
) -> Response:
    try:
        return JSONResponse((await client.confirm_purge(payload)).model_dump(mode="json"))
    except StoreError as error:
        return failure(error)


@router.post("/abandon")
async def abandon(
    payload: AbandonPurge,
    client: HttpStoreClient = Depends(store),
    _: Principal = Depends(require_scopes("admin:purge")),
) -> Response:
    try:
        return JSONResponse((await client.abandon_purge(payload)).model_dump(mode="json"))
    except StoreError as error:
        return failure(error)
