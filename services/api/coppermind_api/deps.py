"""What a request handler is given.

The store client is built once at startup and shared. The API keeps no other
state: no note cache, no filesystem handle, no session store in this slice.
"""

from __future__ import annotations

from fastapi import Request

from coppermind.store_client import HttpStoreClient


def store(request: Request) -> HttpStoreClient:
    return request.app.state.store
