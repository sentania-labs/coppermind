"""Authenticated internal operations for the Admin interface."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from coppermind.errors import to_http
from coppermind.store_protocol import MetadataUnavailable, StoreError, ValidationFailed
from coppermind_store.auth import require_internal_token
from coppermind_store.calendar_import import CalendarImportError

router = APIRouter(prefix="/internal/v1/admin", dependencies=[Depends(require_internal_token)])


@router.post("/{action}")
async def admin_action(action: str, payload: dict[str, Any], request: Request) -> JSONResponse:
    admin = request.app.state.store.admin
    try:
        return JSONResponse(await admin.act(action, payload))
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
