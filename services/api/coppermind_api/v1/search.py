"""`/v1/search`: full-text search over note bodies and titles.

Needs `notes:read`. The store answers from its PostgreSQL full-text index,
which it keeps current from the metadata mirror and can rebuild from the notes
filesystem at any time; nothing here reads or writes a file.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from coppermind.store_client import HttpStoreClient
from coppermind.store_protocol import Page, SearchHit, SearchQuery, StoreError
from coppermind_api.auth import Principal
from coppermind_api.deps import require_scopes, store
from coppermind_api.errors import failure

router = APIRouter(prefix="/v1", tags=["search"])


@router.get("/search", response_model=Page[SearchHit])
async def search(
    query: Annotated[SearchQuery, Query()],
    client: HttpStoreClient = Depends(store),
    _: Principal = Depends(require_scopes("notes:read")),
) -> Page[SearchHit] | JSONResponse:
    """Notes and source projections matching `q`, best match first.

    Takes every filter `GET /v1/notes` takes and pages the same way, by an
    opaque `cursor` and a `limit` of 1 through 200 (50 by default). A hit
    under the sources folder that the store generated says `kind: "source"`
    and carries the source identifier as its `id`.
    """
    try:
        return await client.search(query)
    except StoreError as error:
        return failure(error)
