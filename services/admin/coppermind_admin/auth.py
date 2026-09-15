"""Filesystem credentials and PostgreSQL-backed browser sessions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

import sqlalchemy as sa
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coppermind.atomicio import create_exclusive_bytes

_HASHER = PasswordHasher()


class AlreadyClaimed(Exception):
    pass


class InvalidClaimCode(Exception):
    pass


class AdminCredentials:
    """The one durable admin credential record from control state."""

    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / "admin.json"
        self.claim_code_path = state_dir / "internal" / "claim-code"
        self._claim_lock = asyncio.Lock()

    def is_claimed(self) -> bool:
        return self.path.is_file()

    async def claim(self, code: str, password: str) -> None:
        async with self._claim_lock:
            if self.is_claimed():
                raise AlreadyClaimed
            try:
                expected = self.claim_code_path.read_text(encoding="utf-8").strip()
            except FileNotFoundError as exc:
                raise InvalidClaimCode from exc
            if not expected or not secrets.compare_digest(code, expected):
                raise InvalidClaimCode
            password_hash = await asyncio.to_thread(_HASHER.hash, password)
            body = {
                "schema_version": 1,
                "revision": 1,
                "password_hash": password_hash,
                "claimed_at": datetime.now(tz=UTC).isoformat(),
            }
            try:
                create_exclusive_bytes(
                    self.path,
                    (json.dumps(body, indent=2) + "\n").encode(),
                    mode=0o600,
                )
            except FileExistsError as exc:
                raise AlreadyClaimed from exc
            self.claim_code_path.unlink(missing_ok=True)

    async def verify_password(self, password: str) -> bool:
        try:
            record = json.loads(self.path.read_text(encoding="utf-8"))
            encoded = record["password_hash"]
            return await asyncio.to_thread(_HASHER.verify, encoded, password)
        except (FileNotFoundError, KeyError, TypeError, InvalidHashError, VerificationError):
            return False


class Sessions(Protocol):
    async def create(self, lifetime: timedelta) -> str: ...

    async def valid(self, token: str) -> bool: ...

    async def delete(self, token: str) -> None: ...

    async def ready(self) -> bool: ...


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class PostgresSessions:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory

    async def create(self, lifetime: timedelta) -> str:
        token = secrets.token_urlsafe(32)
        now = datetime.now(tz=UTC)
        async with self.factory() as session, session.begin():
            await session.execute(
                sa.text("DELETE FROM admin_sessions WHERE expires_at <= :now"), {"now": now}
            )
            await session.execute(
                sa.text(
                    "INSERT INTO admin_sessions "
                    "(token_hash, created_at, expires_at, last_seen_at) "
                    "VALUES (:token_hash, :created_at, :expires_at, :last_seen_at)"
                ),
                {
                    "token_hash": token_hash(token),
                    "created_at": now,
                    "expires_at": now + lifetime,
                    "last_seen_at": now,
                },
            )
        return token

    async def valid(self, token: str) -> bool:
        now = datetime.now(tz=UTC)
        async with self.factory() as session, session.begin():
            result = await session.execute(
                sa.text(
                    "UPDATE admin_sessions SET last_seen_at = :now "
                    "WHERE token_hash = :token_hash AND expires_at > :now "
                    "RETURNING token_hash"
                ),
                {"token_hash": token_hash(token), "now": now},
            )
            return result.scalar_one_or_none() is not None

    async def delete(self, token: str) -> None:
        async with self.factory() as session, session.begin():
            await session.execute(
                sa.text("DELETE FROM admin_sessions WHERE token_hash = :token_hash"),
                {"token_hash": token_hash(token)},
            )

    async def ready(self) -> bool:
        try:
            async with self.factory() as session:
                await session.execute(sa.text("SELECT 1"))
            return True
        except Exception:
            return False
