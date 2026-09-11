"""The internal HTTP contract, `/internal/v1`.

This surface is never exposed publicly. It is the store contract of
`coppermind.store_protocol` mapped onto HTTP, so that `LocalStore` inside this
process and `HttpStoreClient` in the API are interchangeable.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import JSONResponse

from coppermind.errors import to_http
from coppermind.store_protocol import (
    CreateNote,
    NoteDocument,
    ReplaceNote,
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
