"""`/v1/notes`.

Create a note and read one back by its identifier. Both are thin: validation,
the call to the store, and the ETag. Nothing here touches a file.

A new note lands in the review folder, which is where anything that has not
been read yet belongs. The identifier in the response is the one written into
the file's frontmatter, so it survives a rename, a move, and a rebuild of the
database from the notes filesystem.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from coppermind.store_client import HttpStoreClient
from coppermind.store_protocol import CreateNote, NoteDocument, StoreError
from coppermind_api.deps import store
from coppermind_api.errors import failure

router = APIRouter(prefix="/v1/notes", tags=["notes"])


class CreateNoteRequest(BaseModel):
    """Create a note in the notes filesystem."""

    title: str = Field(min_length=1, description="The note's H1 and the basis of its filename.")
    body: str = Field(default="", description="Markdown body, without the H1.")
    frontmatter: dict[str, Any] = Field(
        default_factory=dict,
        description="Frontmatter keys to set. Anything omitted takes its schema default.",
    )
    folder: str | None = Field(
        default=None,
        description=(
            "Folder relative to the notes filesystem root. Defaults to the review folder "
            "from settings."
        ),
    )


@router.post("", status_code=201, response_model=NoteDocument)
async def create_note(
    payload: CreateNoteRequest, client: HttpStoreClient = Depends(store)
) -> Response:
    try:
        note = await client.create_note(
            CreateNote(
                title=payload.title,
                body=payload.body,
                frontmatter=payload.frontmatter,
                folder=payload.folder,
                created_by="api",
            )
        )
    except StoreError as error:
        return failure(error)
    return JSONResponse(
        status_code=201,
        content=note.model_dump(mode="json"),
        headers={"ETag": f'"{note.content_hash}"', "Location": f"/v1/notes/{note.id}"},
    )


@router.get("/{note_id}", response_model=NoteDocument)
async def get_note(
    note_id: str,
    accept: str = Header(default="application/json"),
    client: HttpStoreClient = Depends(store),
) -> Response:
    """Return a note as a document, or as the exact file with `Accept: text/markdown`."""
    wants_markdown = "text/markdown" in accept
    try:
        if wants_markdown:
            raw = await client.read_raw(note_id)
            return PlainTextResponse(
                content=raw.text,
                media_type="text/markdown; charset=utf-8",
                headers={"ETag": f'"{raw.content_hash}"'},
            )
        note = await client.get_note(note_id)
    except StoreError as error:
        return failure(error)
    return JSONResponse(
        content=note.model_dump(mode="json"), headers={"ETag": f'"{note.content_hash}"'}
    )
