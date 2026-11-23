"""Recording intake: hand in a stored recording and get a transcribed note in Review.

The recording is uploaded first, through `POST /v1/staged` for anything over the inline
limit. `POST /v1/recordings` then names that artifact. The request returns at once: the
transcription runs in the background on the store, as long as the service needs, and the
status route reports where it stands until the note exists. The route needs a
transcription service set up in Admin; without one it refuses with a plain message.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from coppermind.store_client import HttpStoreClient
from coppermind.store_protocol import RecordingPage, RecordingRequest, RecordingStatus, StoreError
from coppermind_api.auth import Principal
from coppermind_api.deps import require_scopes, store
from coppermind_api.errors import failure

router = APIRouter(prefix="/v1/recordings", tags=["recordings"])


@router.post(
    "",
    status_code=202,
    response_model=RecordingStatus,
    responses={200: {"model": RecordingStatus, "description": "An intake already settled"}},
)
async def accept_recording(
    payload: RecordingRequest,
    client: HttpStoreClient = Depends(store),
    _: Principal = Depends(require_scopes("sources:write", "notes:write", "artifacts:read")),
) -> JSONResponse:
    try:
        result = await client.context_action("recording", payload.model_dump(mode="json"))
    except StoreError as error:
        return failure(error)
    settled = result["state"] in {"succeeded", "failed"}
    return JSONResponse(status_code=200 if settled else 202, content=result)


@router.get("", response_model=RecordingPage)
async def list_recordings(
    state: str | None = Query(default=None, description="queued, running, succeeded or failed"),
    provider: str | None = Query(default=None, min_length=1),
    external_source_id: str | None = Query(default=None, min_length=1),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, min_length=1),
    client: HttpStoreClient = Depends(store),
    _: Principal = Depends(require_scopes("sources:read")),
) -> JSONResponse:
    """Find a recording again: newest first, filtered by state, provider or the id the
    submitter gave it. Each item is the same record `GET /v1/recordings/{id}` returns."""
    filters = {
        "state": state,
        "provider": provider,
        "external_source_id": external_source_id,
        "cursor": cursor,
    }
    try:
        result = await client.context_action(
            "recording-list",
            {**{k: v for k, v in filters.items() if v is not None}, "limit": limit},
        )
    except StoreError as error:
        return failure(error)
    return JSONResponse(content=result)


@router.get("/waiting")
async def waiting_recordings(
    client: HttpStoreClient = Depends(store),
    _: Principal = Depends(require_scopes("notes:read", "sources:read")),
) -> JSONResponse:
    try:
        return JSONResponse(content=await client.context_action("recording-waiting", {}))
    except StoreError as error:
        return failure(error)


@router.post("/{recording_id}/retry")
async def retry_recording(
    recording_id: str,
    client: HttpStoreClient = Depends(store),
    _: Principal = Depends(require_scopes("sources:write", "notes:write", "artifacts:read")),
) -> JSONResponse:
    try:
        result = await client.context_action("retry-failed-recording", {"id": recording_id})
        return JSONResponse(content=result)
    except StoreError as error:
        return failure(error)


@router.post("/{recording_id}/dismiss")
async def dismiss_recording(
    recording_id: str,
    client: HttpStoreClient = Depends(store),
    _: Principal = Depends(require_scopes("sources:write")),
) -> JSONResponse:
    try:
        result = await client.context_action("dismiss-recording", {"id": recording_id})
        return JSONResponse(content=result)
    except StoreError as error:
        return failure(error)


@router.get("/{recording_id}", response_model=RecordingStatus)
async def recording_status(
    recording_id: str,
    client: HttpStoreClient = Depends(store),
    _: Principal = Depends(require_scopes("sources:read")),
) -> JSONResponse:
    try:
        result = await client.context_action("recording-status", {"id": recording_id})
    except StoreError as error:
        return failure(error)
    return JSONResponse(content=result)
