"""Bearer-key authentication for every public ``/v1`` route."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass

import anyio.to_thread

from coppermind.api_keys import ApiKeyRecord, split_credential, verify_secret
from coppermind.store_protocol import Store

# How long the key records are trusted before they are read again. Short, so that a key
# removed in Admin stops working at once and a change to what it may do applies at once.
CACHE_TTL_SECONDS = 2.0
# How often, at most, the store is told that a key was used.
USE_REPORT_SECONDS = 60.0
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
    """A secret that was checked against one key record's hash.

    Checking is expensive, so the answer is kept for as long as that record still
    carries the same hash. Whether the key is still live, and what it may do, is read
    from the current records every time.
    """

    key_id: str
    hash: str


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
        self._records_generation = 0
        self._verified: dict[bytes, _Verified] = {}
        self._loading: asyncio.Task[dict[str, ApiKeyRecord]] | None = None
        self._lock = asyncio.Lock()
        self._verify_limiter = anyio.CapacityLimiter(VERIFY_CONCURRENCY)
        self._use_reported: dict[str, float] = {}
        self._reports: set[asyncio.Task[None]] = set()

    async def records(self) -> dict[str, ApiKeyRecord]:
        """Return the key records, loading them at most once per cache life."""
        if self._records_expire_at > self._clock():
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
        self._records = {record.key_id: record for record in key_set.keys}
        self._records_expire_at = self._clock() + self._ttl
        self._records_generation += 1
        # A checked secret stays checked while its record keeps the hash it was checked
        # against. A removed, revoked or replaced key drops out here.
        self._verified = {
            fingerprint: checked
            for fingerprint, checked in self._verified.items()
            if (record := self._records.get(checked.key_id)) is not None
            and record.revoked_at is None
            and record.hash == checked.hash
        }
        return self._records

    def _used(self, key_id: str) -> None:
        """Tell the store this key was used, at most once a minute, without waiting."""
        now = self._clock()
        if now - self._use_reported.get(key_id, -USE_REPORT_SECONDS) < USE_REPORT_SECONDS:
            return
        self._use_reported[key_id] = now
        report = getattr(self._store, "admin_action", None)
        if report is None:
            return

        async def send() -> None:
            # When a key was last used is a convenience for the owner, never a reason to
            # fail the request that used it.
            with contextlib.suppress(Exception):
                await report("key-used", {"key_id": key_id})

        task = asyncio.create_task(send())
        self._reports.add(task)
        task.add_done_callback(self._reports.discard)

    async def authenticate(self, authorization: str | None) -> Principal | None:
        if not authorization or not authorization.startswith("Bearer "):
            return None
        credential = authorization[len("Bearer ") :].strip()
        parsed = split_credential(credential)
        if parsed is None:
            return None

        fingerprint = hashlib.sha256(credential.encode("utf-8")).digest()
        key_id, secret = parsed
        records = await self.records()
        cached = self._verified.get(fingerprint)
        current = records.get(key_id)
        if (
            cached is not None
            and current is not None
            and current.revoked_at is None
            and current.hash == cached.hash
        ):
            self._used(key_id)
            return Principal(current.key_id, frozenset(current.scopes))

        # Verifying is slow enough for the records to expire and reload under
        # it, so the decision is only kept when it still stands against the
        # snapshot in force once the verification returns.
        while True:
            records = await self.records()
            generation = self._records_generation
            record = records.get(key_id)
            if record is None or record.revoked_at is not None:
                return None
            # Argon2 is deliberately expensive, so it never runs on the event loop.
            if not await anyio.to_thread.run_sync(
                verify_secret, record.hash, secret, limiter=self._verify_limiter
            ):
                return None
            if self._records_generation == generation:
                break

        principal = Principal(record.key_id, frozenset(record.scopes))
        async with self._lock:
            self._verified[fingerprint] = _Verified(record.key_id, record.hash)
        self._used(key_id)
        return principal
