"""`/v1/artifacts`: the second first-class kind, stored byte for byte.

An artifact is a static file or a link: a deck, a PDF, an image, a page
somewhere. It is never a note. The kind is declared on every submission and
refused when absent, the bytes are kept immutable under the artifact's
identity, a browsable copy lands under the artifacts folder of the notes
filesystem, and the extracted text is searchable at once. Nothing here
touches a file: the store does all of it.

`POST` takes either `multipart/form-data` (a `file` part plus text fields) or
JSON (`url`, or `filename` with `content_base64`). The upload is bounded at
200,000,000 bytes here and at `limits.artifact_max_bytes` in the store.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException

from coppermind.errors import envelope
from coppermind.store_client import HttpStoreClient
from coppermind.store_protocol import (
    ArtifactQuery,
    ArtifactRecord,
    ArtifactSummary,
    CreateArtifact,
    CreatedArtifact,
    Page,
    StoreError,
)
from coppermind_api.auth import Principal
from coppermind_api.deps import require_scopes, store
from coppermind_api.errors import failure

router = APIRouter(prefix="/v1/artifacts", tags=["artifacts"])

MAX_UPLOAD_BYTES = 200_000_000
# Text fields a multipart submission may carry beside the file.
TEXT_FIELDS = (
    "kind",
    "title",
    "date",
    "tags",
    "note_id",
    "origin",
    "artifact_id",
    "mime_type",
    "url",
)
# A media type this origin is willing to echo as a Content-Type. Anything else
# is served as octet-stream, so a recorded type can never inject a header.
_MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")


def _too_large() -> JSONResponse:
    return JSONResponse(
        status_code=413,
        content=envelope(
            "payload_too_large",
            f"the artifact upload exceeds {MAX_UPLOAD_BYTES:,} bytes",
            limit_bytes=MAX_UPLOAD_BYTES,
        ),
    )


def _validation(problems: list[str]) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=envelope("validation_error", "; ".join(problems), errors=problems),
    )


def _messages(error: ValidationError) -> list[str]:
    problems = []
    for item in error.errors():
        location = ".".join(str(part) for part in item["loc"])
        message = str(item["msg"]).removeprefix("Value error, ")
        problems.append(
            f"{location}: {message}" if location and not message.startswith(location) else message
        )
    return problems


async def _from_multipart(request: Request) -> tuple[dict[str, Any], int] | JSONResponse:
    """The declared fields and the file of a multipart upload, bounded."""
    try:
        form = await request.form(max_files=2, max_fields=32, max_part_size=MAX_UPLOAD_BYTES)
    except HTTPException as exc:
        if exc.status_code == 413:
            return _too_large()
        return _validation([f"multipart body could not be read: {exc.detail}"])
    payload: dict[str, Any] = {}
    for field in TEXT_FIELDS:
        value = form.get(field)
        if isinstance(value, str) and value != "":
            payload[field] = value
    if "tags" in payload:
        payload["tags"] = [tag.strip() for tag in str(payload["tags"]).split(",") if tag.strip()]
    size = 0
    upload = form.get("file")
    if isinstance(upload, UploadFile):
        data = await upload.read()
        size = len(data)
        if size > MAX_UPLOAD_BYTES:
            return _too_large()
        payload["content_base64"] = base64.b64encode(data).decode()
        payload.setdefault("filename", upload.filename or "")
        if upload.content_type and "mime_type" not in payload:
            payload["mime_type"] = upload.content_type
        await upload.close()
    return payload, size


async def _from_json(request: Request) -> tuple[dict[str, Any], int] | JSONResponse:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_UPLOAD_BYTES + MAX_UPLOAD_BYTES // 2:
            return _too_large()
    try:
        payload = json.loads(bytes(body) or b"{}")
    except ValueError:
        return _validation(["body: must be JSON or multipart/form-data"])
    if not isinstance(payload, dict):
        return _validation(["body: must be a JSON object"])
    return payload, len(body)


@router.post(
    "",
    status_code=201,
    response_model=CreatedArtifact,
    responses={200: {"model": CreatedArtifact, "description": "Artifact replayed"}},
)
async def create_artifact(
    request: Request,
    client: HttpStoreClient = Depends(store),
    _: Principal = Depends(require_scopes("artifacts:write")),
) -> Response:
    """Store a file or a link as an artifact.

    Send `multipart/form-data` with a `file` part and text fields, or a JSON
    object. `kind` must be `artifact`: a submission without a declared kind,
    or declaring anything else, is refused with the reason. `title`, `date`
    (received, YYYY-MM-DD), `tags`, `origin` and `note_id` (a note that cites
    it) are optional; `artifact_id` makes the upload a new revision of that
    artifact. Identical bytes under the same filename, or the same URL,
    answer 200 with `created: false`.
    """
    content_type = request.headers.get("content-type", "")
    parsed = (
        await _from_multipart(request)
        if content_type.startswith("multipart/form-data")
        else await _from_json(request)
    )
    if isinstance(parsed, JSONResponse):
        return parsed
    payload, size = parsed
    try:
        create = CreateArtifact.model_validate(payload)
    except ValidationError as error:
        return _validation(_messages(error))
    try:
        result = await client.create_artifact(create, payload_size_bytes=size)
    except StoreError as error:
        return failure(error)
    return JSONResponse(
        status_code=201 if result.created else 200,
        content=result.model_dump(mode="json"),
        headers={"Location": f"/v1/artifacts/{result.id}"},
    )


@router.get("", response_model=Page[ArtifactSummary])
async def list_artifacts(
    query: Annotated[ArtifactQuery, Query()],
    client: HttpStoreClient = Depends(store),
    _: Principal = Depends(require_scopes("artifacts:read")),
) -> Page[ArtifactSummary] | JSONResponse:
    """List artifacts newest received first, by `tag`, `origin`, citing `note_id`, `from`, `to`."""
    try:
        return await client.list_artifacts(query)
    except StoreError as error:
        return failure(error)


@router.get("/{artifact_id}", response_model=ArtifactRecord)
async def get_artifact(
    artifact_id: str,
    client: HttpStoreClient = Depends(store),
    _: Principal = Depends(require_scopes("artifacts:read")),
) -> ArtifactRecord | JSONResponse:
    """The metadata record: provenance, citing notes, revisions and the full extracted text."""
    try:
        return await client.get_artifact(artifact_id)
    except StoreError as error:
        return failure(error)


@router.get("/{artifact_id}/bytes")
async def get_artifact_bytes(
    artifact_id: str,
    revision: Annotated[int | None, Query(ge=1)] = None,
    client: HttpStoreClient = Depends(store),
    _: Principal = Depends(require_scopes("artifacts:read")),
) -> Response:
    """The archived bytes of the current revision, or of `revision`."""
    try:
        artifact = await client.get_artifact_bytes(artifact_id, revision)
    except StoreError as error:
        return failure(error)
    media = (
        artifact.mime_type if _MEDIA_TYPE.match(artifact.mime_type) else "application/octet-stream"
    )
    ascii_name = (
        "".join(
            char
            for char in artifact.filename.encode("ascii", "ignore").decode()
            if char.isprintable() and char not in '"\\'
        )
        or "artifact"
    )
    disposition = f'attachment; filename="{ascii_name}"'
    if ascii_name != artifact.filename:
        disposition += f"; filename*=UTF-8''{quote(artifact.filename, safe='')}"
    return Response(
        content=artifact.content,
        media_type=media,
        headers={
            "Content-Disposition": disposition,
            "X-Coppermind-SHA256": artifact.sha256,
            "X-Coppermind-Size-Bytes": str(artifact.size_bytes),
            "X-Coppermind-Revision": str(artifact.revision),
            "X-Content-Type-Options": "nosniff",
        },
    )
