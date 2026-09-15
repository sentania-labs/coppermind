"""Bearer-key authentication for every public ``/v1`` route."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass

from coppermind.api_keys import ApiKeyRecord, split_credential, verify_secret
from coppermind.store_protocol import Store

CACHE_TTL_SECONDS = 300.0


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
        self._verified: dict[bytes, _Verified] = {}
        self._lock = asyncio.Lock()

    async def authenticate(self, authorization: str | None) -> Principal | None:
        if not authorization or not authorization.startswith("Bearer "):
            return None
        credential = authorization[len("Bearer ") :].strip()
        parsed = split_credential(credential)
        if parsed is None:
            return None

        now = self._clock()
        fingerprint = hashlib.sha256(credential.encode("utf-8")).digest()
        cached = self._verified.get(fingerprint)
        if cached is not None and cached.expires_at > now:
            return cached.principal

        async with self._lock:
            now = self._clock()
            cached = self._verified.get(fingerprint)
            if cached is not None and cached.expires_at > now:
                return cached.principal
            if self._records_expire_at <= now:
                try:
                    key_set = await self._store.get_api_keys()
                except Exception as exc:  # store errors become one public 503
                    raise AuthenticationUnavailable from exc
                self._records = {record.key_id: record for record in key_set.keys}
                self._records_expire_at = now + self._ttl
                self._verified = {
                    digest: result
                    for digest, result in self._verified.items()
                    if result.expires_at > now
                }

            key_id, secret = parsed
            record = self._records.get(key_id)
            if record is None or record.revoked_at is not None:
                return None
            if not verify_secret(record.hash, secret):
                return None
            principal = Principal(record.key_id, frozenset(record.scopes))
            self._verified[fingerprint] = _Verified(principal, now + self._ttl)
            return principal
