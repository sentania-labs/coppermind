"""Create-only source ingestion.

A deterministic filesystem claim owns each external identifier. Artifact
files are exclusive, then `manifest.json` is written last so an interrupted
bundle is never mistaken for complete. The linked Review note is the final
filesystem write in the same database transaction. A write-phase failure
removes the claim and bundle; a commit failure retains the complete files and
claim so a retry cannot duplicate them.
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

from coppermind import frontmatter as fm
from coppermind.atomicio import create_exclusive_bytes
from coppermind.db.models import Note, NoteSource, Source, SourceArtifact, SourceRevision
from coppermind.db.session import transaction
from coppermind.ids import new_id
from coppermind.naming import sanitize_folder, unique_stem
from coppermind.store_protocol import (
    CreatedNote,
    CreatedSource,
    CreateNote,
    IngestRequest,
    IngestResult,
    NotesFilesystemUnavailable,
    PathCollision,
    PayloadTooLarge,
    SourceAlreadyExists,
    SourcesFilesystemUnavailable,
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

    source_id = new_id()
    note_id = new_id()
    now = datetime.now(tz=UTC)
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

    note_request = CreateNote(
        title=request.note.title,
        body=request.note.body,
        frontmatter={
            **request.note.frontmatter,
            schema.role("sources_key"): [source_id],
        },
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
    manifest_path = source_path / "manifest.json"
    claim_path = _external_id_claim_path(
        store.sources_root,
        request.source.provider,
        request.source.external_source_id,
    )
    claim = _external_id_claim(request, source_id)
    manifest = _manifest(request, source_id, now, identity, artifact_metadata)
    claim_created = False
    filesystem_complete = False

    try:
        async with transaction(store.session_factory) as session:
            await session.execute(sa.text("SELECT 1"))

            try:
                create_exclusive_bytes(claim_path, claim)
            except FileExistsError as exc:
                raise SourceAlreadyExists(
                    request.source.provider, request.source.external_source_id
                ) from exc
            except OSError as exc:
                raise SourcesFilesystemUnavailable(str(exc)) from exc
            claim_created = True

            session.add(
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
                )
            )
            # The filesystem claim is authoritative. This constraint mirrors
            # it and remains a second guard for stale or imported rows.
            await session.flush()

            session.add_all(
                [
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
                    *[
                        SourceArtifact(
                            source_id=source_id,
                            revision=1,
                            name=item["name"],
                            mime_type=item["mime_type"],
                            sha256=item["sha256"],
                            size_bytes=item["size_bytes"],
                        )
                        for item in artifact_metadata
                    ],
                ]
            )
            await session.flush()

            try:
                try:
                    revision_path.mkdir(parents=True, exist_ok=False)
                    for artifact, data in artifacts:
                        create_exclusive_bytes(revision_path / artifact.name, data)
                    create_exclusive_bytes(manifest_path, manifest)
                except OSError as exc:
                    raise SourcesFilesystemUnavailable(str(exc)) from exc

                try:
                    create_exclusive_bytes(note_path, note_data)
                except FileExistsError as exc:
                    raise PathCollision(relative) from exc
                except OSError as exc:
                    raise NotesFilesystemUnavailable(str(exc)) from exc
                filesystem_complete = True
            except BaseException:
                shutil.rmtree(source_path, ignore_errors=True)
                raise
    except SourceAlreadyExists:
        raise
    except IntegrityError as exc:
        constraint = _violated_constraint(exc)
        if constraint == "uq_sources_provider_external_id":
            raise SourceAlreadyExists(
                request.source.provider, request.source.external_source_id
            ) from exc
        if constraint == "uq_notes_path":
            raise PathCollision(relative) from exc
        typed = _metadata_failure(exc)
        if typed is not None:
            raise typed from exc
        raise
    except BaseException as exc:
        typed = _metadata_failure(exc)
        if typed is not None:
            raise typed from exc
        raise
    finally:
        if claim_created and not filesystem_complete:
            try:
                claim_path.unlink(missing_ok=True)
            except OSError as exc:
                raise SourcesFilesystemUnavailable(str(exc)) from exc

    return IngestResult(
        source=CreatedSource(id=source_id),
        note=CreatedNote(id=note_id, path=relative),
    )


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
    captured = request.source.captured_at
    document = {
        "schema_version": 1,
        "source_id": source_id,
        "provider": request.source.provider,
        "external_source_id": request.source.external_source_id,
        "source_type": request.source.source_type,
        "origin": request.source.origin,
        "current_revision": 1,
        "revisions": [
            {
                "revision": 1,
                "ingested_at": ingested_at.isoformat(),
                "captured_at": captured.isoformat() if captured else None,
                "content_identity": identity,
                "metadata": _jsonable(request.source.metadata),
                "artifacts": artifacts,
            }
        ],
    }
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
