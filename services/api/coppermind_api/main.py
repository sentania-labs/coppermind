"""The API service process.

Health and readiness tell the truth. `/healthz` says the process is up.
`/readyz` says the API can serve note operations, which means the store says
it is ready, which in turn means the notes filesystem is writable and
PostgreSQL is reachable. Stop PostgreSQL and this endpoint returns 503 with
the reason rather than reporting healthy next to a failing surface.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from coppermind.errors import envelope
from coppermind.health import Check, Health, Readiness
from coppermind.logging import configure_logging, get_logger
from coppermind.settings import Wiring
from coppermind.store_client import HttpStoreClient
from coppermind_api import __version__
from coppermind_api.v1.notes import router as notes_router

SERVICE = "coppermind-api"

# Codes for the answers the framework raises before a route is reached.
_CODES = {404: "not_found", 405: "method_not_allowed"}

log = get_logger(SERVICE)

DESCRIPTION = """
The Coppermind public contract.

Notes live as Markdown files in the notes filesystem. This service holds no
state of its own: every note operation is a call to the store, which is the
only process that writes those files.
""".strip()


def create_app(wiring: Wiring | None = None) -> FastAPI:
    settings = wiring or Wiring()
    configure_logging(SERVICE, settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client = HttpStoreClient(
            settings.store_url,
            settings.read_internal_token(),
            timeout=settings.store_timeout_s,
        )
        app.state.wiring = settings
        app.state.store = client
        log.info("api started", store=settings.store_url)
        try:
            yield
        finally:
            await client.aclose()

    app = FastAPI(
        title="Coppermind",
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
    )

    # Registered on Starlette's HTTPException rather than FastAPI's subclass so
    # that routing 404s and 405s, which Starlette raises directly, answer in the
    # documented envelope too.
    @app.exception_handler(HTTPException)
    async def _envelope_handler(_: Request, exc: HTTPException) -> JSONResponse:
        """Keep the error envelope flat instead of nesting it under `detail`."""
        if isinstance(exc.detail, dict):
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content=envelope(_CODES.get(exc.status_code, "error"), str(exc.detail)),
        )

    @app.exception_handler(RequestValidationError)
    async def _request_validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        """A malformed request body is the same shape as a schema violation."""
        problems = [
            f"{'.'.join(str(part) for part in error['loc'][1:])}: {error['msg']}".lstrip(": ")
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=envelope("validation_error", "; ".join(problems), errors=problems),
        )

    @app.get("/healthz", response_model=Health, tags=["operations"])
    async def healthz() -> Health:
        return Health(service=SERVICE, version=__version__)

    @app.get("/readyz", tags=["operations"])
    async def readyz(request: Request) -> JSONResponse:
        client: HttpStoreClient = request.app.state.store
        store_ready = await client.is_ready()
        checks = [
            Check(
                name="store",
                ok=store_ready,
                detail="" if store_ready else "the store is not ready; note operations will 503",
            )
        ]
        readiness = Readiness.of(checks)
        return JSONResponse(
            status_code=200 if readiness.ready else 503,
            content=readiness.model_dump(mode="json"),
        )

    app.include_router(notes_router)
    return app


app = create_app()
