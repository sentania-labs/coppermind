"""Read note identities from the filesystem and refresh the metadata mirror.

The scan never writes a note. Files with no known identity are deliberately
ignored until the write-side reconciliation increment.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from coppermind import frontmatter as fm
from coppermind.db.models import Note
from coppermind.db.session import transaction
from coppermind.logging import get_logger
from coppermind.store_protocol import MetadataUnavailable, NotesFilesystemUnavailable
from coppermind_store.fs import content_hash, resolve
from coppermind_store.notes import _jsonable, _mirror_columns, _title_of

if TYPE_CHECKING:
    from coppermind.schema import FrontmatterSchema
    from coppermind_store.notes import LocalStore

log = get_logger("coppermind-store")

_IGNORED_DIRECTORIES = {".git", ".obsidian", ".trash"}


@dataclass(frozen=True)
class Observation:
    note_id: str
    path: str
    state: str
    content_hash: str
    size_bytes: int
    mtime: datetime
    title: str | None = None
    frontmatter: dict[str, Any] | None = None


async def run_reconciler(store: LocalStore) -> None:
    """Run scans on the configured interval without joining a request path."""
    while True:
        try:
            interval = store.control.settings().reconcile.scan_interval_s
        except Exception as exc:  # noqa: BLE001 - readiness reports the control-file fault
            log.warning("reconciliation settings unavailable", error_type=type(exc).__name__)
            interval = 60
        await asyncio.sleep(interval)
        started = monotonic()
        try:
            counts = await reconcile_once(store)
        except (MetadataUnavailable, NotesFilesystemUnavailable, OSError) as exc:
            log.warning("reconciliation deferred", reason=type(exc).__name__)
        except Exception as exc:  # noqa: BLE001 - a background fault must not stop the service
            log.exception("reconciliation failed", error_type=type(exc).__name__)
        else:
            log.info(
                "reconciliation completed",
                duration_ms=round((monotonic() - started) * 1000),
                **counts,
            )


async def reconcile_once(store: LocalStore) -> dict[str, int]:
    """Make known mirror rows describe files carrying the same identity."""
    scan_started = datetime.now(tz=UTC)
    try:
        async with store.session_factory() as session:
            rows = list((await session.scalars(sa.select(Note))).all())
    except (SQLAlchemyError, OSError) as exc:
        raise MetadataUnavailable(str(exc)) from exc

    known_paths = {row.id: row.path for row in rows}
    schema = store.control.schema()
    found = await asyncio.to_thread(_scan, store.notes_root, schema, set(known_paths))
    observations = _choose_observations(found, known_paths)

    counts = {"changed": 0, "moved": 0, "missing": 0, "unparsed": 0}
    now = datetime.now(tz=UTC)
    try:
        async with transaction(store.session_factory) as session:
            for row in rows:
                observed = observations.get(row.id)
                values = _values_for(row, observed, schema)
                if not values:
                    continue
                values["updated_at"] = now
                result = await session.execute(
                    sa.update(Note)
                    .where(Note.id == row.id, Note.updated_at <= scan_started)
                    .values(**values)
                    .returning(Note.id)
                )
                if result.scalar_one_or_none() is None:
                    continue
                if observed is None:
                    counts["missing"] += 1
                elif observed.state == "unparsed":
                    counts["unparsed"] += 1
                elif row.path != observed.path:
                    counts["moved"] += 1
                else:
                    counts["changed"] += 1
    except (SQLAlchemyError, OSError) as exc:
        raise MetadataUnavailable(str(exc)) from exc
    return counts


def _scan(
    root: Path, schema: FrontmatterSchema, known_ids: set[str]
) -> dict[str, list[Observation]]:
    observations: dict[str, list[Observation]] = {}
    try:
        paths = root.rglob("*.md")
        for path in paths:
            relative_path = path.relative_to(root)
            if any(part in _IGNORED_DIRECTORIES for part in relative_path.parts):
                continue
            relative = relative_path.as_posix()
            try:
                safe_path = resolve(root, relative)
                data = safe_path.read_bytes()
                stat = safe_path.stat()
            except FileNotFoundError:
                continue
            except ValueError:
                continue
            text: str | None = None
            try:
                text = data.decode("utf-8")
                frontmatter, body = fm.parse(text)
            except (UnicodeDecodeError, fm.FrontmatterError):
                note_id = _identity_from_broken(text, schema)
                if note_id not in known_ids:
                    continue
                observation = Observation(
                    note_id=note_id,
                    path=relative,
                    state="unparsed",
                    content_hash=content_hash(data),
                    size_bytes=len(data),
                    mtime=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                )
            else:
                note_id = str(frontmatter.get(schema.role("id_key"), ""))
                if note_id not in known_ids:
                    continue
                observation = Observation(
                    note_id=note_id,
                    path=relative,
                    state="ok",
                    content_hash=content_hash(data),
                    size_bytes=len(data),
                    mtime=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                    title=_title_of(body, safe_path),
                    frontmatter=_jsonable(frontmatter),
                )
            observations.setdefault(note_id, []).append(observation)
    except OSError as exc:
        raise NotesFilesystemUnavailable(str(exc)) from exc
    return observations


def _identity_from_broken(text: str | None, schema: FrontmatterSchema) -> str | None:
    """Recover one explicit top-level identity without trusting a broken path."""
    if text is None or not text.startswith("---"):
        return None
    lines = text.splitlines()
    closing = next((index for index, line in enumerate(lines[1:], 1) if line == "---"), None)
    block = "\n".join(lines[1:closing]) if closing is not None else "\n".join(lines[1:])
    key = re.escape(schema.role("id_key"))
    matches = re.findall(rf"(?m)^{key}:[ \t]*['\"]?([^'\" \t\r\n]+)['\"]?[ \t]*$", block)
    return matches[0] if len(matches) == 1 else None


def _choose_observations(
    found: dict[str, list[Observation]], known_paths: dict[str, str]
) -> dict[str, Observation]:
    chosen: dict[str, Observation] = {}
    for note_id, candidates in found.items():
        at_known_path = [item for item in candidates if item.path == known_paths[note_id]]
        if len(at_known_path) == 1:
            chosen[note_id] = at_known_path[0]
        elif len(candidates) == 1:
            chosen[note_id] = candidates[0]
        else:
            log.warning("duplicate note identity left unresolved", note_id=note_id)
    return chosen


def _values_for(
    row: Note, observed: Observation | None, schema: FrontmatterSchema
) -> dict[str, Any]:
    if observed is None:
        if row.state == "missing" and row.state_reason == "not observed during reconciliation":
            return {}
        return {"state": "missing", "state_reason": "not observed during reconciliation"}
    if observed.state == "unparsed":
        values = {
            "path": observed.path,
            "mtime": observed.mtime,
            "size_bytes": observed.size_bytes,
            "state": "unparsed",
            "state_reason": "frontmatter could not be parsed",
        }
    else:
        assert observed.frontmatter is not None
        values = {
            "path": observed.path,
            "title": observed.title,
            "content_hash": observed.content_hash,
            "size_bytes": observed.size_bytes,
            "mtime": observed.mtime,
            "frontmatter": observed.frontmatter,
            **_mirror_columns(observed.frontmatter, schema),
            "state": "ok",
            "state_reason": None,
        }
    return {key: value for key, value in values.items() if getattr(row, key) != value}
