"""`/v1/notices`: what Coppermind moved back, so the companion plugin or an agent can say so.

When a reviewed note or an artifact file is moved or renamed by hand in the
vault, the reconciler returns it to its filed place and records a notice: what
moved, where it was found, where it went back and why. The notice is the same
one Admin's Home shows. Nothing needs doing about it; dismissing it only
hides it. A notice about an artifact is visible only to a key that may read
artifacts, the way artifact search hits are.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, Response

from coppermind.errors import envelope
from coppermind.store_client import HttpStoreClient
from coppermind.store_protocol import ReconcilerNotice, StoreError
from coppermind_api.auth import Principal
from coppermind_api.deps import require_scopes, store
from coppermind_api.errors import failure

router = APIRouter(prefix="/v1/notices", tags=["notices"])

ARTIFACT_SCOPES = {"read": "artifacts:read", "write": "artifacts:write"}


def _forbidden() -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content=envelope("forbidden", "the API key does not grant the required scope"),
    )


@router.get("", response_model=list[ReconcilerNotice])
async def list_notices(
    client: HttpStoreClient = Depends(store),
    principal: Principal = Depends(require_scopes("notes:read")),
) -> list[ReconcilerNotice] | JSONResponse:
    """Current notices, newest first. Artifact notices need `artifacts:read` as well."""
    try:
        notices = await client.list_reconciler_notices()
    except StoreError as error:
        return failure(error)
    if principal.has(ARTIFACT_SCOPES["read"]):
        return notices
    return [notice for notice in notices if notice.kind != "artifact"]


@router.post("/{note_id}/dismiss", status_code=204)
async def dismiss_notice(
    note_id: str,
    client: HttpStoreClient = Depends(store),
    principal: Principal = Depends(require_scopes("notes:write")),
) -> Response:
    """Hide a notice; the file stays where it is. An artifact's needs `artifacts:write` too."""
    try:
        notices = await client.list_reconciler_notices()
        notice = next((item for item in notices if item.note_id == note_id), None)
        if (
            notice is not None
            and notice.kind == "artifact"
            and not principal.has(ARTIFACT_SCOPES["write"])
        ):
            return _forbidden()
        await client.dismiss_reconciler_notice(note_id)
    except StoreError as error:
        return failure(error)
    return Response(status_code=204)
