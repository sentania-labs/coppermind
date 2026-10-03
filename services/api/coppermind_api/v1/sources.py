"""Read-only public source and artifact endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, Response

from coppermind.store_client import HttpStoreClient
from coppermind.store_protocol import (
    Page,
    SourceManifest,
    SourceQuery,
    SourceSummary,
    StoreError,
)
from coppermind_api.auth import Principal
from coppermind_api.deps import require_scopes, store
from coppermind_api.errors import failure

router = APIRouter(prefix="/v1/sources", tags=["sources"])


@router.get("", response_model=Page[SourceSummary])
async def list_sources(
    query: Annotated[SourceQuery, Query()],
    client: HttpStoreClient = Depends(store),
    _: Principal = Depends(require_scopes("sources:read")),
) -> Page[SourceSummary] | JSONResponse:
    """List mirrored sources, newest first, filtered by `provider`, `from` and `to`."""
    try:
        return await client.list_sources(query)
    except StoreError as error:
        return failure(error)


@router.get("/{source_id}", response_model=SourceManifest)
async def get_source(
    source_id: str,
    client: HttpStoreClient = Depends(store),
    _: Principal = Depends(require_scopes("sources:read")),
) -> SourceManifest | JSONResponse:
    try:
        return await client.get_source(source_id)
    except StoreError as error:
        return failure(error)


@router.get("/{source_id}/revisions/{revision}/artifacts/{name}")
async def get_source_artifact(
    source_id: str,
    revision: int,
    name: str,
    client: HttpStoreClient = Depends(store),
    _: Principal = Depends(require_scopes("sources:read")),
) -> Response:
    try:
        artifact = await client.get_source_artifact(source_id, revision, name)
    except StoreError as error:
        return failure(error)
    if artifact.content is not None:
        # The ingested type is free client text. The manifest records it; this
        # origin decides for itself how the bytes it serves are interpreted.
        return Response(
            content=artifact.content,
            media_type="text/plain; charset=utf-8",
            headers={
                "X-Coppermind-SHA256": artifact.sha256,
                "X-Coppermind-Size-Bytes": str(artifact.size_bytes),
                "X-Content-Type-Options": "nosniff",
            },
        )
    return JSONResponse(
        content=artifact.model_dump(mode="json"),
        headers={"X-Content-Type-Options": "nosniff"},
    )
