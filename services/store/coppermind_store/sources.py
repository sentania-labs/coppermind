"""Idempotent source ingestion with immutable revisions.

A deterministic filesystem claim owns each external identifier. Artifact
files are exclusive and each revision is recorded in `manifest.json` only
after its files are durable. The linked Review note is created once and is
never part of a replay or revision write. A write-phase failure removes what
that attempt created, unless replacing `manifest.json` has begun and the new
revision may already be live; a commit failure retains complete files so the
next retry can resolve through the claim and repair the database mirror.
Either of those, or a process killed mid-write, can leave a revision directory
the manifest does not record, which the next ingest names and leaves alone
rather than deleting.
"""

from __future__ import annotations

import asyncio
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
    ArtifactNotFound,
    CreatedNote,
    CreatedSource,
    CreateNote,
    IncompleteRevision,
    IngestArtifact,
    IngestRequest,
    IngestResult,
    NotesFilesystemUnavailable,
    PathCollision,
    PayloadTooLarge,
    ProjectionNotPlaced,
    SourceArtifactDocument,
    SourceClaimMissing,
    SourceManifest,
    SourceNotFound,
    SourcesFilesystemUnavailable,
    StoreError,
    ValidationFailed,
    artifact_text,
)
from coppermind_store.fs import NOTE_SUFFIX, content_hash, existing_stems, resolve
from coppermind_store.notes import (
    _as_date,
    _body_with_heading,
    _build_frontmatter,
    _jsonable,
    _metadata_failure,
    _mirror_columns,
    _stem_for,
    _title_of,
    _today,
)
from coppermind_store.projections import (
    new_projection_path,
    projection_revision,
    write_projection,
)

if TYPE_CHECKING:
    from coppermind_store.notes import LocalStore


async def get_source(store: LocalStore, source_id: str) -> SourceManifest:
    """Read the filesystem manifest by immutable source identity."""
    if not is_valid_id(source_id):
        raise SourceNotFound(source_id)
    path = store.sources_root / source_id / "manifest.json"
    try:
        document = await asyncio.to_thread(_manifest_document, path)
    except FileNotFoundError as exc:
        raise SourceNotFound(source_id) from exc
    except OSError as exc:
        raise SourcesFilesystemUnavailable("the source manifest is unreadable") from exc
    try:
        manifest = SourceManifest.model_validate(document)
    except ValueError as exc:
        raise SourcesFilesystemUnavailable("the source manifest is invalid") from exc
    if manifest.source_id != source_id:
        raise SourcesFilesystemUnavailable("the source manifest names a different source")
    return manifest


