"""`/v1/artifacts`: the second first-class kind, stored byte for byte.

An artifact is a static file or a link: a deck, a PDF, an image, a page
somewhere. It is never a note. The kind is declared on every submission and
refused when absent, the bytes are kept immutable under the artifact's
identity, a browsable copy lands under the artifacts folder of the notes
filesystem, and the extracted text is searchable at once. Nothing here
touches a file: the store does all of it.

`POST` takes either `multipart/form-data` (a `file` part plus text fields) or
JSON (`url`, or `filename` with `content_base64`). The upload is bounded at
`limits.artifact_max_bytes`, which the store reports and this API enforces before
it buffers the body. A larger file is refused with one message, whichever shape
it came in, that says the limit and what to do now: files over it cannot be
added yet, so split or compress the file, or add a link to it instead.
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
    PayloadTooLarge,
    StoreError,
)
from coppermind_api.auth import Principal
from coppermind_api.deps import require_scopes, store
from coppermind_api.errors import failure

router = APIRouter(prefix="/v1/artifacts", tags=["artifacts"])

# Room for multipart framing and the text fields beside the file.
FRAMING_BYTES = 64 * 1024
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


def human_limit(limit: int) -> str:
    """A byte limit as a person says it: `10 MiB`, `512 KiB`, `2.5 MiB`."""
    mib, kib = 1024 * 1024, 1024
    if limit >= mib:
        return f"{limit // mib} MiB" if limit % mib == 0 else f"{limit / mib:.1f} MiB"
    if limit >= kib:
        return f"{limit // kib} KiB" if limit % kib == 0 else f"{limit / kib:.1f} KiB"
    return f"{limit} bytes"


def too_large(limit: int) -> JSONResponse:
    """The one refusal for a file over the limit, for multipart and JSON alike."""
    size = human_limit(limit)
    return JSONResponse(
        status_code=413,
        content=envelope(
            "payload_too_large",
            f"This file is larger than the {size} limit. Files over {size} can't be added "
            "yet; split or compress it, or add a link to it instead.",
            limit_bytes=limit,
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


class _BodyTooLarge(Exception):
    pass


def _body_ceiling(limit: int, *, encoded: bool) -> int:
    """The most a request body may carry for a file of `limit` bytes.

    Multipart framing, text fields and base64 add to the file's own size, so
    the transport bound leaves room for them; the exact check is on the
    decoded file bytes.
    """
    return ((limit + 2) // 3 * 4 if encoded else limit) + FRAMING_BYTES


def _declared_too_large(request: Request, ceiling: int) -> bool:
    declared = request.headers.get("content-length", "")
    return declared.isdigit() and int(declared) > ceiling


def _bounded(request: Request, ceiling: int) -> Request:
    """The same request, whose body raises `_BodyTooLarge` the moment it passes `ceiling`."""
    seen = 0

    async def receive() -> Any:
        nonlocal seen
        message = await request.receive()
        if message["type"] == "http.request":
            seen += len(message.get("body", b""))
            if seen > ceiling:
                raise _BodyTooLarge
        return message

    return Request(request.scope, receive)


async def _from_multipart(
    request: Request, limit: int
) -> tuple[dict[str, Any], int | None] | JSONResponse:
    """The declared fields and the file of a multipart upload, bounded as it streams in."""
    ceiling = _body_ceiling(limit, encoded=False)
    if _declared_too_large(request, ceiling):
        return too_large(limit)
    try:
        form = await _bounded(request, ceiling).form(
            max_files=2, max_fields=32, max_part_size=limit
        )
    except _BodyTooLarge:
        return too_large(limit)
    except HTTPException as exc:
        if exc.status_code == 413:
            return too_large(limit)
        return _validation([f"multipart body could not be read: {exc.detail}"])
    try:
        return await _multipart_payload(form, limit)
    finally:
        await form.close()


async def _multipart_payload(
    form: Any, limit: int
) -> tuple[dict[str, Any], int | None] | JSONResponse:
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
        chunks: list[bytes] = []
        while chunk := await upload.read(1 << 20):
            size += len(chunk)
            if size > limit:
                return too_large(limit)
            chunks.append(chunk)
        data = b"".join(chunks)
        payload["content_base64"] = base64.b64encode(data).decode()
        payload.setdefault("filename", upload.filename or "")
        if upload.content_type and "mime_type" not in payload:
            payload["mime_type"] = upload.content_type
    return payload, size


async def _from_json(
    request: Request, limit: int
) -> tuple[dict[str, Any], int | None] | JSONResponse:
    ceiling = _body_ceiling(limit, encoded=True)
    if _declared_too_large(request, ceiling):
        return too_large(limit)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > ceiling:
            return too_large(limit)
    try:
        payload = json.loads(bytes(body) or b"{}")
    except ValueError:
        return _validation(["body: must be JSON or multipart/form-data"])
    if not isinstance(payload, dict):
        return _validation(["body: must be a JSON object"])
    return payload, None


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
    try:
        limit = await client.get_artifact_limit()
    except StoreError as error:
        return failure(error)
    content_type = request.headers.get("content-type", "")
    parsed = (
        await _from_multipart(request, limit)
        if content_type.startswith("multipart/form-data")
        else await _from_json(request, limit)
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
    except PayloadTooLarge as error:
        return too_large(error.limit_bytes)
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


@router.delete("/{artifact_id}", status_code=204)
async def remove_artifact(
    artifact_id: str,
    client: HttpStoreClient = Depends(store),
    _: Principal = Depends(require_scopes("artifacts:write")),
) -> Response:
    """Remove an artifact the way the Admin page does, for an agent acting for the owner.

    The browsable copy, the record, the mirror row and the search entry go at
    once; the archived bytes stay under the artifact identity, marked removed
    through the API, until the retention purge removes them after its window.
    Notes that cite it keep their citation.
    """
    try:
        await client.remove_artifact(artifact_id, "api")
    except StoreError as error:
        return failure(error)
    return Response(status_code=204)


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
