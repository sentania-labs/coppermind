"""Recording intake: hand in a stored recording and get a transcribed note in Review.

The recording is uploaded first, through `POST /v1/staged` for anything over the inline
limit. `POST /v1/recordings` then names that artifact. The request returns at once: the
transcription runs in the background on the store, as long as the service needs, and the
status route reports where it stands until the note exists. The route needs a
transcription service set up in Admin; without one it refuses with a plain message.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from coppermind.store_client import HttpStoreClient
from coppermind.store_protocol import RecordingRequest, RecordingStatus, StoreError
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