async def get_source_artifact(
    store: LocalStore, source_id: str, revision: int, name: str
) -> SourceArtifactDocument:
    manifest = await get_source(store, source_id)
    found_revision = next((item for item in manifest.revisions if item.revision == revision), None)
    artifact = (
        next((item for item in found_revision.artifacts if item.name == name), None)
        if found_revision is not None
        else None
    )
    if artifact is None:
        raise ArtifactNotFound(source_id, revision, name)
    try:
        path = resolve(store.sources_root / source_id / f"r{revision:04d}", name)
        data = await asyncio.to_thread(
            _verified_artifact_bytes, path, artifact.size_bytes, artifact.sha256
        )
    except (OSError, ValueError) as exc:
        raise SourcesFilesystemUnavailable(f"the source artifact is unreadable: {name}") from exc
    return SourceArtifactDocument(
        **artifact.model_dump(),
        source_id=source_id,
        revision=revision,
        content=artifact_text(data, artifact.mime_type),
    )


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
                    settings,
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

    # The same live-row check `create_note` makes. Historical paths are not
    # unique, so this asks whether any live row holds the path rather than
    # leaving a dropped constraint to answer.
    occupied = (
        (
            await session.execute(
                sa.select(Note.id).where(Note.path == relative, Note.state != "missing").limit(1)
            )
        )
        .scalars()
        .first()
    )
    if occupied is not None:
        raise PathCollision(relative)

    claim_created = False
    filesystem_complete = False
    projection_path = ""
    projection_created = False
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
        projection_path = new_projection_path(
            store.notes_root,
            settings,
            provider=request.source.provider,
            title=note_request.title,
            note_date=_as_date(frontmatter.get(schema.role("date_key"))) or _today(settings),
        )
        try:
            projection_created = await asyncio.to_thread(
                write_projection,
                store.notes_root,
                settings,
                source_id=source_id,
                revision=1,
                title=note_request.title,
                revision_ingested_at=now,
                artifacts=_projection_artifacts(artifacts, artifact_metadata),
                relative_path=projection_path,
            )
        except ProjectionNotPlaced as exc:
            raise PathCollision(projection_path) from exc
        create_exclusive_bytes(
            source_path / "manifest.json",
            _manifest(
                request,
                source_id,
                now,
                identity,
                artifact_metadata,
                projection_path,
            ),
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
        raise
    except OSError as exc:
        raise SourcesFilesystemUnavailable(str(exc)) from exc
    finally:
        if claim_created and not filesystem_complete:
            shutil.rmtree(source_path, ignore_errors=True)
            # The claim goes first. It is the durable record that decides every
            # later ingest of this external id, so a notes filesystem fault
            # while removing the projection must not strand it behind a source
            # directory that is already gone.
            try:
                claim_path.unlink(missing_ok=True)
            except OSError as exc:
                raise SourcesFilesystemUnavailable(str(exc)) from exc
            finally:
                if projection_created and projection_path:
                    try:
                        resolve(store.notes_root, projection_path).unlink(missing_ok=True)
                    except OSError as exc:
                        raise NotesFilesystemUnavailable(str(exc)) from exc
    return IngestResult(
        source=CreatedSource(id=source_id, revision=1, created=True),
        note=CreatedNote(id=note_id, path=relative, created=True),
        projection_path=projection_path,
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
    settings: ProductSettings,
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
    current_artifacts = _manifest_artifacts(current)
    replaying = _content_identity(current_artifacts) == identity
    if replaying:
        await asyncio.to_thread(
            _verify_revision_artifacts,
            source_path,
            current_revision,
            current_artifacts,
        )

    note_id, note_path, note_snapshot = await _linked_note(store, session, source_id, schema)
    await _ensure_mirror(session, manifest, note_id, note_path, note_snapshot, schema)

    if replaying:
        recorded_path = manifest.get("projection_path")
        projection_path = recorded_path if isinstance(recorded_path, str) and recorded_path else ""
        held_revision = await asyncio.to_thread(
            projection_revision, store.notes_root, projection_path, source_id
        )
        projection_created = False
        if held_revision is None:
            projection_path = ""
        if held_revision != current_revision:
            note = await session.get(Note, note_id)
            if note is None:
                raise StoreError("the linked note mirror is incomplete") from None
            projection_path = projection_path or new_projection_path(
                store.notes_root,
                settings,
                provider=str(manifest["provider"]),
                title=note.title,
                note_date=note.date or _today(settings),
            )
            projection_created = await asyncio.to_thread(
                write_projection,
                store.notes_root,
                settings,
                source_id=source_id,
                revision=current_revision,
                title=note.title,
                revision_ingested_at=_manifest_time(current.get("ingested_at"), "ingested_at"),
                artifacts=await asyncio.to_thread(
                    _read_revision_artifacts, source_path, current_revision, current_artifacts
                ),
                relative_path=projection_path,
            )
        if manifest.get("projection_path") != projection_path:
            manifest["projection_path"] = projection_path
            try:
                atomic_write_bytes(manifest_path, _json_bytes(manifest))
            except OSError as exc:
                if projection_created:
                    resolve(store.notes_root, projection_path).unlink(missing_ok=True)
                raise SourcesFilesystemUnavailable(str(exc)) from exc
        return IngestResult(
            source=CreatedSource(
                id=source_id,
                revision=current_revision,
                created=False,
                unstored_fields=_descriptive_differences(
                    request, manifest, current, artifact_metadata
                ),
            ),
            note=CreatedNote(id=note_id, path=note_path, created=False),
            projection_path=projection_path,
        )

    revision = current_revision + 1
    now = datetime.now(tz=UTC)
    revision_path = source_path / f"r{revision:04d}"
    try:
        revision_path.mkdir(parents=False, exist_ok=False)
    except FileExistsError as exc:
        raise IncompleteRevision(f"{source_id}/{revision_path.name}") from exc
    except OSError as exc:
        raise SourcesFilesystemUnavailable(str(exc)) from exc

    manifest_replacement_started = False
    try:
        for artifact, data in artifacts:
            create_exclusive_bytes(revision_path / artifact.name, data)
        note = await session.get(Note, note_id)
        if note is None:
            raise StoreError("the linked note mirror is incomplete")
        recorded_path = manifest.get("projection_path")
        held_revision = (
            await asyncio.to_thread(projection_revision, store.notes_root, recorded_path, source_id)
            if isinstance(recorded_path, str)
            else None
        )
        projection_path = (
            str(recorded_path)
            if held_revision is not None
            else new_projection_path(
                store.notes_root,
                settings,
                provider=str(manifest["provider"]),
                title=note.title,
                note_date=note.date or _today(settings),
            )
        )
        revisions = list(manifest.get("revisions", []))
        revisions.append(_revision_document(request, revision, now, identity, artifact_metadata))
        manifest.update(
            {
                "source_type": request.source.source_type,
                "origin": request.source.origin,
                "current_revision": revision,
                "revisions": revisions,
                "projection_path": projection_path,
            }
        )
        manifest_replacement_started = True
        atomic_write_bytes(manifest_path, _json_bytes(manifest))
        await asyncio.to_thread(
            write_projection,
            store.notes_root,
            settings,
            source_id=source_id,
            revision=revision,
            title=note.title,
            revision_ingested_at=now,
            artifacts=_projection_artifacts(artifacts, artifact_metadata),
            relative_path=projection_path,
        )
    except OSError as exc:
        raise SourcesFilesystemUnavailable(str(exc)) from exc
    finally:
        # Reached with the replacement started only on the way out with the
        # revision recorded, so this removes a revision no manifest names,
        # whatever fault left it behind.
        if not manifest_replacement_started:
            shutil.rmtree(revision_path, ignore_errors=True)

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
        projection_path=projection_path,
    )


def _descriptive_differences(
    request: IngestRequest,
    manifest: dict[str, Any],
    current: dict[str, Any],
    artifact_metadata: list[dict[str, Any]],
) -> list[str]:
    """The fields describing a source that this replay sent differently.

    A source is identified by its artifact bytes, so none of these can make a
    revision of their own and this increment keeps none of them. Naming the
    ones that differ is what makes the replay answer honest rather than a
    silent discard.
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
    stored_types = {
        str(item.get("name")): item.get("mime_type") for item in current.get("artifacts", [])
    }
    if any(item["mime_type"] != stored_types.get(item["name"]) for item in artifact_metadata):
        differing.append("mime_type")
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


def _manifest_document(path: Path) -> dict[str, Any]:
    path.stat()
    return _read_json(path, "source manifest")


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
    return await asyncio.to_thread(_scan_for_linked_note, store.notes_root, source_id, schema)


def _scan_for_linked_note(
    notes_root: Path, source_id: str, schema: FrontmatterSchema
) -> tuple[str, str, tuple[bytes, dict[str, Any], str, datetime]]:
    """Find the note citing a source by reading the notes filesystem.

    Reached only when the mirror lost the link, so the files are the one place
    it survives. Every note is read and parsed, which is why the caller runs
    this in a worker thread: the store is one process with one worker, and a
    walk of a real notes filesystem on its event loop would stop readiness and
    every other request while one degraded retry recovers.
    """
    matches: list[tuple[str, str, tuple[bytes, dict[str, Any], str, datetime]]] = []
    try:
        paths = list(notes_root.rglob(f"*{NOTE_SUFFIX}"))
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
            relative = path.relative_to(notes_root).as_posix()
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


def _manifest_artifacts(revision: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts = revision.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise SourcesFilesystemUnavailable("the source manifest has invalid artifacts")
    for artifact in artifacts:
        if (
            not isinstance(artifact, dict)
            or not isinstance(artifact.get("name"), str)
            or not isinstance(artifact.get("sha256"), str)
        ):
            raise SourcesFilesystemUnavailable("the source manifest has invalid artifacts")
    return artifacts


def _verify_revision_artifacts(
    source_path: Path,
    revision: int,
    artifacts: list[dict[str, Any]],
) -> None:
    """Verify the authoritative files before confirming a replay."""
    revision_path = source_path / f"r{revision:04d}"
    for artifact in artifacts:
        name = str(artifact["name"])
        try:
            path = resolve(revision_path, name)
            with path.open("rb") as handle:
                actual = hashlib.file_digest(handle, "sha256").hexdigest()
        except (OSError, ValueError) as exc:
            raise SourcesFilesystemUnavailable(
                f"the current source revision artifact is unreadable: {name}"
            ) from exc
        if actual != artifact["sha256"]:
            raise SourcesFilesystemUnavailable(
                f"the current source revision artifact failed verification: {name}"
            )


def _verified_artifact_bytes(path: Path, size_bytes: int, sha256: str) -> bytes:
    data = path.read_bytes()
    if len(data) != size_bytes or hashlib.sha256(data).hexdigest() != sha256:
        raise SourcesFilesystemUnavailable(f"the source artifact failed verification: {path.name}")
    return data


def _read_revision_artifacts(
    source_path: Path,
    revision: int,
    artifacts: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], bytes]]:
    try:
        return [
            (
                artifact,
                resolve(source_path / f"r{revision:04d}", str(artifact["name"])).read_bytes(),
            )
            for artifact in artifacts
        ]
    except (OSError, ValueError) as exc:
        raise SourcesFilesystemUnavailable(
            "the current source revision changed while it was being read"
        ) from exc


def _projection_artifacts(
    artifacts: list[tuple[IngestArtifact, bytes]], metadata: list[dict[str, Any]]
) -> list[tuple[dict[str, Any], bytes]]:
    return [(description, data) for (_, data), description in zip(artifacts, metadata, strict=True)]


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
    names_and_hashes = sorted(
        [[str(item["name"]), str(item["sha256"])] for item in artifacts],
        key=lambda item: (item[0], item[1]),
    )
    framed = json.dumps(names_and_hashes, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(framed.encode("utf-8")).hexdigest()


def _manifest(
    request: IngestRequest,
    source_id: str,
    ingested_at: datetime,
    identity: str,
    artifacts: list[dict[str, Any]],
    projection_path: str,
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
        "projection_path": projection_path,
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
