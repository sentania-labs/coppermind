"""Idempotent source ingestion with immutable revisions.

A deterministic filesystem claim owns each external identifier. Artifact
files are exclusive and each revision is recorded in `manifest.json` only
after its files are durable. The linked Review note is created once and is
never part of a replay or revision write. A write-phase failure removes
incomplete files; a commit failure retains complete files so the next retry
can resolve through the claim and repair the database mirror.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from coppermind import frontmatter as fm
from coppermind.atomicio import atomic_write_bytes, create_exclusive_bytes
from coppermind.db.models import Note, NoteSource, Source, SourceArtifact, SourceRevision
from coppermind.db.session import transaction
from coppermind.ids import is_valid_id, new_id
from coppermind.naming import sanitize_folder, unique_stem
from coppermind.schema import FrontmatterSchema
from coppermind.settings import ProductSettings
from coppermind.store_protocol import (
    CreatedNote,
    CreatedSource,
    CreateNote,
    DescriptiveCorrectionUnsupported,
    IngestArtifact,
    IngestRequest,
    IngestResult,
    NotesFilesystemUnavailable,
    PathCollision,
    PayloadTooLarge,
    SourceClaimMissing,
    SourcesFilesystemUnavailable,
    StoreError,
    ValidationFailed,
)
from coppermind_store.fs import NOTE_SUFFIX, content_hash, existing_stems, resolve
from coppermind_store.notes import (
    _body_with_heading,
    _build_frontmatter,
    _jsonable,
    _metadata_failure,
    _mirror_columns,
    _stem_for,
    _title_of,
)

if TYPE_CHECKING:
    from coppermind_store.notes import LocalStore


async def ingest(
    store: LocalStore,
    request: IngestRequest,
    *,
    payload_size_bytes: int | None = None,
) -> IngestResult:
    settings = store.control.settings()
    schema = store.control.schema()
    if max(_payload_size(request), payload_size_bytes or 0) > settings.limits.ingest_max_bytes:
        raise PayloadTooLarge(settings.limits.ingest_max_bytes)

    artifacts = [(artifact, artifact.bytes()) for artifact in request.source.artifacts]
    artifact_metadata = [
        {
            "name": artifact.name,
            "mime_type": artifact.mime_type,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
        }
        for artifact, data in artifacts
    ]
    identity = _content_identity(artifact_metadata)
    claim_path = _external_id_claim_path(
        store.sources_root,
        request.source.provider,
        request.source.external_source_id,
    )
    try:
        async with (
            store._source_lock_for(claim_path.name),
            transaction(store.session_factory) as session,
        ):
            await session.execute(sa.text("SELECT 1"))
            try:
                claim_data = claim_path.read_bytes()
            except FileNotFoundError:
                result = await _ingest_new(
                    store,
                    session,
                    request,
                    artifacts,
                    artifact_metadata,
                    identity,
                    claim_path,
                    schema,
                    settings,
                )
            except OSError as exc:
                raise SourcesFilesystemUnavailable(str(exc)) from exc
            else:
                result = await _ingest_existing(
                    store,
                    session,
                    request,
                    artifacts,
                    artifact_metadata,
                    identity,
                    claim_data,
                    schema,
                )
    except BaseException as exc:
        typed = _metadata_failure(exc)
        if typed is not None:
            raise typed from exc
        raise
    return result


async def _ingest_new(
    store: LocalStore,
    session: AsyncSession,
    request: IngestRequest,
    artifacts: list[tuple[IngestArtifact, bytes]],
    artifact_metadata: list[dict[str, Any]],
    identity: str,
    claim_path: Path,
    schema: FrontmatterSchema,
    settings: ProductSettings,
) -> IngestResult:
    source_id = new_id()
    note_id = new_id()
    now = datetime.now(tz=UTC)
    note_request = CreateNote(
        title=request.note.title,
        body=request.note.body,
        frontmatter={**request.note.frontmatter, schema.role("sources_key"): [source_id]},
    )
    frontmatter = _build_frontmatter(note_request, schema, settings, note_id)
    problems = schema.validate_frontmatter(frontmatter)
    if problems:
        raise ValidationFailed(problems)

    folder = sanitize_folder(settings.notes.review_folder)
    folder_path = resolve(store.notes_root, folder)
    stem = unique_stem(
        _stem_for(note_request.title, frontmatter, schema, settings),
        existing_stems(folder_path),
    )
    relative = f"{folder}/{stem}{NOTE_SUFFIX}" if folder else f"{stem}{NOTE_SUFFIX}"
    note_path = folder_path / f"{stem}{NOTE_SUFFIX}"
    body = _body_with_heading(note_request.title, note_request.body)
    note_data = fm.compose(frontmatter, body).encode("utf-8")
    note_digest = content_hash(note_data)
    source_path = store.sources_root / source_id
    revision_path = source_path / "r0001"

    claim_created = False
    filesystem_complete = False
    try:
        create_exclusive_bytes(claim_path, _external_id_claim(request, source_id))
        claim_created = True
        session.add_all(
            [
                Source(
                    id=source_id,
                    provider=request.source.provider,
                    external_source_id=request.source.external_source_id,
                    source_type=request.source.source_type,
                    origin=request.source.origin,
                    current_revision=1,
                    content_identity=identity,
                    created_at=now,
                    updated_at=now,
                ),
                SourceRevision(
                    source_id=source_id,
                    revision=1,
                    content_identity=identity,
                    ingested_at=now,
                    captured_at=request.source.captured_at,
                    metadata_json=_jsonable(request.source.metadata),
                ),
                Note(
                    id=note_id,
                    path=relative,
                    title=note_request.title,
                    content_hash=note_digest,
                    size_bytes=len(note_data),
                    mtime=now,
                    frontmatter=_jsonable(frontmatter),
                    **_mirror_columns(frontmatter, schema),
                    state="ok",
                    first_seen_at=now,
                    updated_at=now,
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                NoteSource(note_id=note_id, source_id=source_id, created_at=now),
                *_artifact_rows(source_id, 1, artifact_metadata),
            ]
        )
        await session.flush()

        revision_path.mkdir(parents=True, exist_ok=False)
        for artifact, data in artifacts:
            create_exclusive_bytes(revision_path / artifact.name, data)
        create_exclusive_bytes(
            source_path / "manifest.json",
            _manifest(request, source_id, now, identity, artifact_metadata),
        )
        try:
            create_exclusive_bytes(note_path, note_data)
        except FileExistsError as exc:
            raise PathCollision(relative) from exc
        except OSError as exc:
            raise NotesFilesystemUnavailable(str(exc)) from exc
        filesystem_complete = True
    except IntegrityError as exc:
        constraint = _violated_constraint(exc)
        if constraint == "uq_sources_provider_external_id":
            raise SourceClaimMissing(
                request.source.provider, request.source.external_source_id
            ) from exc
        if constraint == "uq_notes_path":
            raise PathCollision(relative) from exc
        raise
    except OSError as exc:
        raise SourcesFilesystemUnavailable(str(exc)) from exc
    finally:
        if claim_created and not filesystem_complete:
            shutil.rmtree(source_path, ignore_errors=True)
            try:
                claim_path.unlink(missing_ok=True)
            except OSError as exc:
                raise SourcesFilesystemUnavailable(str(exc)) from exc
    return IngestResult(
        source=CreatedSource(id=source_id, revision=1, created=True),
        note=CreatedNote(id=note_id, path=relative, created=True),
    )


async def _ingest_existing(
    store: LocalStore,
    session: AsyncSession,
    request: IngestRequest,
    artifacts: list[tuple[IngestArtifact, bytes]],
    artifact_metadata: list[dict[str, Any]],
    identity: str,
    claim_data: bytes,
    schema: FrontmatterSchema,
) -> IngestResult:
    claim = _claimed_source(claim_data, request)
    source_id = claim["source_id"]
    source_path = store.sources_root / source_id
    manifest_path = source_path / "manifest.json"
    manifest = _read_json(manifest_path, "source manifest")
    if (
        manifest.get("source_id") != source_id
        or manifest.get("provider") != request.source.provider
        or manifest.get("external_source_id") != request.source.external_source_id
    ):
        raise StoreError("the external-id claim and source manifest disagree")

    current_revision = _positive_int(manifest.get("current_revision"), "current_revision")
    current = _manifest_revision(manifest, current_revision)
    replaying = current.get("content_identity") == identity
    if replaying:
        differing = _descriptive_differences(request, manifest, current)
        if differing:
            raise DescriptiveCorrectionUnsupported(differing)

    note_id, note_path, note_snapshot = await _linked_note(store, session, source_id, schema)
    await _ensure_mirror(session, manifest, note_id, note_path, note_snapshot, schema)

    if replaying:
        return IngestResult(
            source=CreatedSource(id=source_id, revision=current_revision, created=False),
            note=CreatedNote(id=note_id, path=note_path, created=False),
        )

    revision = current_revision + 1
    now = datetime.now(tz=UTC)
    revision_path = source_path / f"r{revision:04d}"
    manifest_written = False
    try:
        revision_path.mkdir(parents=False, exist_ok=False)
        for artifact, data in artifacts:
            create_exclusive_bytes(revision_path / artifact.name, data)
        revisions = list(manifest.get("revisions", []))
        revisions.append(_revision_document(request, revision, now, identity, artifact_metadata))
        manifest.update(
            {
                "source_type": request.source.source_type,
                "origin": request.source.origin,
                "current_revision": revision,
                "revisions": revisions,
            }
        )
        atomic_write_bytes(manifest_path, _json_bytes(manifest))
        manifest_written = True
    except OSError as exc:
        if not manifest_written:
            shutil.rmtree(revision_path, ignore_errors=True)
        raise SourcesFilesystemUnavailable(str(exc)) from exc

    source = await session.get(Source, source_id)
    if source is None:
        raise StoreError("the source mirror could not be rebuilt")
    source.source_type = request.source.source_type
    source.origin = request.source.origin
    source.current_revision = revision
    source.content_identity = identity
    source.updated_at = now
    session.add(
        SourceRevision(
            source_id=source_id,
            revision=revision,
            content_identity=identity,
            ingested_at=now,
            captured_at=request.source.captured_at,
            metadata_json=_jsonable(request.source.metadata),
        )
    )
    await session.flush()
    session.add_all(_artifact_rows(source_id, revision, artifact_metadata))
    await session.flush()
    return IngestResult(
        source=CreatedSource(id=source_id, revision=revision, created=True),
        note=CreatedNote(id=note_id, path=note_path, created=False),
    )


def _descriptive_differences(
    request: IngestRequest, manifest: dict[str, Any], current: dict[str, Any]
) -> list[str]:
    """The fields describing a source that differ from what is stored.

    A source is identified by its artifact bytes, so these fields cannot make
    a revision of their own. Naming the ones that differ is what turns a
    correction the store cannot keep into a refusal rather than a silent
    discard.
    """
    differing = []
    if not _same_instant(request.source.captured_at, current.get("captured_at")):
        differing.append("captured_at")
    if _jsonable(request.source.metadata) != (current.get("metadata") or {}):
        differing.append("metadata")
    if request.source.source_type != manifest.get("source_type"):
        differing.append("source_type")
    if request.source.origin != manifest.get("origin"):
        differing.append("origin")
    return differing


def _same_instant(value: datetime | None, stored: Any) -> bool:
    if value is None or stored is None:
        return value is None and stored is None
    try:
        return datetime.fromisoformat(stored) == value
    except (TypeError, ValueError):
        return False


def _claimed_source(claim_data: bytes, request: IngestRequest) -> dict[str, Any]:
    try:
        claim = json.loads(claim_data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourcesFilesystemUnavailable("the external-id claim is unreadable") from exc
    if (
        claim.get("schema_version") != 1
        or claim.get("provider") != request.source.provider
        or claim.get("external_source_id") != request.source.external_source_id
        or not is_valid_id(claim.get("source_id"))
    ):
        raise SourcesFilesystemUnavailable("the external-id claim is invalid")
    return claim


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourcesFilesystemUnavailable(f"the {label} is unreadable") from exc
    if not isinstance(document, dict):
        raise SourcesFilesystemUnavailable(f"the {label} is invalid")
    return document


async def _linked_note(
    store: LocalStore, session: AsyncSession, source_id: str, schema: FrontmatterSchema
) -> tuple[str, str, tuple[bytes, dict[str, Any], str, datetime] | None]:
    row = (
        await session.execute(
            sa.select(Note.id, Note.path)
            .join(NoteSource, NoteSource.note_id == Note.id)
            .where(NoteSource.source_id == source_id)
        )
    ).one_or_none()
    if row is not None:
        return row.id, row.path, None

    matches: list[tuple[str, str, tuple[bytes, dict[str, Any], str, datetime]]] = []
    try:
        paths = list(store.notes_root.rglob(f"*{NOTE_SUFFIX}"))
    except OSError as exc:
        raise NotesFilesystemUnavailable(str(exc)) from exc
    for path in paths:
        try:
            data = path.read_bytes()
            parsed, body = fm.parse(data.decode("utf-8"))
            stat = path.stat()
        except (OSError, UnicodeDecodeError, fm.FrontmatterError):
            continue
        sources = parsed.get(schema.role("sources_key"), [])
        note_id = parsed.get(schema.role("id_key"))
        if source_id in sources and is_valid_id(note_id):
            note_id = str(note_id)
            relative = path.relative_to(store.notes_root).as_posix()
            matches.append(
                (
                    note_id,
                    relative,
                    (
                        data,
                        parsed,
                        body,
                        datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                    ),
                )
            )
    if len(matches) != 1:
        raise StoreError("the durable source claim does not resolve to exactly one note")
    return matches[0]


async def _ensure_mirror(
    session: AsyncSession,
    manifest: dict[str, Any],
    note_id: str,
    note_path: str,
    note_snapshot: tuple[bytes, dict[str, Any], str, datetime] | None,
    schema: FrontmatterSchema,
) -> None:
    source_id = manifest.get("source_id")
    revisions = manifest.get("revisions")
    if not is_valid_id(source_id) or not isinstance(revisions, list) or not revisions:
        raise SourcesFilesystemUnavailable("the source manifest is invalid")
    source_id = str(source_id)
    current_revision = _positive_int(manifest.get("current_revision"), "current_revision")
    current = _manifest_revision(manifest, current_revision)
    created_at = _manifest_time(revisions[0].get("ingested_at"), "ingested_at")
    updated_at = _manifest_time(current.get("ingested_at"), "ingested_at")

    source = await session.get(Source, source_id)
    if source is None:
        conflicting = await session.scalar(
            sa.select(Source.id).where(
                Source.provider == manifest.get("provider"),
                Source.external_source_id == manifest.get("external_source_id"),
            )
        )
        if conflicting is not None:
            raise StoreError("the source mirror conflicts with the durable external-id claim")
        source = Source(
            id=source_id,
            provider=str(manifest.get("provider", "")),
            external_source_id=str(manifest.get("external_source_id", "")),
            source_type=str(manifest.get("source_type", "")),
            origin=str(manifest.get("origin", "")),
            current_revision=current_revision,
            content_identity=str(current.get("content_identity", "")),
            created_at=created_at,
            updated_at=updated_at,
        )
        session.add(source)
        await session.flush()
    else:
        source.source_type = str(manifest.get("source_type", ""))
        source.origin = str(manifest.get("origin", ""))
        source.current_revision = current_revision
        source.content_identity = str(current.get("content_identity", ""))
        source.updated_at = updated_at

    for item in revisions:
        revision = _positive_int(item.get("revision"), "revision")
        if await session.get(SourceRevision, (source_id, revision)) is None:
            session.add(
                SourceRevision(
                    source_id=source_id,
                    revision=revision,
                    content_identity=str(item.get("content_identity", "")),
                    ingested_at=_manifest_time(item.get("ingested_at"), "ingested_at"),
                    captured_at=(
                        _manifest_time(item["captured_at"], "captured_at")
                        if item.get("captured_at")
                        else None
                    ),
                    metadata_json=item.get("metadata", {}),
                )
            )
            await session.flush()
            session.add_all(_artifact_rows(source_id, revision, item.get("artifacts", [])))
    await session.flush()

    if await session.get(Note, note_id) is None:
        if note_snapshot is None:
            raise StoreError("the linked note mirror is incomplete")
        data, frontmatter, body, mtime = note_snapshot
        session.add(
            Note(
                id=note_id,
                path=note_path,
                title=_title_of(body, Path(note_path)),
                content_hash=content_hash(data),
                size_bytes=len(data),
                mtime=mtime,
                frontmatter=_jsonable(frontmatter),
                **_mirror_columns(frontmatter, schema),
                state="ok",
                first_seen_at=mtime,
                updated_at=mtime,
            )
        )
        await session.flush()
    if await session.get(NoteSource, (note_id, source_id)) is None:
        session.add(NoteSource(note_id=note_id, source_id=source_id, created_at=created_at))
        await session.flush()


def _artifact_rows(
    source_id: str, revision: int, artifacts: list[dict[str, Any]]
) -> list[SourceArtifact]:
    return [
        SourceArtifact(
            source_id=source_id,
            revision=revision,
            name=str(item["name"]),
            mime_type=str(item["mime_type"]),
            sha256=str(item["sha256"]),
            size_bytes=int(item["size_bytes"]),
        )
        for item in artifacts
    ]


def _positive_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise SourcesFilesystemUnavailable(f"the source manifest has an invalid {field}")
    return value


def _manifest_revision(manifest: dict[str, Any], revision: int) -> dict[str, Any]:
    revisions = manifest.get("revisions", [])
    matches = [item for item in revisions if item.get("revision") == revision]
    if len(matches) != 1:
        raise SourcesFilesystemUnavailable("the source manifest has inconsistent revisions")
    return matches[0]


def _manifest_time(value: Any, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise SourcesFilesystemUnavailable(f"the source manifest has an invalid {field}") from exc
    if parsed.tzinfo is None:
        raise SourcesFilesystemUnavailable(f"the source manifest has an invalid {field}")
    return parsed


def _violated_constraint(exc: IntegrityError) -> str:
    """The constraint a unique violation names, as asyncpg reported it.

    SQLAlchemy's asyncpg adapter raises its own DBAPI error carrying only the
    SQLSTATE and chains the asyncpg exception, which is the object that knows
    the constraint, as its cause.
    """
    return getattr(getattr(exc.orig, "__cause__", None), "constraint_name", "") or ""


def _payload_size(request: IngestRequest) -> int:
    """Bytes in the compact JSON request sent over the store contract."""
    return len(request.model_dump_json(exclude_none=True).encode("utf-8"))


def _external_id_claim_path(root: Path, provider: str, external_source_id: str) -> Path:
    """The stable claim path for a provider and its external identifier."""
    key = json.dumps(
        [provider, external_source_id], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    digest = hashlib.sha256(key).hexdigest()
    return root / f".external-id-{digest}.json"


def _external_id_claim(request: IngestRequest, source_id: str) -> bytes:
    document = {
        "schema_version": 1,
        "provider": request.source.provider,
        "external_source_id": request.source.external_source_id,
        "source_id": source_id,
    }
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _content_identity(artifacts: list[dict[str, Any]]) -> str:
    names_and_hashes = sorted(f"{item['name']}:{item['sha256']}" for item in artifacts)
    return hashlib.sha256("\n".join(names_and_hashes).encode("utf-8")).hexdigest()


def _manifest(
    request: IngestRequest,
    source_id: str,
    ingested_at: datetime,
    identity: str,
    artifacts: list[dict[str, Any]],
) -> bytes:
    document = {
        "schema_version": 1,
        "source_id": source_id,
        "provider": request.source.provider,
        "external_source_id": request.source.external_source_id,
        "source_type": request.source.source_type,
        "origin": request.source.origin,
        "current_revision": 1,
        "revisions": [_revision_document(request, 1, ingested_at, identity, artifacts)],
    }
    return _json_bytes(document)


def _revision_document(
    request: IngestRequest,
    revision: int,
    ingested_at: datetime,
    identity: str,
    artifacts: list[dict[str, Any]],
) -> dict[str, Any]:
    captured = request.source.captured_at
    return {
        "revision": revision,
        "ingested_at": ingested_at.isoformat(),
        "captured_at": captured.isoformat() if captured else None,
        "content_identity": identity,
        "metadata": _jsonable(request.source.metadata),
        "artifacts": artifacts,
    }


def _json_bytes(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
