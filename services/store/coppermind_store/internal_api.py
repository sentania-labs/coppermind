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
    FolderTree,
    IngestRequest,
    IngestResult,
    MoveNote,
    NoteDocument,
    NoteQuery,
    NoteSourceInfo,
    NoteSummary,
    Page,
    PatchFrontmatter,
    ProblemInfo,
    RebuildMetadataResult,
    RebuildSearchIndexResult,
    RenameNote,
    ReplaceNote,
    SchemaDocument,
    SearchHit,
    SearchIndexStatus,
    SearchQuery,
    SourceArtifactDocument,
    SourceManifest,
    SourceQuery,
    SourceSummary,
    StatusResponse,
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


@router.get("/tags")
async def list_tags(request: Request) -> Response:
    try:
        tags = await _store(request).list_tags()
    except StoreError as error:
        return _failure(error)
    return JSONResponse(content={"tags": [tag.model_dump(mode="json") for tag in tags]})


@router.get("/schema", response_model=SchemaDocument)
async def get_schema(request: Request) -> SchemaDocument | JSONResponse:
    try:
        return await _store(request).get_schema()
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


@router.get("/sources", response_model=Page[SourceSummary])
async def list_sources(
    request: Request, query: Annotated[SourceQuery, Query()]
) -> Page[SourceSummary] | JSONResponse:
    try:
        return await _store(request).list_sources(query)
    except StoreError as error:
        return _failure(error)


@router.get("/notes/{note_id}/sources", response_model=list[NoteSourceInfo])
async def get_note_sources(note_id: str, request: Request) -> list[NoteSourceInfo] | JSONResponse:
    try:
        return await _store(request).get_note_sources(note_id)
    except StoreError as error:
        return _failure(error)


@router.get("/status", response_model=StatusResponse)
async def get_status(request: Request) -> StatusResponse | JSONResponse:
    try:
        return await _store(request).get_status()
    except StoreError as error:
        return _failure(error)


@router.get("/problems", response_model=list[ProblemInfo])
async def get_problems(request: Request) -> list[ProblemInfo] | JSONResponse:
    try:
        return await _store(request).get_problems()
    except StoreError as error:
        return _failure(error)


@router.post("/notes/{note_id}/move", response_model=NoteDocument)
async def move_note(
    note_id: str,
    payload: MoveNote,
    request: Request,
    if_match: Annotated[str | None, Header()] = None,
) -> Response:
    try:
        if_match_val = etag_from_if_match(if_match) if if_match else None
        note = await _store(request).move_note(note_id, payload, if_match_val)
    except StoreError as error:
        return _failure(error)
    return JSONResponse(
        content=note.model_dump(mode="json"), headers={"ETag": f'"{note.content_hash}"'}
    )


@router.post("/notes/{note_id}/rename", response_model=NoteDocument)
async def rename_note(
    note_id: str,
    payload: RenameNote,
    request: Request,
    if_match: Annotated[str | None, Header()] = None,
) -> Response:
    try:
        note = await _store(request).rename_note(note_id, payload, etag_from_if_match(if_match))
    except StoreError as error:
        return _failure(error)
    return JSONResponse(
        content=note.model_dump(mode="json"), headers={"ETag": f'"{note.content_hash}"'}
    )


@router.get("/folders", response_model=FolderTree)
async def list_folders(request: Request) -> FolderTree:
    return await _store(request).list_folders()


@router.post("/jobs/rebuild_metadata", response_model=RebuildMetadataResult)
async def rebuild_metadata(request: Request) -> RebuildMetadataResult | JSONResponse:
    try:
        return await _store(request).rebuild_metadata()
    except StoreError as error:
        return _failure(error)


@router.get("/search", response_model=Page[SearchHit])
async def search(
    request: Request, query: Annotated[SearchQuery, Query()]
) -> Page[SearchHit] | JSONResponse:
    try:
        return await _store(request).search(query)
    except StoreError as error:
        return _failure(error)


@router.get("/search/index", response_model=SearchIndexStatus)
async def get_search_index_status(request: Request) -> SearchIndexStatus | JSONResponse:
    try:
        return await _store(request).get_search_index_status()
    except StoreError as error:
        return _failure(error)


@router.post("/jobs/rebuild_search_index", status_code=202, response_model=RebuildSearchIndexResult)
async def rebuild_search_index(request: Request) -> Response:
    """Start rebuilding the index from the notes filesystem; it runs in the background."""
    try:
        result = await _store(request).rebuild_search_index()
    except StoreError as error:
        return _failure(error)
    return JSONResponse(status_code=202, content=result.model_dump(mode="json"))
