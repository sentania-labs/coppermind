"""The staged upload path: one file larger than the inline limit, in three requests.

`POST /v1/artifacts` holds its whole body in memory, so it stops at
`limits.artifact_max_bytes`. A bigger file uses this path instead, so that no
single request has to be buffered whole:

1. `POST /v1/staged` reserves. The declaration names the filename, media type,
   exact byte count and lowercase sha256 of the file, plus the same fields the
   inline route accepts. The receipt and the accepted limits come back.
2. `PUT /v1/staged/{receipt_id}` carries the file's bytes as the whole body,
   streamed to durable staging outside the browsable notes filesystem.
3. `POST /v1/staged/{receipt_id}/finalize` verifies the count and the digest,
   then stores the artifact through the same code path an inline upload takes, so
   the archive, immutability, revisions, citations and replay behaviour match.

`GET /v1/staged/{receipt_id}` reports a receipt's state for a caller deciding
whether to retry. Every write route needs `artifacts:write`, and the read needs
`artifacts:read`. An identical retry succeeds; bytes that disagree with the
declaration fail on that receipt rather than becoming a second revision.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from pydantic_core import ErrorDetails

from coppermind.settings import ProductSettings
from coppermind.store_protocol import (
    StagedReceipt,
    StagedReservation,
    StoreError,
)
from coppermind_api.auth import Principal
from coppermind_api.deps import require_scopes, store
from coppermind_api.errors import failure
from coppermind_api.v1.artifacts import human_limit

router = APIRouter(prefix="/v1/staged", tags=["staged"])

# Room for JSON framing beside the declared fields.
FRAMING_BYTES = 64 * 1024


def _validation(problems: list[str]) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"error": "validation_error", "message": "; ".join(problems), "errors": problems},
    )


class _TooLarge(Exception):
    """The request body passed its limit while it was still arriving."""


def _oversized(subject: str, limit: int) -> JSONResponse:
    return JSONResponse(
        status_code=413,
        content={
            "error": "payload_too_large",
            "message": f"This {subject} is larger than the {human_limit(limit)} limit",
            "limit_bytes": limit,
        },
    )


async def _limited(request: Request, limit: int) -> AsyncIterator[bytes]:
    """The request body a chunk at a time, stopping with `_TooLarge` once it passes `limit`.

    The header is checked first so an honest oversize is refused unread, and the
    stream is counted as it arrives so a body with no header or a false one is
    stopped at the limit instead of being carried on.
    """
    announced = request.headers.get("content-length", "")
    if announced.isdigit() and int(announced) > limit:
        raise _TooLarge
    seen = 0
    async for chunk in request.stream():
        seen += len(chunk)
        if seen > limit:
            raise _TooLarge
        yield chunk


async def _read_body(request: Request, limit: int) -> bytes:
    return b"".join([chunk async for chunk in _limited(request, limit)])


@router.post("", status_code=201, response_model=StagedReceipt)
async def reserve_staged_upload(
    request: Request,
    client: Annotated[Any, Depends(store)],
    _: Principal = Depends(require_scopes("artifacts:write")),
) -> JSONResponse:
    """Declare one file, and get the receipt its bytes stream into.

    The declaration carries `filename`, `size_bytes` and `sha256` beside the
    fields an inline submission accepts. An identical reservation replays the
    same receipt; a declared size above the staged maximum is refused at once.
    """
    try:
        body = await _read_body(request, FRAMING_BYTES)
    except _TooLarge:
        return _oversized("declaration", FRAMING_BYTES)
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        return _validation(["body: must be a JSON object"])
    if not isinstance(payload, dict):
        return _validation(["body: must be a JSON object"])
    try:
        declaration = StagedReservation.model_validate(payload)
    except ValidationError as invalid:
        return _validation([_problem(item) for item in invalid.errors(include_url=False)])
    try:
        receipt = await client.reserve_staged_upload(declaration)
    except StoreError as error:
        return failure(error)
    return JSONResponse(status_code=201, content=receipt.model_dump(mode="json"))


def _problem(item: ErrorDetails) -> str:
    """One pydantic error as a caller can act on it.

    `loc` names the field directly (`('size_bytes',)`), since the model is what
    is being validated, so every entry has exactly one part.
    """
    location = ".".join(str(part) for part in item["loc"] if part != "")
    message = str(item["msg"]).removeprefix("Value error, ").strip()
    if item["type"] == "missing":
        # Pydantic says only "Field required" for this, so the field is the news.
        return f"{location}: field required"
    if not location or message.startswith(location):
        return message or location
    return f"{location}: {message}"


def settings_limit() -> int:
    return ProductSettings().limits.staged_max_bytes


@router.put("/{receipt_id}", status_code=200, response_model=StagedReceipt)
async def put_staged_bytes(
    receipt_id: str,
    request: Request,
    client: Annotated[Any, Depends(store)],
    _: Principal = Depends(require_scopes("artifacts:write")),
) -> JSONResponse:
    """Stream the file's bytes for one receipt into durable staging."""
    limit = settings_limit()
    declared = request.headers.get("content-length", "")
    try:
        receipt = await client.put_staged_bytes(
            receipt_id,
            _limited(request, limit),
            declared_bytes=int(declared) if declared.isdigit() else None,
        )
    except _TooLarge:
        return _oversized("file", limit)
    except StoreError as error:
        return failure(error)
    return JSONResponse(content=receipt.model_dump(mode="json"))


@router.post("/{receipt_id}/finalize", status_code=200, response_model=StagedReceipt)
async def finalize_staged_upload(
    receipt_id: str,
    client: Annotated[Any, Depends(store)],
    _: Principal = Depends(require_scopes("artifacts:write")),
) -> JSONResponse:
    """Verify the staged bytes and store the artifact."""
    try:
        receipt = await client.finalize_staged_upload(receipt_id)
    except StoreError as error:
        return failure(error)
    return JSONResponse(content=receipt.model_dump(mode="json"))


@router.get("/{receipt_id}", status_code=200, response_model=StagedReceipt)
async def get_staged_upload(
    receipt_id: str,
    client: Annotated[Any, Depends(store)],
    _: Principal = Depends(require_scopes("artifacts:read")),
) -> JSONResponse:
    """One receipt's state, its missing pieces and the artifact it became."""
    try:
        receipt = await client.get_staged_upload(receipt_id)
    except StoreError as error:
        return failure(error)
    return JSONResponse(content=receipt.model_dump(mode="json"))
