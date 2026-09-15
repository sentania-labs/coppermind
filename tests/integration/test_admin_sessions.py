"""PostgreSQL backed admin browser sessions.

The statements and the `admin_sessions` table they read are only exercised
here: the unit suite drives Admin through an in-memory double.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from coppermind_admin.auth import PostgresSessions, SessionsUnavailable, token_hash
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coppermind.db.session import make_engine, make_session_factory


@pytest.fixture
async def sessions(session_factory: async_sessionmaker[AsyncSession]) -> PostgresSessions:
    async with session_factory() as session, session.begin():
        await session.execute(sa.text("TRUNCATE TABLE admin_sessions"))
    return PostgresSessions(session_factory)


async def _insert(
    factory: async_sessionmaker[AsyncSession], token: str, expires_at: datetime
) -> None:
    now = datetime.now(tz=UTC)
    async with factory() as session, session.begin():
        await session.execute(
            sa.text(
                "INSERT INTO admin_sessions (token_hash, created_at, expires_at) "
                "VALUES (:token_hash, :created_at, :expires_at)"
            ),
            {"token_hash": token_hash(token), "created_at": now, "expires_at": expires_at},
        )


async def test_a_created_session_is_valid_until_it_is_deleted(sessions: PostgresSessions):
    token = await sessions.create(timedelta(hours=12))

    assert await sessions.valid(token) is True
    assert await sessions.valid("not the token that was issued") is False

    await sessions.delete(token)
    assert await sessions.valid(token) is False


async def test_an_expired_session_is_refused_and_swept(
    sessions: PostgresSessions, session_factory: async_sessionmaker[AsyncSession]
):
    stale = "expired-session-token"
    await _insert(session_factory, stale, datetime.now(tz=UTC) - timedelta(seconds=1))
    assert await sessions.valid(stale) is False

    live = await sessions.create(timedelta(hours=12))
    async with session_factory() as session:
        remaining = (
            (await session.execute(sa.text("SELECT token_hash FROM admin_sessions")))
            .scalars()
            .all()
        )
    assert remaining == [token_hash(live)]


async def test_revoke_all_ends_every_session_at_once(sessions: PostgresSessions):
    first = await sessions.create(timedelta(hours=12))
    second = await sessions.create(timedelta(hours=12))

    await sessions.revoke_all()

    assert await sessions.valid(first) is False
    assert await sessions.valid(second) is False


async def test_an_outage_raises_the_error_admin_answers_503_for():
    engine = make_engine(
        "postgresql+asyncpg://coppermind:coppermind@127.0.0.1:5999/coppermind_absent"
    )
    unreachable = PostgresSessions(make_session_factory(engine))
    try:
        assert await unreachable.ready() is False
        with pytest.raises(SessionsUnavailable):
            await unreachable.create(timedelta(hours=12))
        with pytest.raises(SessionsUnavailable):
            await unreachable.valid("any token")
        with pytest.raises(SessionsUnavailable):
            await unreachable.delete("any token")
        with pytest.raises(SessionsUnavailable):
            await unreachable.revoke_all()
    finally:
        await engine.dispose()
