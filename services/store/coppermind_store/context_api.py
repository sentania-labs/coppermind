"""Authenticated internal operations for calendar and enrichment administration."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from coppermind.errors import to_http
from coppermind.store_protocol import MetadataUnavailable, StoreError
from coppermind_store.auth import require_internal_token
from coppermind_store.context_service import ContextService

ACTIONS = {
    "configure-property": "configure_property",
    "resolve-calendar": "resolve_calendar",
    "setup-lifecycle": "setup_lifecycle",
    "lifecycle": "lifecycle_detail",
    "complete-review": "complete_review",
    "upload-drop": "upload_drop",
    "dismiss-drop": "dismiss_drop",
    "dropbox-list": "dropbox_list",
    "save-intake": "save_intake",
    "save-connection": "save_connection",
    "convert-connection": "convert_connection",
    "discover": "discover",
    "test-model": "test_model",
    "save-assignment": "save_assignment",
    "upload": "upload",
    "retry-import": "retry_import",
    "dismiss-import": "dismiss_import",
    "backlog": "backlog",
    "enqueue": "enqueue",
    "job": "job_detail",
    "retry-job": "retry_job",
    "reject": "reject",
    "approve": "approve",
    "resume-approval": "resume_approval",
    "correct-contact": "correct_contact",
    "correct-association": "correct_association",
    "resume-correction": "resume_correction",
}
router = APIRouter(prefix="/internal/v1/context", dependencies=[Depends(require_internal_token)])


@router.post("/{action}")
async def context_action(action: str, payload: dict[str, Any], request: Request) -> JSONResponse:
    context: ContextService = request.app.state.store.context
    try:
        if action == "snapshot":
            result = await context.snapshot(int(payload.get("offset", 0)))
        elif action in ACTIONS:
            result = await getattr(context, ACTIONS[action])(payload)
        else:
            return JSONResponse({"error": "not_found", "message": "unknown operation"}, 404)
        return JSONResponse(result)
    except SQLAlchemyError:
        status, body = to_http(MetadataUnavailable(), surface="internal")
        return JSONResponse(body, status)
    except StoreError as exc:
        status, body = to_http(exc, surface="internal")
        return JSONResponse(body, status)
    except ValidationError:
        return JSONResponse({"error": "validation_error", "message": "Invalid context fields"}, 422)
    except (KeyError, StopIteration, FileNotFoundError):
        return JSONResponse({"error": "validation_error", "message": "Record not found"}, 422)
    except ValueError as exc:
        return JSONResponse({"error": "validation_error", "message": str(exc)}, 422)
