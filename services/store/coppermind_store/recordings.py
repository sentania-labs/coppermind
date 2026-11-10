"""Recording intake: audio handed in on its own, transcribed by a service, filed for review.

The recording arrives as an artifact (the staged upload path takes files of hundreds of
MiB), then `POST /v1/recordings` names it. That makes a transcription job, kept as one
small file under `<state>/context/recordings/`, and a worker loop of its own picks the
jobs up one at a time. It is a separate loop from the enrichment worker because one
recording can take many minutes on a CPU service, and nothing else should wait for it.

The call itself is `ModelProvider.transcribe`, so it gets the machinery the model calls
get: the wait limit counts silence rather than total time, a failure that may pass backs off
before the whole recording is sent again, and every failure is a plain message with a stable
rule name.

When the service answers, the speaker segments are kept as given and filed through the
ordinary ingest as the note's source (`transcript.txt` to read, `transcript.json` with the
timings), which lands a note in Review like any other intake. The recording stays an
artifact, cited from the note until review and filing. A transcript is saved before
the note is filed, so a failure after that point retries without transcribing again.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import random
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field, ValidationError

from coppermind.context_config import ContextModel, FeatureAssignment
from coppermind.db.models import Note
from coppermind.store_protocol import (
    ArtifactMissing,
    IngestArtifact,
    IngestNote,
    IngestRequest,
    IngestSource,
    RecordingRequest,
    RecordingStatus,
    ReplaceNote,
    StoreError,
    ValidationFailed,
)
from coppermind_store import artifacts
from coppermind_store.model_provider import ProviderError

if TYPE_CHECKING:
    from coppermind_store.context_service import ContextService

log = logging.getLogger(__name__)

# How much of a failed reply is kept, as for enrichment jobs.
REPLY_KEPT = 4000
# A failed attempt sends the whole recording again, so the wait before the next one starts
# at half an hour and doubles with each attempt.
RETRY_BACKOFF_S = 1800
# A service that answers "busy" is working for someone else: that is a wait, not a failed
# attempt. It is asked again after its Retry-After, or a couple of minutes with jitter, and
# given up on only after a day of being busy without a break.
BUSY_WAIT_S = 120
BUSY_LIMIT_S = 24 * 3600
BUSY_RULE = "transcription-busy"
BUSY_MESSAGE = "Waiting for the transcription service, which is busy."
NOT_SET_UP = (
    "No transcription service is set up, so a recording on its own cannot be accepted. "
    "In Admin open Model connections, find Transcription setup, choose a connection "
    "and a model and turn it on."
)
# How much of a transcript the recording page shows; the whole text is in the note's source.
DETAIL_TRANSCRIPT_CHARS = 200_000
AUDIO_REMOVAL_ATTEMPTS = 3
AUDIO_KEPT_SHARED = "Kept because another note still uses this recording."
SUMMARY_PENDING = "_Transcribed from a recording. The summary has not been written yet._"


class TranscriptionJob(ContextModel):
    id: str
    artifact_id: str
    # The revision accepted. A newer revision of the artifact is a different recording and
    # is never substituted for it; 0 marks a job saved before revisions were pinned.
    artifact_revision: int = 0
    filename: str
    mime_type: str
    size_bytes: int
    title: str
    provider: str
    external_source_id: str
    origin: str = ""
    captured_at: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    frontmatter: dict[str, Any] = Field(default_factory=dict)
    state: Literal["queued", "running", "succeeded", "failed"] = "queued"
    attempts: int = 0
    attempt_limit: int = Field(default=2, ge=1, le=3)
    error: str = ""
    error_rule: str = ""
    reply: str = Field(default="", max_length=REPLY_KEPT)
    retry_after: str = ""
    # When the service first answered busy in the current run of busy answers.
    busy_since: str = ""
    # True once the service's transcript is saved. A retry then files it without sending
    # the recording again.
    transcribed: bool = False
    transcription_generation: int = 0
    created_at: str
    started_at: str = ""
    succeeded_at: str = ""
    usage: dict[str, Any] = Field(default_factory=dict)
    note_id: str = ""
    note_path: str = ""
    source_id: str = ""
    audio_removed: bool = False
    audio_removal_error: str = ""
    audio_removal_retry_after: str = ""
    audio_removal_attempts: int = 0
    audio_kept: str = ""

    @property
    def audio_removal_stuck(self) -> bool:
        return (
            self.state == "succeeded"
            and not self.audio_removed
            and self.audio_removal_attempts >= AUDIO_REMOVAL_ATTEMPTS
        )

    def set_error(
        self, message: str = "", rule: str = "", *, reply: str = "", retry_after: str = ""
    ) -> None:
        self.error, self.error_rule = message, rule
        self.reply, self.retry_after = reply[:REPLY_KEPT], retry_after

    def status(self) -> RecordingStatus:
        return RecordingStatus(
            id=self.id,
            state=self.state,
            artifact_id=self.artifact_id,
            attempts=self.attempts,
            attempt_limit=self.attempt_limit,
            error=self.error,
            error_rule=self.error_rule,
            retry_after=self.retry_after,
            created_at=self.created_at,
            succeeded_at=self.succeeded_at,
            note_id=self.note_id,
            note_path=self.note_path,
            source_id=self.source_id,
        )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def job_id(provider: str, external_source_id: str) -> str:
    """One job per provider and external id, so a second request finds the first."""
    return hashlib.sha256(json.dumps([provider, external_source_id]).encode()).hexdigest()


def _clock(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 3600:02d}:{whole % 3600 // 60:02d}:{whole % 60:02d}"


def render_transcript(transcript: dict[str, Any]) -> str:
    """One line per segment, the speaker label exactly as the service gave it."""
    lines = []
    for segment in transcript["segments"]:
        stamp = f"[{_clock(segment['start'])}] " if segment["start"] is not None else ""
        lines.append(f"{stamp}{segment['speaker'] or 'Unknown speaker'}: {segment['text']}")
    return "\n".join(lines) + "\n"


class Recordings:
    def __init__(self, context: ContextService) -> None:
        self.context = context
        self.worker_error = ""

    # -- state ---------------------------------------------------------------------------

    def jobs(self) -> list[TranscriptionJob]:
        found = []
        for path in sorted((self.context.root / "recordings").glob("*.json")):
            try:
                found.append(TranscriptionJob.model_validate_json(path.read_bytes()))
            except (OSError, ValidationError):
                log.warning("recording job file %s is unreadable", path.name)
        return found

    def load(self, identity: str) -> TranscriptionJob:
        if len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
            raise ValueError("invalid recording identity")
        path = self.context.root / "recordings" / f"{identity}.json"
        try:
            return TranscriptionJob.model_validate_json(path.read_bytes())
        except FileNotFoundError as exc:
            raise ValueError("no such recording intake") from exc

    def save(self, job: TranscriptionJob) -> None:
        self.context.write(f"recordings/{job.id}", job.model_dump(mode="json"))

    def assignment(self) -> FeatureAssignment | None:
        return self.context.config().assignments.get("transcription")

    def ready(self) -> FeatureAssignment:
        assignment = self.assignment()
        if assignment is None or not (
            assignment.enabled and assignment.connection and assignment.model
        ):
            raise ValueError(NOT_SET_UP)
        return assignment

    def enabled(self) -> bool:
        assignment = self.assignment()
        return bool(assignment and assignment.enabled)

    async def detail(self, payload: dict[str, Any]) -> dict[str, Any]:
        """One intake in full, for its page: the readable transcript and the technical facts."""
        job = self.load(str(payload.get("id", "")))
        transcript = ""
        if job.transcribed:
            saved = self.context.read(self.transcript_path(job), None)
            if isinstance(saved, dict):
                transcript = render_transcript(saved)
        return {
            **job.model_dump(mode="json", exclude={"frontmatter", "metadata"}),
            "can_retry": (job.state == "failed" and self.enabled()) or job.audio_removal_stuck,
            "audio_removal_stuck": job.audio_removal_stuck,
            "transcript": transcript[:DETAIL_TRANSCRIPT_CHARS],
            "transcript_cut": len(transcript) > DETAIL_TRANSCRIPT_CHARS,
        }

    def listing(self) -> list[dict[str, Any]]:
        """The newest intakes for the Jobs list: what each is, where it stands and why."""
        newest = sorted(self.jobs(), key=lambda item: item.created_at, reverse=True)[:100]
        return [
            {
                **item.model_dump(
                    mode="json",
                    exclude={"metadata", "frontmatter", "usage", "reply", "mime_type"},
                ),
                "can_retry": (item.state == "failed" and self.enabled())
                or item.audio_removal_stuck,
                "audio_removal_stuck": item.audio_removal_stuck,
            }
            for item in newest
        ]

    async def save_assignment(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Choose the connection and model that transcribe recordings, and switch it on."""
        given = FeatureAssignment.model_validate(payload["assignment"])
        async with self.context.lock:
            config = self.context.config()
            self.context.check_revision(config, payload)
            if given.connection:
                connection = self.context.connection(given.connection)
                if connection.provider == "anthropic":
                    raise ValueError(
                        "Anthropic does not offer transcription. Choose an OpenAI-compatible "
                        "connection."
                    )
            if given.enabled and not (given.connection and given.model.strip()):
                raise ValueError("choose a connection and a model before turning transcription on")
            old = config.assignments.get("transcription") or FeatureAssignment()
            config.assignments["transcription"] = FeatureAssignment(
                connection=given.connection,
                model=given.model.strip(),
                enabled=given.enabled,
                paused=given.paused,
                wait_s=given.wait_s,
                attempts=given.attempts,
                response_format=given.response_format,
                automatic_after=old.automatic_after,
            )
            self.context.save_config(config)
        return {"saved": "transcription"}

    # -- intake --------------------------------------------------------------------------

    async def accept(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Queue a stored recording for transcription; the same request answers the same job."""
        try:
            request = RecordingRequest.model_validate(payload)
        except ValidationError as exc:
            raise ValueError(
                "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
            ) from exc
        identity = job_id(request.provider, request.external_source_id)
        # An intake already accepted answers first: replaying a request must still find its job
        # after transcription is switched off or the recording is removed.
        async with self.context.lock:
            try:
                existing = self.load(identity)
            except ValueError:
                existing = None
        if existing is not None:
            return self.replay(existing, request)
        assignment = self.ready()
        try:
            _, revision = await asyncio.to_thread(
                artifacts.current_file, self.context.store, request.artifact_id
            )
        except ArtifactMissing as exc:
            raise ValueError(
                f"artifact_id: no stored file with id {request.artifact_id}. Upload the "
                "recording first (POST /v1/staged for a large one), then name its id here."
            ) from exc
        mime = str(revision.get("mime_type") or "")
        if not mime.startswith("audio/"):
            raise ValueError(
                f"artifact_id: this file is {mime or 'of an unknown type'}, not a recording. "
                "Send the audio with an audio media type."
            )
        accepted_revision = revision["revision"]
        async with self.context.lock:
            existing = None
            with contextlib.suppress(ValueError):
                existing = self.load(identity)
            if existing is not None:
                return self.replay(existing, request)
            # Cleanup may have retired the artifact while this request was waiting.
            try:
                _, revision = await asyncio.to_thread(
                    artifacts.current_file, self.context.store, request.artifact_id
                )
            except ArtifactMissing as exc:
                raise ValueError("artifact_id: the recording is no longer available.") from exc
            if revision["revision"] != accepted_revision:
                raise ValueError("artifact_id: the recording changed; submit the request again.")
            job = TranscriptionJob(
                id=identity,
                artifact_id=request.artifact_id,
                artifact_revision=int(revision["revision"]),
                filename=str(revision["filename"]),
                mime_type=mime,
                size_bytes=int(revision["size_bytes"]),
                title=request.title,
                provider=request.provider,
                external_source_id=request.external_source_id,
                origin=request.origin or "api",
                captured_at=request.captured_at.isoformat() if request.captured_at else "",
                metadata=request.metadata,
                frontmatter=request.frontmatter,
                attempt_limit=assignment.attempts,
                created_at=_now(),
            )
            self.save(job)
        return job.status().model_dump(mode="json")

    @staticmethod
    def replay(existing: TranscriptionJob, request: RecordingRequest) -> dict[str, Any]:
        if existing.artifact_id != request.artifact_id:
            raise ValueError(
                "external_source_id: this provider and id already belong to a different "
                "recording. Use a new external_source_id for new material."
            )
        return existing.status().model_dump(mode="json")

    async def status(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.load(str(payload.get("id", ""))).status().model_dump(mode="json")

    async def retry(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Queue a failed intake again with fresh attempts; a saved transcript is reused."""
        async with self.context.lock:
            job = self.load(str(payload.get("id", "")))
            if job.audio_removal_stuck:
                job.audio_removal_attempts, job.audio_removal_retry_after = 0, ""
                job.audio_removal_error = ""
                self.save(job)
                return {"job": job.id}
            if job.state != "failed":
                raise ValueError("only a failed recording can be retried")
            self.ready()
            job.state, job.attempts, job.busy_since = "queued", 0, ""
            job.set_error()
            self.save(job)
        return {"job": job.id}

    # -- worker --------------------------------------------------------------------------

    async def run(self) -> None:
        while True:
            worked = False
            try:
                worked = await self.tick()
                self.worker_error = ""
            except asyncio.CancelledError:
                raise
            except Exception:
                self.worker_error = (
                    "The recording worker stopped unexpectedly and will try again shortly."
                )
                log.exception("recording worker failed")
            if not worked:
                await asyncio.sleep(10)

    async def tick(self) -> bool:
        """Run the oldest job that is due. True when one ran."""
        for job in sorted(await asyncio.to_thread(self.jobs), key=lambda item: item.created_at):
            if job.state == "succeeded" and not job.audio_removed:
                if job.audio_removal_stuck:
                    continue
                if job.audio_removal_retry_after and job.audio_removal_retry_after > _now():
                    continue
                if await self.remove_reviewed_audio(job):
                    return True
        assignment = self.assignment()
        if assignment is None or not assignment.enabled or assignment.paused:
            return False
        for known in sorted(await asyncio.to_thread(self.jobs), key=lambda item: item.created_at):
            job = self.load(known.id)
            if job.state in {"succeeded"}:
                continue
            if job.state == "running":
                # The store stopped mid-call. That is not a failed attempt: asking again either
                # re-attaches to the job the service has under the same id, or finds the
                # service still busy with the old upload and waits.
                self.interrupt(job)
                self.save(job)
            if job.state == "failed" and job.attempts < job.attempt_limit:
                if job.retry_after and job.retry_after > _now():
                    continue
                if repeats_only(job.error_rule):
                    continue
                job.state = "queued"
            if job.state == "queued":
                if job.retry_after and job.retry_after > _now():
                    continue
                await self.run_job(job, assignment)
                return True
        return False

    @staticmethod
    def interrupt(job: TranscriptionJob) -> None:
        job.state, job.attempts = "queued", max(0, job.attempts - 1)
        job.set_error()

    @staticmethod
    def wait_for_service(job: TranscriptionJob, exc: ProviderError) -> None:
        """The service said busy: ask again soon without using up an attempt."""
        now = datetime.now(UTC)
        job.attempts = max(0, job.attempts - 1)
        since = datetime.fromisoformat(job.busy_since) if job.busy_since else now
        job.busy_since = since.isoformat()
        if (now - since).total_seconds() >= BUSY_LIMIT_S:
            job.state = "failed"
            job.set_error(
                "The transcription service stayed busy for 24 hours, so the app stopped asking. "
                "Press Retry when it is free.",
                "transcription-busy-limit",
            )
            return
        wait = exc.retry_after or BUSY_WAIT_S * random.uniform(1.0, 1.5)
        job.state = "queued"
        job.set_error(
            BUSY_MESSAGE, BUSY_RULE, retry_after=(now + timedelta(seconds=wait)).isoformat()
        )

    async def remove_reviewed_audio(self, job: TranscriptionJob) -> bool:
        """Remove only the accepted revision after its nonempty transcript is filed."""
        async with self.context.lock:
            return await self._remove_reviewed_audio(job)

    async def _remove_reviewed_audio(self, job: TranscriptionJob) -> bool:
        store = self.context.store
        try:
            if not job.transcribed:
                return False
            async with store.session_factory() as session:
                row = await session.get(Note, job.note_id)
            if row is None:
                return False
            deleted = row.state == "deleted"
            if not deleted and not (row.filed and row.reviewed):
                return False
            if await self._shared_audio(job):
                return self._keep_audio(job, AUDIO_KEPT_SHARED)
            transcript = await asyncio.to_thread(self.saved_transcript, job)
            if not any(
                str(segment.get("text") or "").strip()
                for segment in transcript.get("segments", [])
                if isinstance(segment, dict)
            ):
                return False
            if deleted:
                await artifacts.remove_recording_audio(
                    store, job.artifact_id, job.artifact_revision
                )
                return self._audio_removed(job)
            note = await store.get_note(job.note_id)
            reviewed_key = store.control.schema().role("reviewed_key")
            if note.frontmatter.get(reviewed_key) is not True or not note.filed:
                return False
            manifest = await asyncio.to_thread(artifacts.read_manifest, store, job.artifact_id)
            revision = artifacts._current_revision(manifest)
            if not job.artifact_revision or revision.get("revision") != job.artifact_revision:
                raise ValueError("The recording has changed since it was accepted.")
            link = artifacts.link_for(
                manifest.get("path"), str(manifest["title"]), manifest.get("url")
            )
            duration = transcript.get("duration")
            length = f"{duration} seconds" if duration is not None else "unknown"
            receipt = (
                "Audio is scheduled for removal after review. "
                f"SHA-256: {revision['sha256']}; size: {revision['size_bytes']} bytes; "
                f"length: {length}."
            )
            pointer = job.metadata.get("archive_url")
            if pointer:
                receipt += f" Archive: {pointer}"
            finished = receipt.replace("is scheduled for removal", "was removed")
            if receipt not in note.body and finished not in note.body:
                body = (
                    note.body.replace(link, receipt)
                    if link in note.body
                    else note.body + "\n\n" + receipt + "\n"
                )
                fields = dict(note.frontmatter)
                artifacts_key = store.control.schema().optional_role("artifacts_key")
                cited = fields.get(artifacts_key)
                if isinstance(cited, list):
                    fields[artifacts_key] = [item for item in cited if item != job.artifact_id]
                note = await store.replace_note(
                    job.note_id,
                    ReplaceNote(frontmatter=fields, body=body),
                    note.content_hash,
                )
            # Serialize against API note edits and recheck both the file and filing mirror.
            from coppermind_store.notes import _bytes_at

            relative, path = await store._locate(job.note_id)
            async with store._lock_for(job.note_id):
                _, fields, _ = _bytes_at(
                    job.note_id, relative, path, store.control.schema(), note.content_hash
                )
                async with store.session_factory() as session:
                    row = await session.get(Note, job.note_id)
                    if (
                        not row
                        or not row.filed
                        or not row.reviewed
                        or fields.get(reviewed_key) is not True
                    ):
                        return False
                await artifacts.remove_recording_audio(
                    store, job.artifact_id, job.artifact_revision
                )
            note = await store.get_note(job.note_id)
            if receipt in note.body:
                await store.replace_note(
                    job.note_id,
                    ReplaceNote(
                        frontmatter=note.frontmatter, body=note.body.replace(receipt, finished)
                    ),
                    note.content_hash,
                )
            return self._audio_removed(job)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("recording audio removal failed for %s", job.id)
            job.audio_removal_attempts += 1
            job.audio_removal_error = "The reviewed recording could not be removed."
            job.audio_removal_retry_after = (
                datetime.now(UTC) + timedelta(minutes=5 * job.audio_removal_attempts)
            ).isoformat()
            self.save(job)
            return True

    def _audio_removed(self, job: TranscriptionJob) -> bool:
        job.audio_removed, job.audio_kept = True, ""
        job.audio_removal_error, job.audio_removal_retry_after = "", ""
        job.audio_removal_attempts = 0
        self.save(job)
        return True

    def _keep_audio(self, job: TranscriptionJob, reason: str) -> bool:
        if job.audio_kept != reason or job.audio_removal_error or job.audio_removal_attempts:
            job.audio_kept, job.audio_removal_error = reason, ""
            job.audio_removal_retry_after, job.audio_removal_attempts = "", 0
            self.save(job)
        return False

    async def _shared_audio(self, job: TranscriptionJob) -> bool:
        """Whether another intake or note that is not yet finished still needs this audio."""
        store = self.context.store

        async def finished(note_id: str) -> bool:
            async with store.session_factory() as session:
                row = await session.get(Note, note_id)
            return row is None or row.state == "deleted" or bool(row.filed and row.reviewed)

        for other in await asyncio.to_thread(self.jobs):
            if other.id == job.id or other.artifact_id != job.artifact_id or other.audio_removed:
                continue
            if other.state != "succeeded" or not await finished(other.note_id):
                return True
        async with store.session_factory() as session:
            citing = await artifacts.citing_notes(session, [job.artifact_id])
        for item in citing.get(job.artifact_id, []):
            if item.id != job.note_id and not await finished(item.id):
                return True
        return False

    async def run_job(self, job: TranscriptionJob, assignment: FeatureAssignment) -> None:
        from coppermind_store.context_service import TIMEOUT_BACKOFF_S

        job.state, job.started_at = "running", _now()
        job.attempts += 1
        job.set_error()
        self.save(job)

        def later() -> str:
            if job.attempts >= job.attempt_limit:
                return ""
            return (datetime.now(UTC) + timedelta(seconds=TIMEOUT_BACKOFF_S)).isoformat()

        busy = False
        try:
            transcript = (
                self.saved_transcript(job) if job.transcribed else await self.transcribe(job)
            )
            await self.file_note(job, transcript)
            job.state, job.succeeded_at = "succeeded", _now()
            job.set_error()
        except asyncio.CancelledError:
            self.interrupt(job)
            self.save(job)
            raise
        except ProviderError as exc:
            if exc.job_failed:
                job.transcription_generation += 1
            if exc.rule == BUSY_RULE:
                busy = True
                self.wait_for_service(job, exc)
            else:
                wait = (
                    (
                        datetime.now(UTC)
                        + timedelta(seconds=RETRY_BACKOFF_S * 2 ** (job.attempts - 1))
                    ).isoformat()
                    if not repeats_only(exc.rule) and job.attempts < job.attempt_limit
                    else ""
                )
                job.state = "failed"
                job.set_error(str(exc), exc.rule, reply=exc.reply, retry_after=wait)
        except ArtifactMissing:
            job.state = "failed"
            job.set_error(
                "The stored recording was removed before the note could cite it. The transcript "
                "is kept and the note is filed without that citation."
                if job.transcribed
                else "The stored recording is gone, so there is nothing to transcribe. "
                "Upload it again under a new external id.",
                "recording-missing",
            )
        except ValidationFailed as exc:
            job.state = "failed"
            job.set_error(
                "The note could not be filed: " + "; ".join(exc.errors)[:500] + ". The transcript "
                "is kept. The note fields are fixed once the recording is accepted, so a retry "
                "succeeds only after the schema is changed; otherwise upload the recording "
                "again under a new external id, which transcribes it again.",
                "note-rejected",
            )
        except (StoreError, OSError) as exc:
            job.state = "failed"
            job.set_error(
                f"Filing the note failed ({type(exc).__name__}). The transcript is kept; "
                "retry once the store is healthy."
                if job.transcribed
                else f"The stored recording could not be read ({type(exc).__name__}). The audio "
                "is kept; retry once the store is healthy.",
                "filing-failed" if job.transcribed else "recording-unreadable",
                retry_after=later(),
            )
        except ValueError as exc:
            job.state = "failed"
            job.set_error(
                ("The note could not be filed: " if job.transcribed else "") + str(exc)[:500],
                "recording-not-ready",
                retry_after=later(),
            )
        except Exception:
            job.state = "failed"
            job.set_error(
                "Transcription stopped unexpectedly. Your recording is still saved"
                + (" and so is its transcript" if job.transcribed else "")
                + ". Try again; if it fails again, open details.",
                "job-error",
                retry_after=later(),
            )
            log.exception("recording job %s failed unexpectedly", job.id)
        if not busy:
            job.busy_since = ""
        if job.state == "failed":
            log.warning(
                "recording job %s failed: rule=%s attempts=%s", job.id, job.error_rule, job.attempts
            )
        self.save(job)

    def transcript_path(self, job: TranscriptionJob) -> str:
        return f"recording-transcripts/{job.id}"

    def saved_transcript(self, job: TranscriptionJob) -> dict[str, Any]:
        transcript = self.context.read(self.transcript_path(job), None)
        if not isinstance(transcript, dict):
            job.transcribed = False
            raise ProviderError(
                "The saved transcript could not be read, so the recording will be transcribed "
                "again.",
                "transcript-lost",
                waiting=True,
            )
        return transcript

    async def transcribe(self, job: TranscriptionJob) -> dict[str, Any]:
        assignment = self.assignment()
        assert assignment is not None
        try:
            connection = self.context.connection(assignment.connection)
        except ValueError as exc:
            raise ProviderError(
                "The Transcription connection is not set up: "
                f"{exc}. Open Model connections, find Transcription setup, and choose one.",
                "connection-missing",
            ) from exc
        path, revision = await asyncio.to_thread(
            artifacts.current_file,
            self.context.store,
            job.artifact_id,
            job.artifact_revision or None,
        )

        duration = job.metadata.get("duration_seconds")
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(duration)
            or duration <= 0
        ):
            duration = None
        speakers = job.metadata.get("num_speakers")
        if isinstance(speakers, bool) or not isinstance(speakers, int) or speakers < 1:
            speakers = None
        transcript, usage = await self.context.provider.transcribe(
            connection,
            self.context.key(connection.id),
            assignment,
            path,
            filename=job.filename,
            mime_type=job.mime_type,
            size_bytes=int(revision["size_bytes"]),
            job_id=f"{job.id}-{job.transcription_generation}",
            duration_seconds=duration,
            num_speakers=speakers,
        )
        transcript["model"] = assignment.model
        self.context.write(self.transcript_path(job), transcript)
        job.transcribed, job.usage = True, usage
        self.save(job)
        return transcript

    async def file_note(self, job: TranscriptionJob, transcript: dict[str, Any]) -> None:
        """File the transcript as a source and a note in Review, and cite the recording."""
        captured = datetime.fromisoformat(job.captured_at) if job.captured_at else None
        request = IngestRequest(
            source=IngestSource(
                provider=job.provider,
                external_source_id=job.external_source_id,
                source_type="transcript",
                origin=job.origin,
                captured_at=captured,
                metadata={
                    **job.metadata,
                    "recording_artifact_id": job.artifact_id,
                    "transcription_model": transcript.get("model", ""),
                },
                artifacts=[
                    IngestArtifact(
                        name="transcript.txt",
                        mime_type="text/plain",
                        content=render_transcript(transcript),
                    ),
                    IngestArtifact(
                        name="transcript.json",
                        mime_type="application/json",
                        content=json.dumps(transcript, ensure_ascii=False, indent=1),
                    ),
                ],
            ),
            note=IngestNote(
                title=job.title, body=SUMMARY_PENDING, frontmatter=dict(job.frontmatter)
            ),
        )
        result = await self.context.store.ingest(request)
        job.note_id, job.note_path = result.note.id, result.note.path
        job.source_id = result.source.id
        self.save(job)
        try:
            await artifacts.known_paths(self.context.store, [job.artifact_id])
        except ValidationFailed as exc:
            raise ArtifactMissing(job.artifact_id) from exc
        await artifacts.cite(self.context.store, result.note.id, [job.artifact_id])


# A refusal that the same request would only repeat is not tried again by itself. The
# transcription service's refusals are named `transcription-http-<status>`; only 5xx, 408
# and 429 are named plain `transcription-http`, and those can pass.
NO_AUTOMATIC_RETRY = frozenset(
    {
        "provider-too-long",
        "transcription-audio_unreadable",
        "transcription-no_speech",
        "transcription-too_long",
        "transcription-provider",
        "transcript-empty",
        "transcript-flat",
        "transcript-no-speakers",
        "recording-missing",
        "transcription-busy-limit",
        "transcription-unfinished",
        "note-rejected",
        "connection-missing",
    }
)


def repeats_only(rule: str) -> bool:
    return rule in NO_AUTOMATIC_RETRY or rule.startswith("transcription-http-")
