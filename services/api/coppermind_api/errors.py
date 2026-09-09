"""Turning store errors into public answers.

The public envelope is the same one the internal contract uses, so an operator
reading an API error and an engineer reading a store log see the same code.
"""

from __future__ import annotations

from fastapi.responses import JSONResponse

from coppermind.errors import to_http
from coppermind.store_protocol import StoreError


def failure(error: StoreError) -> JSONResponse:
    status_code, body = to_http(error)
    return JSONResponse(status_code=status_code, content=body)
