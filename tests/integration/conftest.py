"""Fixtures for the PostgreSQL backed tests.

These run against a real server because the behaviour under test is the write
protocol itself: what reaches the filesystem, what reaches the database, and
in what order. A stand-in database would prove none of it.

    make db-up && make test-integration && make db-down
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from coppermind_store.control import ControlState
from coppermind_store.notes import LocalStore
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coppermind.db.session import make_engine, make_session_factory
from coppermind.settings import Wiring

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_URL = "postgresql://coppermind:coppermind@127.0.0.1:5433/coppermind_test"


def _base_url() -> str:
    return os.environ.get("COPPERMIND_TEST_DATABASE_URL", DEFAULT_URL)


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> Iterator[str]:
    """Bring the test database to the current migration head, once."""
    url = _base_url()
    environment = {**os.environ, "COPPERMIND_DATABASE_URL": url}
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
        cwd=REPO_ROOT,
        env=environment,
        check=True,
        capture_output=True,
    )
    yield url


@pytest.fixture
def wiring(tmp_path: Path) -> Wiring:
    return Wiring(data_dir=tmp_path / "data", database_url=_base_url())


@pytest.fixture
async def session_factory(wiring: Wiring) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine(wiring.database_url_for("asyncpg"))
    factory = make_session_factory(engine)
    # Each test starts from an empty mirror; the notes filesystem is a fresh
    # temporary directory, so leftover rows would point at files that are gone.
    async with engine.begin() as connection:
        await connection.execute(sa.text("TRUNCATE TABLE notes"))
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture
def control(wiring: Wiring) -> ControlState:
    state = ControlState(wiring.state_dir)
    state.ensure_defaults()
    return state


@pytest.fixture
def store(
    wiring: Wiring, control: ControlState, session_factory: async_sessionmaker[AsyncSession]
) -> LocalStore:
    wiring.notes_dir.mkdir(parents=True, exist_ok=True)
    return LocalStore(wiring.notes_dir, control, session_factory)


@pytest.fixture
def unreachable_store(wiring: Wiring, control: ControlState) -> LocalStore:
    """A store whose database is not there, which is the outage under test."""
    engine = make_engine(
        "postgresql+asyncpg://coppermind:coppermind@127.0.0.1:5999/coppermind_absent"
    )
    wiring.notes_dir.mkdir(parents=True, exist_ok=True)
    return LocalStore(wiring.notes_dir, control, make_session_factory(engine))
