"""Which preserved source copies cannot be read, kept as a small file for Problems.

A bundle with files but no readable manifest, or one a rebuild could not verify, is
listed as a problem. The list is written by the startup scan and by the rebuild, and
read by Problems and Home; reading it never walks a bundle.
"""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from coppermind.atomicio import atomic_write_bytes
from coppermind.db.models import Source
from coppermind.ids import is_valid_id
from coppermind.logging import get_logger
from coppermind.store_protocol import SourcesFilesystemUnavailable
from coppermind_store.sources import _manifest_document

if TYPE_CHECKING:
    from coppermind_store.notes import LocalStore

log = get_logger("coppermind-store")

_LOCK = threading.Lock()


def _path(store: LocalStore) -> Path:
    return store.control.store.state_dir / "damaged-sources.json"


def bundle_state(bundle: Path) -> tuple[str, dict[str, Any]]:
    """`empty` holds no files, `ok` has a readable manifest, `damaged` is anything else."""
    if not any(not p.is_dir() for p in bundle.rglob("*")):
        return "empty", {}
    try:
        return "ok", _manifest_document(bundle / "manifest.json")
    except (SourcesFilesystemUnavailable, OSError):
        return "damaged", {}


def damaged_bundles(store: LocalStore, source_ids: list[str]) -> list[str]:
    return [
        source_id
        for source_id in source_ids
        if is_valid_id(source_id) and bundle_state(store.sources_root / source_id)[0] == "damaged"
    ]


def recorded(store: LocalStore) -> list[dict[str, str]]:
    """The damaged bundles the last scan or rebuild recorded."""
    try:
        value = json.loads(_path(store).read_bytes())
    except (OSError, ValueError):
        return []
    found = value.get("damaged", []) if isinstance(value, dict) else []
    return [entry for entry in found if isinstance(entry, dict) and "source_id" in entry]


def _change(store: LocalStore, change) -> None:
    with _LOCK, suppress(OSError):
        found = recorded(store)
        updated = change(found)
        if updated != found:
            atomic_write_bytes(_path(store), json.dumps({"damaged": updated}).encode(), mode=0o600)


def note_damaged(
    store: LocalStore,
    source_id: str,
    provider: str = "",
    external_source_id: str = "",
    cause: str = "manifest",
) -> None:
    """Record what found a bundle damaged: `manifest` (a scan) or `recovery` (a rebuild)."""
    entry = {
        "source_id": source_id,
        "provider": provider,
        "external_source_id": external_source_id,
        "cause": cause,
    }

    def change(found: list[dict[str, str]]) -> list[dict[str, str]]:
        others = [
            e for e in found if (e["source_id"], e.get("cause", "manifest")) != (source_id, cause)
        ]
        return [*others, entry]

    _change(store, change)


def note_repaired(store: LocalStore, source_id: str, cause: str | None = None) -> None:
    """Forget what was recorded for a bundle once that condition is verified repaired."""

    def change(found: list[dict[str, str]]) -> list[dict[str, str]]:
        return [
            e
            for e in found
            if e["source_id"] != source_id or (cause and e.get("cause", "manifest") != cause)
        ]

    _change(store, change)


async def scan(store: LocalStore) -> None:
    """Walk every bundle once, at start, and record the damaged ones."""
    async with store.session_factory() as session:
        rows = (
            await session.execute(
                sa.select(Source.id, Source.provider, Source.external_source_id).order_by(Source.id)
            )
        ).all()
    named = {row.id: row for row in rows}
    damaged = await asyncio.to_thread(damaged_bundles, store, list(named))
    verification = [
        e
        for e in recorded(store)
        if e.get("cause") == "recovery" and (store.sources_root / e["source_id"]).exists()
    ]
    found = [
        {
            "source_id": source_id,
            "provider": named[source_id].provider,
            "external_source_id": named[source_id].external_source_id,
            "cause": "manifest",
        }
        for source_id in damaged
    ] + verification

    def write() -> None:
        with _LOCK:
            atomic_write_bytes(_path(store), json.dumps({"damaged": found}).encode(), mode=0o600)

    await asyncio.to_thread(write)
