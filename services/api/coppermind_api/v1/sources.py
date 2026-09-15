"""Read-only public source and artifact endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, Response

from coppermind.store_client import HttpStoreClient
from coppermind.store_protocol import SourceImmutable, SourceManifest, StoreError
from coppermind_api.auth import Principal
from coppermind_api.deps import authenticated_key, require_scopes, store
from coppermind_api.errors import failure

router = APIRouter(prefix="/v1/sources", tags=["sources"])


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
    headers = {
        "X-Coppermind-SHA256": artifact.sha256,
        "X-Coppermind-Size-Bytes": str(artifact.size_bytes),
        "X-Content-Type-Options": "nosniff",
    }
    if artifact.content is not None:
        # The ingested type is free client text. The manifest records it; this
        # origin decides for itself how the bytes it serves are interpreted.
        return Response(
            content=artifact.content, media_type="text/plain; charset=utf-8", headers=headers
        )
    return JSONResponse(content=artifact.model_dump(mode="json"), headers=headers)


@router.api_route(
    "/{source_path:path}", methods=["PUT", "PATCH", "DELETE"], include_in_schema=False
)
async def refuse_source_mutation(
    source_path: str,
    _: Principal = Depends(authenticated_key),
) -> JSONResponse:
    # A source is immutable, so there is nothing to ask the Store about. The
    # refusal is the answer whether or not the Store is reachable.
    return failure(SourceImmutable(source_path.split("/", 1)[0]))
