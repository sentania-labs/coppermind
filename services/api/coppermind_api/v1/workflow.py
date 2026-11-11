"""Scoped workflow operations for the Obsidian companion and binary intake."""

from __future__ import annotations

import base64
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from coppermind.errors import envelope
from coppermind.meeting_summary import MeetingSummary
from coppermind.store_client import HttpStoreClient
from coppermind.store_protocol import StoreError
from coppermind_api.auth import Principal
from coppermind_api.deps import require_scopes, store
from coppermind_api.errors import failure
from coppermind_api.v1.artifacts import too_large

router = APIRouter(prefix="/v1", tags=["workflow"])


class ReviewCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    if_match: str = Field(min_length=1, max_length=100)
    confirmation: str
    job_id: str | None = None
    selected: list[int] = Field(default_factory=list, max_length=30)
    meeting_summary: MeetingSummary | None = None
    speaker_choices: dict[str, str] = Field(default_factory=dict, max_length=30)
    manual_speakers: dict[str, str] = Field(default_factory=dict, max_length=30)
    manual_source_revision: int | None = Field(default=None, ge=1)
    manual_source_id: str | None = None
    accept_link_risk: bool = False


async def call(client: HttpStoreClient, action: str, payload: dict[str, Any]) -> Response:
    try:
        return JSONResponse(await client.context_action(action, payload))
    except StoreError as exc:
        return failure(exc)


PEOPLE_HIDDEN = (
    "This key cannot read people, so speakers cannot be named with it. In Coppermind Admin, "
    "open Connected apps (API Keys) and create a new key with Read notes, Update notes, "
    "Read sources, Update sources, and Read people and companies, "
    "then select that key in the companion settings."
)


@router.get("/notes/{note_id}/review")
async def review(
    note_id: str,
    client: HttpStoreClient = Depends(store),
    principal: Principal = Depends(require_scopes("notes:read", "sources:read")),
) -> Response:
    try:
        detail = await client.context_action("lifecycle", {"note_id": note_id})
    except StoreError as exc:
        return failure(exc)
    detail["people_access_notice"] = PEOPLE_HIDDEN
    if principal.has("entities:read"):
        return JSONResponse(detail)
    hidden = detail.get("manual_speaker_review") is not None or any(
        s.get("kind") == "speaker" for s in detail.get("suggestions", [])
    )
    detail["suggestions"] = [s for s in detail.get("suggestions", []) if s.get("kind") != "speaker"]
    detail["manual_speaker_review"] = None
    if detail.get("recording_meeting"):
        detail["recording_meeting"]["candidates"] = [
            {**candidate, "people": []}
            for candidate in detail["recording_meeting"].get("candidates", [])
        ]
    detail["speakers_unavailable"] = PEOPLE_HIDDEN if hidden else ""
    return JSONResponse(detail)


@router.post("/notes/{note_id}/review")
async def finish(
    note_id: str,
    payload: ReviewCompletion,
    client: HttpStoreClient = Depends(store),
    principal: Principal = Depends(require_scopes("notes:write", "notes:read", "sources:read")),
) -> Response:
    def forbidden() -> HTTPException:
        return HTTPException(
            status_code=403,
            detail=envelope("forbidden", "the API key does not grant the required scope"),
        )

    manual = [value for value in payload.manual_speakers.values() if value]
    if manual and not principal.has("sources:write", "entities:read"):
        raise forbidden()
    if payload.selected and not principal.has("sources:write", "entities:read"):
        # Inspect the durable job, not client-supplied labels. This also covers retries
        # after a speaker decision has already been recorded in an unfinished review.
        try:
            job = await client.context_action("job", {"id": payload.job_id or ""})
        except StoreError as exc:
            return failure(exc)
        suggestions = (job.get("result") or {}).get("suggestions", [])
        for index in payload.selected:
            if index < 0 or index >= len(suggestions):
                raise forbidden()
            item = suggestions[index]
            choice = payload.speaker_choices.get(str(index), item.get("value") or "")
            if item.get("kind") == "speaker" and choice:
                raise forbidden()
    return await call(
        client,
        "complete-review",
        {
            "note_id": note_id,
            **payload.model_dump(),
            "can_create_people": principal.has("entities:write"),
        },
    )


@router.post("/notes/{note_id}/suggestions")
async def refresh(
    note_id: str,
    client: HttpStoreClient = Depends(store),
    _: Principal = Depends(require_scopes("notes:write", "notes:read", "sources:read")),
) -> Response:
    return await call(
        client,
        "enqueue",
        {
            "note_ids": [note_id],
            "regenerate": True,
            "confirmation": "Send selected notes for enrichment",
        },
    )


@router.get("/dropbox")
async def dropbox_list(
    client: HttpStoreClient = Depends(store),
    _: Principal = Depends(require_scopes("sources:read")),
) -> Response:
    return await call(client, "dropbox-list", {})


@router.post("/dropbox/{receipt_id}/dismiss")
async def dismiss_drop(
    receipt_id: str,
    client: HttpStoreClient = Depends(store),
    _: Principal = Depends(require_scopes("sources:write")),
) -> Response:
    return await call(client, "dismiss-drop", {"id": receipt_id})


@router.post("/dropbox", status_code=202)
async def drop(
    request: Request,
    filename: str = Query(min_length=1, max_length=255),
    kind: str | None = Query(default=None, min_length=1, max_length=40),
    client: HttpStoreClient = Depends(store),
    principal: Principal = Depends(require_scopes("sources:write")),
) -> Response:
    """Drop a file. Calendars route themselves; anything else needs `kind=artifact`.

    A drop that declares a kind stores an artifact, so it also needs `artifacts:write`.
    """
    if kind is not None and not principal.has("artifacts:write"):
        return JSONResponse(
            {
                "error": "forbidden",
                "message": "the API key does not grant the required scope",
            },
            403,
        )
    ceiling = 200_000_000
    if kind is not None:
        try:
            ceiling = await client.get_artifact_limit()
        except StoreError as exc:
            return failure(exc)
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > ceiling:
            if kind is not None:
                return too_large(ceiling)
            return JSONResponse(
                {"error": "payload_too_large", "message": "Dropbox limit: 200,000,000 bytes"}, 413
            )
    payload: dict[str, Any] = {
        "filename": filename,
        "content_base64": base64.b64encode(data).decode(),
    }
    if kind is not None:
        payload["kind"] = kind
    result = await call(client, "upload-drop", payload)
    if result.status_code == 200:
        result.status_code = 202
    return result


class CalendarResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    if_match: str
    calendar_hash: str
    event_id: str = ""


class RecordingMeetingChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    if_match: str
    calendar_hash: str
    event_id: str = Field(min_length=1, max_length=500)


@router.post("/notes/{note_id}/recording-meeting")
async def recording_meeting(
    note_id: str,
    payload: RecordingMeetingChoice,
    client: HttpStoreClient = Depends(store),
    _: Principal = Depends(require_scopes("notes:write", "notes:read", "sources:read")),
) -> Response:
    return await call(
        client, "choose-recording-meeting", {"note_id": note_id, **payload.model_dump()}
    )


@router.post("/notes/{note_id}/calendar-update")
async def calendar_update(
    note_id: str,
    payload: CalendarResolution,
    client: HttpStoreClient = Depends(store),
    _: Principal = Depends(require_scopes("notes:write", "notes:read", "sources:read")),
) -> Response:
    return await call(client, "resolve-calendar", {"note_id": note_id, **payload.model_dump()})
