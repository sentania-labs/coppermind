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
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from pydantic_core import ErrorDetails

from coppermind.store_protocol import (
    StagedReceipt,
    StagedReservation,
    StoreError,
)
from coppermind_api.auth import Principal
from coppermind_api.deps import require_scopes, store
from coppermind_api.errors import failure

router = APIRouter(prefix="/v1/staged", tags=["staged"])

# Room for JSON framing beside the declared fields.
FRAMING_BYTES = 64 * 1024


def _validation(problems: list[str]) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"error": "validation_error", "message": "; ".join(problems), "errors": problems},
    )


def _oversized(subject: str, limit: int) -> JSONResponse:
    from coppermind_api.v1.artifacts import human_limit

    return JSONResponse(
        status_code=413,
        content={
            "error": "payload_too_large",
            "message": f"This {subject} is larger than the {human_limit(limit)} limit",
            "limit_bytes": limit,
        },
    )


async def _read_body(request: Request, limit: int) -> bytes | None:
    """The request body, or None as soon as it passes `limit` bytes.

    The header is checked first so an honest oversize is refused unread, and the
    stream is counted as it arrives so a body with no header or a false one is
    stopped at the limit instead of being buffered whole.
    """
    announced = request.headers.get("content-length", "")
    if announced.isdigit() and int(announced) > limit:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            return None
    return bytes(body)


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
    body = await _read_body(request, FRAMING_BYTES)
    if body is None:
        return _oversized("declaration", FRAMING_BYTES)
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        return _validation(["body: must be a JSON object"])
    if not isinstance(payload, dict):
        return _validation(["body: must be a JSON object"])
    staged_limit = settings_limit()
    problems = _declaration_problems(payload, staged_limit)
    if problems:
        return _validation(problems)
    try:
        receipt = await client.reserve_staged_upload(StagedReservation.model_validate(payload))
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


def _declaration_problems(payload: dict[str, Any], staged_limit: int) -> list[str]:
    """Why this declaration cannot be reserved, in field order, or an empty list.

    `loc[0]` is the model's own name, so the field is what a caller needs. The
    one question pydantic leaves unanswered is which field was missing, so that
    case is named here rather than repeated by every model.
    """
    try:
        declaration = StagedReservation.model_validate(payload)
    except ValidationError as failure:
        return [_problem(item) for item in failure.errors(include_url=False)]
    if declaration.size_bytes > staged_limit:
        return [
            f"size_bytes: above the staged maximum of {staged_limit} bytes; "
            "split or compress the file"
        ]
    return []


def settings_limit() -> int:
    from coppermind.settings import ProductSettings

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
    data = await _read_body(request, limit)
    if data is None:
        return _oversized("file", limit)
    declared = request.headers.get("content-length", "")
    try:
        receipt = await client.put_staged_bytes(
            receipt_id,
            data,
            declared_bytes=int(declared) if declared.isdigit() else None,
        )
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
