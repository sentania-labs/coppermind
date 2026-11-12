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
        elif action in {"voice-settings", "voice-settings-save"}:
            async with request.app.state.store.context.lock:
                context = request.app.state.store.context
                if action == "voice-settings-save":
                    strong, maybe = payload.get("strong"), payload.get("maybe")
                    if (
                        isinstance(strong, bool)
                        or isinstance(maybe, bool)
                        or not isinstance(strong, (int, float))
                        or not isinstance(maybe, (int, float))
                        or not -1 <= maybe <= strong <= 1
                    ):
                        raise ValidationFailed(
                            [
                                "Maybe must be at most the voice threshold; "
                                "both must be between -1 and 1"
                            ]
                        )
                    context.write("voice-settings", {"strong": strong, "maybe": maybe})
                result = context.read("voice-settings", {"strong": 0.60, "maybe": 0.45})
        elif action in {"voice-recognition", "voice-recognition-save"}:
            from coppermind_store.voice_store import VoiceStore

            async with request.app.state.store.context.lock:
                voice = VoiceStore(request.app.state.store.context)
                if action == "voice-recognition-save":
                    if type(payload.get("enabled")) is not bool:
                        raise ValidationFailed(["Invalid voice recognition setting"])
                    voice.set_recognition_enabled(payload["enabled"])
                result = {"enabled": voice.recognition_enabled()}
        elif action in {"voice-unknown-list", "voice-unknown-delete"}:
            from coppermind_store.voice_store import VoiceStore

            async with request.app.state.store.context.lock:
                voice = VoiceStore(request.app.state.store.context)
                if action == "voice-unknown-delete":
                    voice.delete_unknown(payload["id"])
                result = voice.unknown_summaries()
        elif action in {"voice-combine", "voice-split"}:
            from coppermind_store.voice_relabel import combine, separate

            context = request.app.state.store.context
            async with context.lifecycle.lock, context.lock:
                if action == "voice-combine":
                    await combine(context, payload["id"], payload["target"])
                    result = {"ok": True}
                else:
                    identity = await separate(
                        context,
                        payload["id"],
                        payload["note_id"],
                        payload["label"],
                        payload.get("source_id"),
                    )
                    result = {"id": identity}
        elif action == "voice-list":
            from coppermind_store.voice_store import VoiceStore

            async with request.app.state.store.context.lock:
                voice = VoiceStore(request.app.state.store.context)
                people = entities.load().entities
                result = {
                    identity: {
                        **summary,
                        "name": people[identity].name,
                        "assignments": voice.assignments("person:" + identity),
                    }
                    for identity, summary in voice.summaries().items()
                    if identity in people
                    and people[identity].kind == "person"
                    and not people[identity].retired_at
                    and not people[identity].redirect
                    and summary["signatures"]
                }
        elif action in {"voice-get", "voice-delete"}:
            from coppermind_store.voice_store import VoiceStore

            identity = payload["id"]
            async with request.app.state.store.context.lock:
                entity = entities.load().entities.get(identity)
                if not entity or entity.kind != "person" or entity.retired_at or entity.redirect:
                    raise ValidationFailed(["Person not found"])
                voice = VoiceStore(request.app.state.store.context)
                if action == "voice-delete":
                    voice.delete(identity)
                result = voice.summary(identity)
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
    except (KeyError, ValidationError):
        status, body = to_http(ValidationFailed(["Invalid entity fields"]), surface="internal")
    except ValueError as exc:
        message = (
            str(exc) if action in {"voice-combine", "voice-split"} else "Invalid entity fields"
        )
        status, body = to_http(ValidationFailed([message]), surface="internal")
    return JSONResponse(body, status)
