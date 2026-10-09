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
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coppermind.db.session import make_engine, make_session_factory
from coppermind.settings import Wiring

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_URL = "postgresql://coppermind@127.0.0.1:5433/coppermind_test"


def _split_url() -> tuple[str, str]:
    """The test server's URL without a password, and the password to use.

    The services refuse a URL carrying a password, because credentials are
    files. A test server handed over as one URL with its password in it, which
    is how a CI service container or a worker's database usually arrives, is
    split here: the password goes into the file the fixtures write, and the URL
    without it is what the code under test sees.
    """
    url = make_url(os.environ.get("COPPERMIND_TEST_DATABASE_URL", DEFAULT_URL))
    password = url.password if url.password is not None else "coppermind"
    return url._replace(password=None).render_as_string(hide_password=False), str(password)


def _base_url() -> str:
    return _split_url()[0]


def _password_file(directory: Path) -> Path:
    path = directory / "postgres-password"
    path.write_text(_split_url()[1] + "\n", encoding="utf-8")
    return path


@pytest.fixture(scope="session", autouse=True)
def migrated_database(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Bring the test database to the current migration head, once."""
    url = _base_url()
    password = _password_file(tmp_path_factory.mktemp("database"))
    environment = {
        **os.environ,
        "COPPERMIND_DATABASE_URL": url,
        "COPPERMIND_DB_PASSWORD_FILE": str(password),
    }
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
    password = _password_file(tmp_path)
    return Wiring(data_dir=tmp_path / "data", database_url=_base_url(), db_password_file=password)


@pytest.fixture
async def session_factory(wiring: Wiring) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine(wiring.database_url_for("asyncpg"))
    factory = make_session_factory(engine)
    # Each test starts from an empty mirror; the notes filesystem is a fresh
    # temporary directory, so leftover rows would point at files that are gone.
    async with engine.begin() as connection:
        await connection.execute(
            sa.text(
                "TRUNCATE TABLE notes, sources, recorded_rejections, outbox_events, "
                "search_documents CASCADE"
            )
        )
        await connection.execute(
            sa.text(
                "UPDATE search_index_state SET last_changed_at = NULL, "
                "rebuild_started_at = NULL, rebuild_completed_at = NULL, rebuild_error = NULL"
            )
        )
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
    return LocalStore(wiring.notes_dir, control, session_factory, wiring.sources_dir)


@pytest.fixture
def unreachable_store(wiring: Wiring, control: ControlState) -> LocalStore:
    """A store whose database is not there, which is the outage under test."""
    engine = make_engine(
        "postgresql+asyncpg://coppermind:coppermind@127.0.0.1:5999/coppermind_absent"
    )
    wiring.notes_dir.mkdir(parents=True, exist_ok=True)
    return LocalStore(wiring.notes_dir, control, make_session_factory(engine), wiring.sources_dir)
