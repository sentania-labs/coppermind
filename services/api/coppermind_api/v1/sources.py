"""Read-only public source and artifact endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, Response

from coppermind.store_client import HttpStoreClient
from coppermind.store_protocol import SourceManifest, StoreError
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
    }
    if artifact.content is not None:
        return Response(content=artifact.content, media_type=artifact.mime_type, headers=headers)
    return JSONResponse(content=artifact.model_dump(mode="json"), headers=headers)


@router.get("/{source_id}/projection")
async def get_source_projection(
    source_id: str,
    client: HttpStoreClient = Depends(store),
    _: Principal = Depends(require_scopes("sources:read")),
) -> Response:
    try:
        projection = await client.get_source_projection(source_id)
    except StoreError as error:
        return failure(error)
    return Response(
        content=projection.content,
        media_type="text/markdown",
        headers={"X-Coppermind-Projection-Path": projection.path},
    )


@router.api_route(
    "/{source_path:path}", methods=["PUT", "PATCH", "DELETE"], include_in_schema=False
)
async def refuse_source_mutation(
    source_path: str,
    client: HttpStoreClient = Depends(store),
    _: Principal = Depends(authenticated_key),
) -> JSONResponse:
    source_id = source_path.split("/", 1)[0]
    try:
        await client.refuse_source_mutation(source_id)
    except StoreError as error:
        return failure(error)
    raise AssertionError("the immutable source guard returned")
