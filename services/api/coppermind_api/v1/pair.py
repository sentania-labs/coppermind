"""Pair the Obsidian companion with a code instead of a pasted key.

The one public route that takes no API key: the companion has none yet. What it
offers is the short code Admin is showing, which works once, for ten minutes,
and is spent by a handful of wrong guesses.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from coppermind.errors import envelope
from coppermind.store_client import HttpStoreClient
from coppermind.store_protocol import StoreError
from coppermind_api.deps import store
from coppermind_api.errors import failure

PAIR_PATH = "/v1/pair"
router = APIRouter(tags=["companion"])


class PairRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=6, max_length=16)
    device: str = Field(default="", max_length=120)


@router.post(PAIR_PATH)
async def pair(payload: PairRequest, client: HttpStoreClient = Depends(store)) -> JSONResponse:
    """Trade the pairing code shown in Admin for this device's own key."""
    try:
        result = await client.admin_action(
            "pair-exchange", {"code": payload.code, "device": payload.device}
        )
    except StoreError as error:
        return failure(error)
    refused = result.get("refused")
    if refused:
        return JSONResponse(
            status_code=403, content=envelope("pairing_refused", refused["message"])
        )
    return JSONResponse({"credential": result["credential"], "name": result["name"]})
