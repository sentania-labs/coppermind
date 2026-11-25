"""Notes in Review: one list of every note waiting there, and the two things an owner asks of it.

*Resummarize* writes a recording's note up again from the transcript already saved, with no new
transcription. *Enrich* runs the suggestion step on the note again (the same request the review
panel makes). Neither changes the note's fields or whether it is reviewed.

A note written up in one step has its write-up as its body. A rewrite replaces that body only
while it is still exactly what was generated; the version it replaces is kept. A body the owner
edited is never touched: the new write-up waits beside the note, and replacing the owner's
version takes an explicit second request. A failed rewrite leaves the note as it was.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from coppermind.db.models import Note
from coppermind.ids import is_valid_id
from coppermind.naming import sanitize_folder
from coppermind.recording_writeup import failure_reason
from coppermind.store_protocol import NotFound, ReplaceNote, StoreError

if TYPE_CHECKING:
    from coppermind_store.context_service import ContextService

log = logging.getLogger("coppermind.store.review_notes")

STATE = "note-resummarize/"
PROPOSED = "recording-writeup-proposed/"
LISTED = 200
DIFF_LINES = 400
EARLIER_KEPT = 5
SAME_BODY = "The note already says what the new write-up says."


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _step(state: str, reason: str = "", when: str = "", **more: Any) -> dict[str, Any]:
    return {"state": state, "reason": reason, "when": when, **more}


class Unwritable(Exception):
    """A rewrite that cannot start, with the reason in words for the owner."""


class ReviewNotes:
    def __init__(self, context: ContextService) -> None:
        self.context = context
        # A "running" record only means something while its task is alive in this process.
        self.live: dict[str, asyncio.Task[None]] = {}
        # Rewrites go one at a time, as recordings do, so a long backlog cannot flood the service.
        self.turn = asyncio.Semaphore(1)

    # -- the list ------------------------------------------------------------------------

    async def listing(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Every note in the Review folder, with where its write-up and suggestions stand."""
        context = self.context
        folder = sanitize_folder(context.store.control.settings().notes.review_folder)
        if not folder:
            return {"notes": [], "suggestions_on": False, "writeup_on": False}
        async with context.store.session_factory() as session:
            rows = list(
                (
                    await session.scalars(
                        sa.select(Note)
                        .where(Note.state == "ok", Note.path.startswith(f"{folder}/"))
                        .order_by(Note.first_seen_at.desc())
                        .limit(LISTED)
                    )
                ).all()
            )
        recordings = {
            job.note_id: job for job in context.recordings.jobs() if job.note_id and job.source_id
        }
        latest = context.recordings.latest_enrichment()
        suggestions_on = bool(context.config().assignments["enrichment"].enabled)
        notes = []
        for row in rows:
            job = recordings.get(row.id)
            notes.append(await self.row(row, job, latest.get(row.id), suggestions_on))
        return {
            "notes": notes,
            "suggestions_on": suggestions_on,
            "writeup_on": self.writeup_ready(),
        }

    def writeup_ready(self) -> bool:
        from coppermind_store.recording_writeup import assignment

        try:
            limits = assignment(self.context)
        except Exception:  # noqa: BLE001
            return False
        return bool(limits.enabled and not limits.paused and limits.connection)

    async def row(
        self, row: Note, job: Any, enrichment: Any, suggestions_on: bool
    ) -> dict[str, Any]:
        item: dict[str, Any] = {
            "id": row.id,
            "title": row.title or row.path.rsplit("/", 1)[-1].removesuffix(".md"),
            "path": row.path,
            "date": (job.captured_at if job and job.captured_at else "")
            or (row.date.isoformat() if row.date else ""),
            "from_recording": job is not None,
            "can_resummarize": job is not None and not row.reviewed,
            "can_enrich": suggestions_on and not row.reviewed,
        }
        item["writeup"] = self.writeup_step(row.id, job, enrichment)
        item["suggestions"] = self.suggestions_step(row.id, job, enrichment, suggestions_on)
        kept = self.context.read(STATE + row.id, {})
        if kept.get("state") == "kept":
            item["kept"] = {
                "when": kept.get("finished_at", ""),
                "difference": kept.get("difference", []),
                "if_match": kept.get("if_match", ""),
            }
        return item

    def writeup_step(self, note_id: str, job: Any, enrichment: Any) -> dict[str, Any]:
        if job is None:
            return _step("none")
        if job.generation_mode != "markdown-v1":
            # An older recording note gets its summary from the suggestion step.
            summary = self.context.recordings.summary_of(
                job, {note_id: enrichment} if enrichment else {}
            )
            if enrichment is not None and enrichment.state in {"queued", "running"}:
                return _step("working", when=enrichment.created_at)
            state = summary["summary_state"]
            if state == "succeeded":
                return _step("written", when=summary["summary_finished_at"])
            if state == "failed":
                return _step(
                    "failed",
                    summary["summary_error"] or "The AI service did not give a usable summary.",
                    summary["summary_finished_at"],
                )
            if state == "running":
                return _step("working", when=job.created_at)
            return _step("missing")
        record = self.context.read(STATE + note_id, {})
        if record.get("state") == "running":
            if note_id in self.live:
                return _step("working", when=record["started_at"])
            return _step(
                "failed",
                "The rewrite was interrupted. The note is unchanged.",
                record.get("started_at", ""),
            )
        if record.get("state") == "failed":
            return _step("failed", record.get("error", ""), record.get("finished_at", ""))
        saved = self.context.read("recording-writeups/" + job.id, {})
        if not saved.get("body"):
            return _step("missing")
        return _step("written", when=record.get("finished_at") or job.succeeded_at)

    def suggestions_step(
        self, note_id: str, job: Any, enrichment: Any, suggestions_on: bool
    ) -> dict[str, Any]:
        if enrichment is not None:
            if enrichment.state in {"queued", "running"}:
                return _step("working", when=enrichment.created_at)
            ended = enrichment.ended
            if ended == "failed":
                return _step(
                    "failed",
                    enrichment.plain or enrichment.error or "The AI service did not answer.",
                    enrichment.finished_at,
                )
            if ended == "succeeded":
                return _step("written", when=enrichment.finished_at)
            if ended == "skipped":
                return _step("missing", "There was nothing to suggest.", enrichment.finished_at)
        if job is not None and job.generation_mode == "markdown-v1":
            saved = self.context.read("recording-writeups/" + job.id, {})
            if saved.get("suggestions"):
                return _step("written", when=job.succeeded_at)
            if saved.get("suggestion_error"):
                return _step("failed", saved["suggestion_error"], job.succeeded_at)
        return _step("missing" if suggestions_on else "off")

    # -- resummarize ---------------------------------------------------------------------

    async def resummarize(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Write a recording's note up again from its saved transcript.

        `replace` with the `if_match` the list gave swaps in a rewrite that was held back
        because the owner had edited the note."""
        note_id = str(payload.get("id", ""))
        if not is_valid_id(note_id):
            raise ValueError("That note is no longer listed.")
        async with self.context.store.session_factory() as session:
            row = await session.get(Note, note_id)
        if row is None or row.state != "ok":
            raise NotFound(note_id)
        job = next(
            (j for j in self.context.recordings.jobs() if j.note_id == note_id and j.source_id),
            None,
        )
        if job is None:
            raise ValueError(
                "This note did not come from a recording, so there is nothing to write up again."
            )
        if job.generation_mode != "markdown-v1":
            from coppermind_store.ai_services import Refused

            try:
                result = await self.context.store.admin.notes.summary_again({"id": note_id})
            except Refused as refusal:
                raise ValueError(refusal.message) from refusal
            return {"started": True, "already": bool(result.get("already"))}
        if row.reviewed:
            raise ValueError(
                "This note has been reviewed, so its write-up is yours now and Coppermind "
                "does not write it again."
            )
        if payload.get("replace") is True:
            return await self.replace(row, job, str(payload.get("if_match", "")))
        record = self.context.read(STATE + note_id, {})
        if record.get("state") == "running" and note_id in self.live:
            return {"started": True, "already": True}
        if not self.writeup_ready():
            raise ValueError(
                "Recordings are not set up to be written up. Choose a writing service and turn "
                "write-ups on in Coppermind Admin, under How recordings are written up."
            )
        self.context.write(STATE + note_id, {"state": "running", "started_at": _now()})
        task = asyncio.create_task(self.rewrite(note_id, job.id), name="coppermind-resummarize")
        self.live[note_id] = task
        task.add_done_callback(lambda _: self.live.pop(note_id, None))
        return {"started": True, "already": False}

    async def rewrite(self, note_id: str, recording_id: str) -> None:
        started = self.context.read(STATE + note_id, {}).get("started_at", _now())
        try:
            async with self.turn:
                outcome = await self.write_again(note_id, recording_id)
            self.context.write(STATE + note_id, {**outcome, "started_at": started})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            rule = getattr(exc, "rule", "")
            if isinstance(exc, Unwritable):
                reason = str(exc)
            elif rule.startswith("writeup-"):
                reason = failure_reason(rule, getattr(exc, "detail", ""))
            elif isinstance(exc, StoreError):
                reason = "The note could not be read or saved. Try again."
            else:
                log.exception("write-up again failed for %s", note_id)
                reason = "The write-up could not be finished. Try again."
            self.context.write(
                STATE + note_id,
                {
                    "state": "failed",
                    "started_at": started,
                    "finished_at": _now(),
                    "error": reason + " The note is unchanged.",
                },
            )

    async def write_again(self, note_id: str, recording_id: str) -> dict[str, Any]:
        from coppermind_store.recording_writeup import generate

        context = self.context
        recordings = context.recordings
        job = recordings.load(recording_id)
        key = "recording-writeups/" + job.id
        saved = context.read(key, {})
        transcript = saved.get("transcript")
        if not transcript:
            raise Unwritable(
                "The saved transcript for this recording could not be found, so it cannot be "
                "written up again."
            )
        # The names the owner has given voices since the first write-up carry over.
        current = context.read("voice-mappings/" + job.id, {})
        then = saved.get("voice_map", {})
        renames = {
            then[label]: current[label]
            for label in then
            if current.get(label) not in {None, then[label]}
        }
        if renames:
            transcript = {
                **transcript,
                "segments": [
                    {**s, "speaker": renames.get(s.get("speaker"), s.get("speaker"))}
                    for s in transcript["segments"]
                ],
            }
        voice_map = {label: current.get(label, name) for label, name in then.items()}
        # The write-up is made under its own name, so the one the note was filed from is
        # untouched unless the new one is good.
        trial = job.model_copy(update={"id": job.id + "-again"})
        try:
            fresh = await generate(context, trial, transcript, voice_map)
        finally:
            for name in ("recording-writeups/", "recording-writeup-parts/"):
                (context.root / f"{name}{trial.id}.json").unlink(missing_ok=True)
        body = await recordings.with_references(job, fresh, fresh["body"])
        manifest = await context.store.get_source(job.source_id)
        link = str(manifest.projection_path or "").removesuffix(".md")
        from coppermind.recording_writeup import SOURCE_LINK_TOKEN

        body = body.replace(SOURCE_LINK_TOKEN, f"[[{link}]]")
        note = await context.store.get_note(note_id)
        baseline = saved.get("voice_body", saved.get("filed_body", ""))
        finished = _now()
        if note.body.strip() == body.strip():
            return {"state": "done", "finished_at": finished}
        edited = note.body.strip() != baseline.strip()
        if edited:
            context.write(PROPOSED + note_id, {**fresh, "filed_body": body})
            difference = list(
                difflib.unified_diff(
                    note.body.splitlines(),
                    body.splitlines(),
                    "your note",
                    "new write-up",
                    lineterm="",
                    n=1,
                )
            )[:DIFF_LINES]
            return {
                "state": "kept",
                "finished_at": finished,
                "if_match": note.content_hash,
                "difference": difference,
            }
        await self.put(note, job, saved, fresh, body)
        return {"state": "done", "finished_at": finished}

    async def put(
        self, note: Any, job: Any, saved: dict[str, Any], fresh: dict[str, Any], body: str
    ) -> None:
        """Make the note say the new write-up, keeping the version it replaces."""
        from coppermind_store.recordings import RecordingBoundary

        context = self.context
        earlier = [*saved.get("earlier_bodies", []), {"body": note.body, "replaced_at": _now()}]
        await context.store.replace_note(
            note.id,
            ReplaceNote(frontmatter=note.frontmatter, body=body),
            if_match=note.content_hash,
        )
        context.write(
            "recording-writeups/" + job.id,
            {**fresh, "filed_body": body, "earlier_bodies": earlier[-EARLIER_KEPT:]},
        )
        (context.root / f"{PROPOSED}{note.id}.json").unlink(missing_ok=True)
        job.boundaries = [RecordingBoundary.model_validate(b) for b in fresh["boundaries"]]
        context.recordings.save(job)

    async def replace(self, row: Note, job: Any, if_match: str) -> dict[str, Any]:
        """The owner chose the new write-up over their edited note."""
        context = self.context
        record = context.read(STATE + row.id, {})
        proposed = context.read(PROPOSED + row.id, {})
        if record.get("state") != "kept" or not proposed:
            raise ValueError("There is no new write-up waiting. Write it up again first.")
        note = await context.store.get_note(row.id)
        if note.content_hash != if_match or note.content_hash != record.get("if_match"):
            raise ValueError("The note changed since this write-up was made. Write it up again.")
        saved = context.read("recording-writeups/" + job.id, {})
        body = proposed.pop("filed_body")
        await self.put(note, job, saved, proposed, body)
        context.write(STATE + row.id, {"state": "done", "finished_at": _now()})
        return {"started": False, "replaced": True}
