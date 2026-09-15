"""HTTP implementation of the store contract.

Used by every process that is not the store: the API today, the curator and
the indexer later. It turns the store's HTTP error envelope back into the
typed errors of `store_protocol`, so a caller handles the same exceptions
whether it is talking to a local store object or to the store over the
network.
"""

from __future__ import annotations

from urllib.parse import quote

import httpx

from coppermind.api_keys import ApiKeySet
from coppermind.store_protocol import (
    ArtifactNotFound,
    CreateNote,
    ETag,
    IncompleteRevision,
    IngestRequest,
    IngestResult,
    MetadataUnavailable,
    NoteDocument,
    NoteId,
    NoteQuery,
    NotesFilesystemUnavailable,
    NoteSummary,
    NoteUnparseable,
    NotFound,
    Page,
    PatchFrontmatter,
    PathCollision,
    PayloadTooLarge,
    PreconditionRequired,
    ProjectionNotPlaced,
    ReplaceNote,
    SourceArtifactDocument,
    SourceClaimMissing,
    SourceId,
    SourceManifest,
    SourceNotFound,
    SourcesFilesystemUnavailable,
    StoreError,
    StoreUnavailable,
    ValidationFailed,
    VersionConflict,
)

INTERNAL_PREFIX = "/internal/v1"


def _segment(value: str) -> str:
    """Encode a value that is one path segment and nothing else.

    An identifier arrives from a caller, so without this a `?` or a `#` in it
    would be reparsed as a query or a fragment and the store would be asked
    for a different note than the one requested.
    """
    return quote(value, safe="")


class HttpStoreClient:
    """The store, reached over the internal contract."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def create_note(self, request: CreateNote) -> NoteDocument:
        response = await self._send("POST", f"{INTERNAL_PREFIX}/notes", json=request.model_dump())
        return NoteDocument.model_validate(response.json())

    async def get_note(self, note_id: NoteId) -> NoteDocument:
        response = await self._send("GET", f"{INTERNAL_PREFIX}/notes/{_segment(note_id)}")
        return NoteDocument.model_validate(response.json())

    async def list_notes(self, query: NoteQuery) -> Page[NoteSummary]:
        response = await self._send(
            "GET",
            f"{INTERNAL_PREFIX}/notes",
            params=query.model_dump(mode="json", by_alias=True, exclude_none=True),
        )
        return Page[NoteSummary].model_validate(response.json())

    async def replace_note(
        self, note_id: NoteId, request: ReplaceNote, if_match: ETag
    ) -> NoteDocument:
        response = await self._send(
            "PUT",
            f"{INTERNAL_PREFIX}/notes/{_segment(note_id)}",
            json=request.model_dump(),
            headers={"If-Match": f'"{if_match}"'},
        )
        return NoteDocument.model_validate(response.json())

    async def patch_frontmatter(
        self, note_id: NoteId, request: PatchFrontmatter, if_match: ETag
    ) -> NoteDocument:
        response = await self._send(
            "PATCH",
            f"{INTERNAL_PREFIX}/notes/{_segment(note_id)}/frontmatter",
            json=request.model_dump(),
            headers={"If-Match": f'"{if_match}"'},
        )
        return NoteDocument.model_validate(response.json())

    async def ingest(
        self, request: IngestRequest, *, payload_size_bytes: int | None = None
    ) -> IngestResult:
        headers = (
            {"X-Coppermind-Payload-Bytes": str(payload_size_bytes)}
            if payload_size_bytes is not None
            else None
        )
        response = await self._send(
            "POST",
            f"{INTERNAL_PREFIX}/ingest",
            json=request.model_dump(mode="json", exclude_none=True),
            headers=headers,
        )
        return IngestResult.model_validate(response.json())

    async def get_source(self, source_id: SourceId) -> SourceManifest:
        response = await self._send("GET", f"{INTERNAL_PREFIX}/sources/{_segment(source_id)}")
        return SourceManifest.model_validate(response.json())

    async def get_source_artifact(
        self, source_id: SourceId, revision: int, name: str
    ) -> SourceArtifactDocument:
        response = await self._send(
            "GET",
            f"{INTERNAL_PREFIX}/sources/{_segment(source_id)}/revisions/{revision}/artifacts/"
            f"{_segment(name)}",
        )
        return SourceArtifactDocument.model_validate(response.json())

    async def get_api_keys(self) -> ApiKeySet:
        response = await self._send("GET", f"{INTERNAL_PREFIX}/api-keys")
        return ApiKeySet.model_validate(response.json())

    async def is_ready(self) -> bool:
        """True when the store reports itself ready. Never raises."""
        try:
            response = await self._client.get("/readyz")
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    async def _send(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        try:
            response = await self._client.request(method, url, **kwargs)  # type: ignore[arg-type]
        except httpx.HTTPError as exc:
            raise StoreUnavailable(str(exc)) from exc
        if response.is_success:
            return response
        raise _as_typed_error(response)


def _as_typed_error(response: httpx.Response) -> Exception:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    code = payload.get("error", "")
    message = payload.get("message", response.text)
    if response.status_code == 404 and "source_id" in payload and "name" in payload:
        return ArtifactNotFound(
            payload.get("source_id", ""), payload.get("revision", 0), payload.get("name", "")
        )
    if response.status_code == 404 and "source_id" in payload:
        return SourceNotFound(payload.get("source_id", ""))
    if response.status_code == 404:
        return NotFound(payload.get("note_id", message))
    if code == "path_collision":
        return PathCollision(payload.get("existing_path", message))
    if code == "source_claim_missing":
        return SourceClaimMissing(
            payload.get("provider", ""), payload.get("external_source_id", "")
        )
    if code == "projection_not_placed":
        return ProjectionNotPlaced(
            payload.get("source_id", ""), payload.get("revision", 0), payload.get("path", message)
        )
    if code == "incomplete_revision":
        return IncompleteRevision(payload.get("path", message))
    if code == "payload_too_large":
        return PayloadTooLarge(payload.get("limit_bytes", 0))
    if code == "sources_filesystem_unavailable":
        return SourcesFilesystemUnavailable(payload.get("detail", message))
    if code == "version_conflict":
        return VersionConflict(payload.get("current_version", ""))
    if code == "precondition_required":
        return PreconditionRequired()
    if code == "note_unparseable":
        return NoteUnparseable(payload.get("note_id", ""), payload.get("reason", message))
    if code == "validation_error":
        return ValidationFailed(payload.get("errors", [message]))
    if code == "notes_filesystem_unavailable":
        return NotesFilesystemUnavailable(payload.get("detail", message))
    if code == "metadata_unavailable" or response.status_code == 503:
        return MetadataUnavailable(message)
    # This is only reached because the store answered, so it is not
    # unreachable. Saying otherwise would point the operator at a store that
    # is up while the real cause sits in its log.
    return StoreError(payload.get("detail", f"store returned {response.status_code}: {message}"))
