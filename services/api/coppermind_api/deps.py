"""What a request handler is given.

The store client and bounded authentication cache are built once at startup.
The API keeps no filesystem handle and no durable state.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from coppermind.errors import envelope
from coppermind.store_client import HttpStoreClient
from coppermind_api.auth import Principal

bearer = HTTPBearer(auto_error=False)


def store(request: Request) -> HttpStoreClient:
    return request.app.state.store


def authenticated_key(
    request: Request,
    _: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)] = None,
) -> Principal:
    """Return the principal established by the ``/v1`` boundary middleware."""
    principal: Principal | None = getattr(request.state, "api_key", None)
    if principal is None:
        raise HTTPException(
            status_code=401,
            detail=envelope("unauthorized", "a valid API bearer key is required"),
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal


def require_scopes(*scopes: str) -> Callable[..., Principal]:
    async def dependency(principal: Principal = Depends(authenticated_key)) -> Principal:
        if not principal.has(*scopes):
            raise HTTPException(
                status_code=403,
                detail=envelope("forbidden", "the API key does not grant the required scope"),
            )
        return principal

    return dependency


def forbid_missing_scope(principal: Principal, scope: str) -> None:
    if not principal.has(scope):
        raise HTTPException(
            status_code=403,
            detail=envelope("forbidden", "the API key does not grant the required scope"),
        )
