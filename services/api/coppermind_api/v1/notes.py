"""`/v1/notes`.

Create a note, read one back by its identifier, and replace one on the
condition that it has not changed since it was read. All three are thin:
validation, the call to the store, and the ETag. Nothing here touches a file.

A new note lands in the review folder, which is where anything that has not
been read yet belongs. The identifier in the response is the one written into
the file's frontmatter, so it survives a rename, a move, and a rebuild of the
database from the notes filesystem.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Response
from fastapi.responses import JSONResponse

from coppermind.store_client import HttpStoreClient
from coppermind.store_protocol import (
    CreateNote,
    NoteDocument,
    ReplaceNote,
    StoreError,
    etag_from_if_match,
)
from coppermind_api.deps import store
from coppermind_api.errors import failure

router = APIRouter(prefix="/v1/notes", tags=["notes"])


@router.post("", status_code=201, response_model=NoteDocument)
async def create_note(payload: CreateNote, client: HttpStoreClient = Depends(store)) -> Response:
    try:
        note = await client.create_note(payload)
    except StoreError as error:
        return failure(error)
    return JSONResponse(
        status_code=201,
        content=note.model_dump(mode="json"),
        headers={"ETag": f'"{note.content_hash}"', "Location": f"/v1/notes/{note.id}"},
    )


@router.get("/{note_id}", response_model=NoteDocument)
async def get_note(note_id: str, client: HttpStoreClient = Depends(store)) -> Response:
    """Return a note as a document, with the ETag of the file's bytes."""
    try:
        note = await client.get_note(note_id)
    except StoreError as error:
        return failure(error)
    return JSONResponse(
        content=note.model_dump(mode="json"), headers={"ETag": f'"{note.content_hash}"'}
    )


@router.put("/{note_id}", response_model=NoteDocument)
async def replace_note(
    note_id: str,
    payload: ReplaceNote,
    if_match: Annotated[str | None, Header()] = None,
    client: HttpStoreClient = Depends(store),
) -> Response:
    """Replace a note's frontmatter and body, keeping its identifier and path.

    `If-Match` must carry the ETag a read returned. Without it the answer is
    428; with an ETag the file no longer hashes to, 409 `version_conflict`
    naming the current one, and the file is untouched. The body is the
    document shape a read returns, so read, edit and send it back.
    """
    try:
        note = await client.replace_note(note_id, payload, etag_from_if_match(if_match))
    except StoreError as error:
        return failure(error)
    return JSONResponse(
        content=note.model_dump(mode="json"), headers={"ETag": f'"{note.content_hash}"'}
    )
