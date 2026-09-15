"""Bearer-key authentication for every public ``/v1`` route."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass

import anyio.to_thread

from coppermind.api_keys import ApiKeyRecord, split_credential, verify_secret
from coppermind.store_protocol import Store

CACHE_TTL_SECONDS = 300.0
# A key minted since the last load must work now, not in five minutes. An
# unknown key id may therefore cost a load, and this floor is how often: one
# load per window however many requests arrive, because callers that meet a
# load already running await that one attempt instead of starting another.
UNKNOWN_KEY_RELOAD_FLOOR_SECONDS = 1.0
# Argon2 at the shipped cost holds 64 MiB per verification, so the number that
# can run at once is capped rather than left to the default thread limiter.
VERIFY_CONCURRENCY = 4


class AuthenticationUnavailable(Exception):
    """The key control state could not be loaded from the store."""


@dataclass(frozen=True)
class Principal:
    key_id: str
    scopes: frozenset[str]

    def has(self, *required: str) -> bool:
        return all(scope in self.scopes for scope in required)


@dataclass(frozen=True)
class _Verified:
    principal: Principal
    expires_at: float


class ApiKeyAuthenticator:
    """Verify API keys, with a bounded cache of hashes and successful checks."""

    def __init__(
        self,
        store: Store,
        *,
        ttl_seconds: float = CACHE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._ttl = ttl_seconds
        self._clock = clock
        self._records: dict[str, ApiKeyRecord] = {}
        self._records_expire_at = 0.0
        self._reload_allowed_at = 0.0
        self._verified: dict[bytes, _Verified] = {}
        self._loading: asyncio.Task[dict[str, ApiKeyRecord]] | None = None
        self._lock = asyncio.Lock()
        self._verify_limiter = anyio.CapacityLimiter(VERIFY_CONCURRENCY)

    async def records(self) -> dict[str, ApiKeyRecord]:
        """Return the key records, loading them at most once per cache life."""
        if self._records_expire_at > self._clock():
            return self._records
        return await self._load()

    async def _reload_for_unknown_key(self) -> dict[str, ApiKeyRecord]:
        if self._reload_allowed_at > self._clock():
            return self._records
        return await self._load()

    async def _load(self) -> dict[str, ApiKeyRecord]:
        """Join the read already running, or start the only one that will run.

        A store that answers slowly, or not at all, therefore costs one attempt
        and one answer for every caller waiting on it rather than one each.
        """
        if self._loading is None:
            self._loading = asyncio.create_task(self._read_key_set())
        return await asyncio.shield(self._loading)

    async def _read_key_set(self) -> dict[str, ApiKeyRecord]:
        try:
            key_set = await self._store.get_api_keys()
        except Exception as exc:  # store errors become one public 503
            raise AuthenticationUnavailable from exc
        finally:
            self._loading = None
        now = self._clock()
        self._records = {record.key_id: record for record in key_set.keys}
        self._records_expire_at = now + self._ttl
        self._reload_allowed_at = now + UNKNOWN_KEY_RELOAD_FLOOR_SECONDS
        self._verified = {
            digest: result for digest, result in self._verified.items() if result.expires_at > now
        }
        return self._records

    async def has_active_key(self) -> bool:
        """Answer readiness from the same cache the request path reads."""
        records = await self.records()
        return any(record.revoked_at is None for record in records.values())

    async def authenticate(self, authorization: str | None) -> Principal | None:
        if not authorization or not authorization.startswith("Bearer "):
            return None
        credential = authorization[len("Bearer ") :].strip()
        parsed = split_credential(credential)
        if parsed is None:
            return None

        fingerprint = hashlib.sha256(credential.encode("utf-8")).digest()
        cached = self._verified.get(fingerprint)
        if cached is not None and cached.expires_at > self._clock():
            return cached.principal

        key_id, secret = parsed
        record = (await self.records()).get(key_id)
        if record is None:
            record = (await self._reload_for_unknown_key()).get(key_id)
        if record is None or record.revoked_at is not None:
            return None
        # Argon2 is deliberately expensive, so it never runs on the event loop.
        if not await anyio.to_thread.run_sync(
            verify_secret, record.hash, secret, limiter=self._verify_limiter
        ):
            return None

        principal = Principal(record.key_id, frozenset(record.scopes))
        async with self._lock:
            self._verified[fingerprint] = _Verified(principal, self._clock() + self._ttl)
        return principal
