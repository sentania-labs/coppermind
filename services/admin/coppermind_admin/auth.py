"""Filesystem credentials and stateless signed browser sessions."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from coppermind.atomicio import create_exclusive_bytes
from coppermind.statefiles import StateStore

_HASHER = PasswordHasher()


# How long a new setup link for a forgotten password works after the server issued it.
RESET_LINK_SECONDS = 3600


class AlreadyClaimed(Exception):
    pass


class InvalidClaimCode(Exception):
    pass


class AdminRecordUnreadable(Exception):
    """The admin record is there but is not a usable credential record."""


class ClaimStateProblem(Exception):
    """The state directory refused something a claim needs."""

    def __init__(self, path: Path, problem: str) -> None:
        super().__init__(f"{path}: {problem}")
        self.path = path
        self.problem = problem


class ClaimCodeUnreadable(ClaimStateProblem):
    """The claim code is on the volume but Admin cannot read it."""


class ClaimStateUnwritable(ClaimStateProblem):
    """The state directory would not take the admin record."""


class AdminCredentials:
    """The one durable admin credential record from control state.

    Reads go through the state store that owns `admin.json`, and only the
    claim writes the file itself, because an exclusive create is what refuses
    the second claimant when two people open the Claim page at once.
    """

    def __init__(self, state_dir: Path) -> None:
        self.state = StateStore(state_dir)
        self.path = self.state.path_for("admin")
        self.claim_code_path = state_dir / "internal" / "claim-code"
        self.reset_code_path = state_dir / "internal" / "reset-code"
        self._claim_lock = asyncio.Lock()

    def is_claimed(self) -> bool:
        return self.path.is_file()

    def reset_offered(self) -> bool:
        """Whether a new setup link was issued from the server and is still good.

        The link is shown where the server was started, which is also where its log is kept,
        so it works for an hour and then no longer.
        """
        if not self.is_claimed():
            return False
        try:
            age = time.time() - self.reset_code_path.stat().st_mtime
        except OSError:
            return False
        return age < RESET_LINK_SECONDS

    @staticmethod
    def _same_code(offered: str, expected: str) -> bool:
        """A setup code is typed by hand, so dashes, spaces and letter case do not count."""

        def plain(value: str) -> bytes:
            return "".join(c for c in value if c.isalnum()).upper().encode()

        # Compared as bytes: a code pasted out of a terminal can carry a
        # non-ASCII character, which compare_digest refuses on str.
        return bool(plain(expected)) and secrets.compare_digest(plain(offered), plain(expected))

    async def reset(self, code: str, password: str) -> None:
        """Choose a new password with a setup link issued from the server.

        The session secret is replaced with it, so every browser signed in before
        is signed out. Notes and settings are not touched.
        """
        async with self._claim_lock:
            if not self.reset_offered():
                raise InvalidClaimCode
            try:
                expected = self.reset_code_path.read_text(encoding="utf-8").strip()
            except FileNotFoundError as exc:
                raise InvalidClaimCode from exc
            except OSError as exc:
                raise ClaimCodeUnreadable(self.reset_code_path, str(exc)) from exc
            if not self._same_code(code, expected):
                raise InvalidClaimCode
            await self._replace_password(password)
            with contextlib.suppress(OSError):
                self.reset_code_path.unlink(missing_ok=True)

    async def _replace_password(self, password: str) -> None:
        password_hash = await asyncio.to_thread(_HASHER.hash, password)
        try:
            current = self.state.read("admin")
        except (OSError, ValueError) as exc:
            raise AdminRecordUnreadable(str(exc)) from exc
        body = dict(current.body)
        body["password_hash"] = password_hash
        body["session_secret"] = secrets.token_hex(32)
        try:
            self.state.write("admin", body, if_revision=current.revision)
        except OSError as exc:
            raise ClaimStateUnwritable(self.path, str(exc)) from exc

    async def change_password(self, current: str, new: str) -> bool:
        """Change the password from inside Admin. False when the current one is wrong."""
        if not await self.verify_password(current):
            return False
        await self._replace_password(new)
        return True

    async def claim(self, code: str, password: str) -> None:
        """Take the admin record for the holder of the claim code."""
        async with self._claim_lock:
            if self.is_claimed():
                raise AlreadyClaimed
            try:
                expected = self.claim_code_path.read_text(encoding="utf-8").strip()
            except FileNotFoundError as exc:
                raise InvalidClaimCode from exc
            except OSError as exc:
                raise ClaimCodeUnreadable(self.claim_code_path, str(exc)) from exc
            if not self._same_code(code, expected):
                raise InvalidClaimCode
            password_hash = await asyncio.to_thread(_HASHER.hash, password)
            body = {
                "schema_version": 1,
                "revision": 1,
                "password_hash": password_hash,
                "session_secret": secrets.token_hex(32),
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
            except OSError as exc:
                raise ClaimStateUnwritable(self.path, str(exc)) from exc
            with contextlib.suppress(OSError):
                self.claim_code_path.unlink(missing_ok=True)

    def _record(self) -> dict[str, object]:
        try:
            return self.state.read("admin").body
        except (OSError, ValueError) as exc:
            raise AdminRecordUnreadable(str(exc)) from exc

    async def verify_password(self, password: str) -> bool:
        record = self._record()
        encoded = record.get("password_hash")
        if not isinstance(encoded, str) or not encoded.isascii():
            raise AdminRecordUnreadable("password_hash is not an ASCII Argon2 hash string")
        try:
            return await asyncio.to_thread(_HASHER.verify, encoded, password)
        except VerifyMismatchError:
            return False
        except (VerificationError, InvalidHashError) as exc:
            raise AdminRecordUnreadable(
                f"password_hash is not a usable Argon2 hash: {exc}"
            ) from exc

    def session_secret(self) -> bytes:
        encoded = self._record().get("session_secret")
        if not isinstance(encoded, str) or len(encoded) != 64:
            raise AdminRecordUnreadable("session_secret is not a 32-byte hexadecimal value")
        try:
            secret = bytes.fromhex(encoded)
        except ValueError as exc:
            raise AdminRecordUnreadable(
                "session_secret is not a 32-byte hexadecimal value"
            ) from exc
        if len(secret) != 32:
            raise AdminRecordUnreadable("session_secret is not a 32-byte hexadecimal value")
        return secret


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


class SignedSessions:
    """Session state carried by an authenticated cookie, not a database row."""

    def __init__(
        self,
        credentials: AdminCredentials,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.credentials = credentials
        self.now = now or _utcnow

    def create(self, lifetime: timedelta) -> str:
        expires_at = int((self.now() + lifetime).timestamp())
        payload = f"v1.{expires_at}.{secrets.token_urlsafe(18)}"
        signature = self._signature(payload)
        return f"{payload}.{signature}"

    def valid(self, token: str) -> bool:
        if len(token) > 512 or not token.isascii():
            return False
        try:
            version, expires_at, nonce, signature = token.split(".")
            expiry = int(expires_at)
        except (TypeError, ValueError):
            return False
        if version != "v1" or not nonce or expiry <= int(self.now().timestamp()):
            return False
        payload = f"{version}.{expires_at}.{nonce}"
        return hmac.compare_digest(signature, self._signature(payload))

    def csrf_token(self, token: str) -> str:
        """Derive the form token from a server-authenticated session."""
        return hmac.new(
            self.credentials.session_secret(),
            f"csrf.{token}".encode(),
            hashlib.sha256,
        ).hexdigest()

    def _signature(self, payload: str) -> str:
        digest = hmac.new(
            self.credentials.session_secret(), payload.encode(), hashlib.sha256
        ).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
