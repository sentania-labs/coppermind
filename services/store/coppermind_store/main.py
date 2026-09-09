"""The store service process.

Startup order matters and is deliberate: read the wiring, make sure the
control state files exist with their shipped defaults, open the database
engine, then serve. The engine is opened lazily by SQLAlchemy, so the store
starts and reports itself unhealthy rather than crash looping when PostgreSQL
is not up yet.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import sqlalchemy as sa
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from coppermind.db.session import make_engine, make_session_factory
from coppermind.health import Check, Health, Readiness
from coppermind.logging import configure_logging, get_logger
from coppermind.settings import Wiring
from coppermind_store import __version__
from coppermind_store.auth import InternalAuth
from coppermind_store.control import ControlState
from coppermind_store.fs import is_writable
from coppermind_store.internal_api import router as internal_router
from coppermind_store.notes import LocalStore

SERVICE = "coppermind-store"

log = get_logger(SERVICE)


def create_app(wiring: Wiring | None = None) -> FastAPI:
    settings = wiring or Wiring()
    configure_logging(SERVICE, settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        control = ControlState(settings.state_dir)
        control.ensure_defaults()
        engine = make_engine(settings.database_url_for("asyncpg"))
        factory = make_session_factory(engine)
        app.state.wiring = settings
        app.state.auth = InternalAuth.from_wiring(settings)
        app.state.store = LocalStore(settings.notes_dir, control, factory)
        app.state.engine = engine
        log.info(
            "store started",
            notes_filesystem=str(settings.notes_dir),
            state=str(settings.state_dir),
        )
        try:
            yield
        finally:
            await engine.dispose()

    app = FastAPI(
        title="Coppermind store",
        version=__version__,
        description=(
            "Internal contract of the store, the only writer of the notes filesystem. "
            "Not a public surface."
        ),
        lifespan=lifespan,
    )

    @app.exception_handler(HTTPException)
    async def _envelope_handler(_: Request, exc: HTTPException) -> JSONResponse:
        """Keep the error envelope flat instead of nesting it under `detail`."""
        if isinstance(exc.detail, dict):
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": "error", "message": str(exc.detail)},
        )

    @app.get("/healthz", response_model=Health)
    async def healthz() -> Health:
        return Health(service=SERVICE, version=__version__)

    @app.get("/readyz")
    async def readyz(request: Request) -> JSONResponse:
        """Ready means the store can actually do its job.

        With PostgreSQL stopped this reports not ready and says so. The notes
        filesystem stays readable and writable by Obsidian Sync throughout; it
        is the API surface that steps back, not the notes.
        """
        writable, detail = is_writable(request.app.state.wiring.notes_dir)
        checks = [Check(name="notes_filesystem", ok=writable, detail=detail)]
        try:
            async with request.app.state.engine.connect() as connection:
                await connection.execute(sa.text("SELECT 1"))
            checks.append(Check(name="metadata", ok=True))
        except (SQLAlchemyError, OSError) as exc:
            checks.append(Check(name="metadata", ok=False, detail=str(exc)))
        readiness = Readiness.of(checks)
        return JSONResponse(
            status_code=200 if readiness.ready else 503,
            content=readiness.model_dump(mode="json"),
        )

    app.include_router(internal_router)
    return app


app = create_app()
