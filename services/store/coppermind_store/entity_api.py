"""Internal entity operations; the public API supplies per-operation scopes."""

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from coppermind.entities import EntityChange, EntityInput, EntityQuery
from coppermind.errors import to_http
from coppermind.store_protocol import MetadataUnavailable, StoreError, ValidationFailed
from coppermind_store.auth import require_internal_token

router = APIRouter(prefix="/internal/v1/entities", dependencies=[Depends(require_internal_token)])


@router.post("/{action}")
async def entity_action(action: str, payload: dict[str, Any], request: Request) -> JSONResponse:
    entities = request.app.state.store.entities
    try:
        if action == "list":
            result = await entities.list(EntityQuery.model_validate(payload))
        elif action == "get":
            result = await entities.get(payload["id"])
        elif action == "create":
            result = await entities.create(EntityInput.model_validate(payload))
        elif action == "change":
            result = await entities.change(
                payload["id"], EntityChange.model_validate(payload["change"])
            )
        else:
            raise ValidationFailed(["unknown entity operation"])
        return JSONResponse(result)
    except StoreError as exc:
        status, body = to_http(exc, surface="internal")
    except SQLAlchemyError:
        status, body = to_http(MetadataUnavailable(), surface="internal")
    except (ValueError, KeyError, ValidationError):
        status, body = to_http(ValidationFailed(["Invalid entity fields"]), surface="internal")
    return JSONResponse(body, status)
