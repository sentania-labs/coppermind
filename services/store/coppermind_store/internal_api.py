"""The internal HTTP contract, `/internal/v1`.

This surface is never exposed publicly. It is the store contract of
`coppermind.store_protocol` mapped onto HTTP, so that `LocalStore` inside this
process and `HttpStoreClient` in the API are interchangeable.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import JSONResponse

from coppermind.api_keys import ApiKeySet
from coppermind.errors import to_http
from coppermind.store_protocol import (
    CreateNote,
    IngestRequest,
    IngestResult,
    NoteDocument,
    NoteQuery,
    NoteSummary,
    Page,
    PatchFrontmatter,
    ReplaceNote,
    SourceArtifactDocument,
    SourceManifest,
    SourceProjection,
    StoreError,
    etag_from_if_match,
)
from coppermind_store.auth import require_internal_token
from coppermind_store.notes import LocalStore

router = APIRouter(prefix="/internal/v1", dependencies=[Depends(require_internal_token)])


def _store(request: Request) -> LocalStore:
    return request.app.state.store


def _failure(error: StoreError) -> JSONResponse:
    status_code, body = to_http(error, surface="internal")
    return JSONResponse(status_code=status_code, content=body)


@router.get("/api-keys", response_model=ApiKeySet)
async def get_api_keys(request: Request) -> ApiKeySet:
    return await _store(request).get_api_keys()


@router.post("/notes", status_code=201, response_model=NoteDocument)
async def create_note(payload: CreateNote, request: Request) -> Response:
    try:
        note = await _store(request).create_note(payload)
    except StoreError as error:
        return _failure(error)
    return JSONResponse(
        status_code=201,
        content=note.model_dump(mode="json"),
        headers={"ETag": f'"{note.content_hash}"'},
    )


@router.get("/notes", response_model=Page[NoteSummary])
async def list_notes(
    request: Request, query: Annotated[NoteQuery, Query()]
) -> Page[NoteSummary] | JSONResponse:
    try:
        return await _store(request).list_notes(query)
    except StoreError as error:
        return _failure(error)


@router.post(
    "/ingest",
    status_code=201,
    response_model=IngestResult,
    responses={200: {"model": IngestResult, "description": "Source replayed or revised"}},
)
async def ingest(
    payload: IngestRequest,
    request: Request,
    payload_size_bytes: Annotated[int | None, Header(alias="X-Coppermind-Payload-Bytes")] = None,
) -> Response:
    try:
        internal_size = len(await request.body())
        result = await _store(request).ingest(
            payload,
            payload_size_bytes=max(internal_size, payload_size_bytes or 0),
        )
    except StoreError as error:
        return _failure(error)
    status_code = 201 if result.note.created else 200
    return JSONResponse(status_code=status_code, content=result.model_dump(mode="json"))


@router.get("/sources/{source_id}", response_model=SourceManifest)
async def get_source(source_id: str, request: Request) -> SourceManifest | JSONResponse:
    try:
        return await _store(request).get_source(source_id)
    except StoreError as error:
        return _failure(error)


@router.get(
    "/sources/{source_id}/revisions/{revision}/artifacts/{name}",
    response_model=SourceArtifactDocument,
)
async def get_source_artifact(
    source_id: str, revision: int, name: str, request: Request
) -> SourceArtifactDocument | JSONResponse:
    try:
        return await _store(request).get_source_artifact(source_id, revision, name)
    except StoreError as error:
        return _failure(error)


@router.get("/sources/{source_id}/projection", response_model=SourceProjection)
async def get_source_projection(
    source_id: str, request: Request
) -> SourceProjection | JSONResponse:
    try:
        return await _store(request).get_source_projection(source_id)
    except StoreError as error:
        return _failure(error)


@router.get("/notes/{note_id}", response_model=NoteDocument)
async def get_note(note_id: str, request: Request) -> Response:
    try:
        note = await _store(request).get_note(note_id)
    except StoreError as error:
        return _failure(error)
    return JSONResponse(
        content=note.model_dump(mode="json"), headers={"ETag": f'"{note.content_hash}"'}
    )


@router.put("/notes/{note_id}", response_model=NoteDocument)
async def replace_note(
    note_id: str,
    payload: ReplaceNote,
    request: Request,
    if_match: Annotated[str | None, Header()] = None,
) -> Response:
    try:
        note = await _store(request).replace_note(note_id, payload, etag_from_if_match(if_match))
    except StoreError as error:
        return _failure(error)
    return JSONResponse(
        content=note.model_dump(mode="json"), headers={"ETag": f'"{note.content_hash}"'}
    )


@router.patch("/notes/{note_id}/frontmatter", response_model=NoteDocument)
async def patch_frontmatter(
    note_id: str,
    payload: PatchFrontmatter,
    request: Request,
    if_match: Annotated[str | None, Header()] = None,
) -> Response:
    try:
        note = await _store(request).patch_frontmatter(
            note_id, payload, etag_from_if_match(if_match)
        )
    except StoreError as error:
        return _failure(error)
    return JSONResponse(
        content=note.model_dump(mode="json"), headers={"ETag": f'"{note.content_hash}"'}
    )
