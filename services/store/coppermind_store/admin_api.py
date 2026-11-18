"""Authenticated internal operations for the Admin interface."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from coppermind.errors import to_http
from coppermind.store_protocol import MetadataUnavailable, StoreError, ValidationFailed
from coppermind_store.admin_calendar import CALENDAR_UPLOAD_LIMIT
from coppermind_store.auth import require_internal_token
from coppermind_store.calendar_import import CalendarImportError

router = APIRouter(prefix="/internal/v1/admin", dependencies=[Depends(require_internal_token)])


async def _respond(call: Awaitable[dict[str, Any]]) -> JSONResponse:
    try:
        return JSONResponse(await call)
    except StoreError as exc:
        status, body = to_http(exc, surface="internal")
    except SQLAlchemyError:
        status, body = to_http(MetadataUnavailable(), surface="internal")
    except KeyError:
        status, body = to_http(ValidationFailed(["That is no longer listed."]), surface="internal")
    except ValidationError:
        status, body = to_http(ValidationFailed(["Invalid fields"]), surface="internal")
    except (ValueError, CalendarImportError) as exc:
        status, body = to_http(ValidationFailed([str(exc)]), surface="internal")
    return JSONResponse(body, status)


async def _body(request: Request, limit: int) -> bytes:
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > limit:
            raise ValueError(f"The file is larger than {limit:,} bytes, the most allowed.")
    return bytes(data)


@router.post("/calendar-file")
async def calendar_file(filename: str, request: Request) -> JSONResponse:
    """A calendar file as the raw request body, so it is never encoded into JSON."""
    admin = request.app.state.store.admin

    async def add() -> dict[str, Any]:
        return await admin.calendar_file(filename, await _body(request, CALENDAR_UPLOAD_LIMIT))

    return await _respond(add())


@router.post("/{action}")
async def admin_action(action: str, payload: dict[str, Any], request: Request) -> JSONResponse:
    admin = request.app.state.store.admin
    return await _respond(admin.act(action, payload))
