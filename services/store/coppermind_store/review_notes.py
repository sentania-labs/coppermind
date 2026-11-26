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
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from coppermind.db.models import Note
from coppermind.ids import is_valid_id
from coppermind.naming import sanitize_folder
from coppermind.recording_writeup import failure_reason
from coppermind.store_protocol import NotFound, ReplaceNote, StoreError
from coppermind_store.note_lifecycle import SETTLING

if TYPE_CHECKING:
    from coppermind_store.context_service import ContextService

log = logging.getLogger("coppermind.store.review_notes")

STATE = "note-resummarize/"
# The few facts the list needs about a recording's write-up, kept small so the list never opens
# the saved write-up, which holds the whole transcript.
STATUS = "recording-writeup-status/"
PROPOSED = "recording-writeup-proposed/"
PAGE = 100
MAX_PAGE = 200
DIFF_LINES = 400
EARLIER_KEPT = 5
SAME_BODY = "The note already says what the new write-up says."


def write_status(context: Any, recording_id: str, saved: dict[str, Any]) -> None:
    """Record, when a write-up is made, whether it has suggestions or why it has none."""
    context.write(
        STATUS + recording_id,
        {
            "suggestions": bool(saved.get("suggestions")),
            "suggestion_error": str(saved.get("suggestion_error") or ""),
        },
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _step(state: str, reason: str = "", when: str = "", **more: Any) -> dict[str, Any]:
    return {"state": state, "reason": reason, "when": when, **more}


# What earlier releases recorded when a work note had no start time. Nothing holds a note back
# for that now, so a journal that still says it is waiting for the next pass, not a failure.
RETIRED_REASONS = ("confirm the meeting time", "enter a valid local meeting time")


def filing_sentence(error: str) -> str:
    """One plain sentence for a reviewed note that could not be filed, from the recorded error."""
    cause = error.split(":", 1)[0]
    if cause == "ValueError" and any(
        text in error
        for text in ("a note date is required", "expected a valid date", "without a UTC offset")
    ):
        return "This note could not be filed because it has no date Coppermind can read."
    if cause in {"NoteUnparseable", "FrontmatterError"}:
        return "This note could not be filed because Coppermind could not read it."
    if cause.endswith("FilesystemUnavailable") or cause in {"OSError", "PermissionError"}:
        return "This note could not be filed because its folder could not be written to."
    if cause in {"MetadataUnavailable", "StoreUnavailable", "VersionConflict"}:
        return "This note could not be filed because Coppermind's records were busy."
    return "This note could not be filed."


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
        limit, start = payload.get("limit", PAGE), payload.get("cursor") or "0"
        if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
            raise ValueError(f"limit: use a number from 1 to {MAX_PAGE}")
        if not str(start).isdigit():
            raise ValueError("cursor: use the next_cursor an earlier answer gave")
        if not folder:
            return {
                "notes": [],
                "total": 0,
                "next_cursor": None,
                "suggestions_on": False,
                "writeup_on": False,
            }
        where = (Note.state == "ok", Note.path.startswith(f"{folder}/"))
        async with context.store.session_factory() as session:
            total = await session.scalar(sa.select(sa.func.count()).select_from(Note).where(*where))
            rows = list(
                (
                    await session.scalars(
                        sa.select(Note)
                        .where(*where)
                        .order_by(Note.first_seen_at.desc(), Note.id)
                        .offset(int(start))
                        .limit(limit)
                    )
                ).all()
            )
        recordings = {
            job.note_id: job for job in context.recordings.jobs() if job.note_id and job.source_id
        }
        latest = context.recordings.latest_enrichment()
        suggestions_on = bool(context.config().assignments["enrichment"].enabled)
        writeup_on = self.writeup_ready()
        notes = []
        for row in rows:
            job = recordings.get(row.id)
            notes.append(await self.row(row, job, latest.get(row.id), suggestions_on, writeup_on))
        following = int(start) + len(rows)
        return {
            "notes": notes,
            "total": total or 0,
            "next_cursor": str(following) if following < (total or 0) and rows else None,
            "suggestions_on": suggestions_on,
            "writeup_on": writeup_on,
        }

    def writeup_ready(self) -> bool:
        from coppermind_store.recording_writeup import assignment

        try:
            limits = assignment(self.context)
        except Exception:  # noqa: BLE001
            return False
        return bool(limits.enabled and not limits.paused and limits.connection)

    async def row(
        self, row: Note, job: Any, enrichment: Any, suggestions_on: bool, writeup_on: bool = True
    ) -> dict[str, Any]:
        item: dict[str, Any] = {
            "id": row.id,
            "title": row.title or row.path.rsplit("/", 1)[-1].removesuffix(".md"),
            "path": row.path,
            "date": (job.captured_at if job and job.captured_at else "")
            or (row.date.isoformat() if row.date else ""),
            "from_recording": job is not None,
            "can_resummarize": job is not None
            and not row.reviewed
            and (writeup_on if job.generation_mode == "markdown-v1" else suggestions_on),
            "can_enrich": suggestions_on and not row.reviewed,
        }
        if row.reviewed and (problem := self.filing_problem(row.id)):
            item["filing"] = {"problem": problem}
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

    def filing_problem(self, note_id: str) -> str:
        """What stopped a reviewed note from being filed, in a sentence, or nothing.

        A short wait for the file to stop changing is not a problem, and a note that has not
        failed is never listed."""
        journal = self.context.store.control.store.state_dir / "filing" / f"{note_id}.json"
        try:
            error = str(json.loads(journal.read_bytes()).get("error") or "")
        except (OSError, ValueError):
            return ""
        if (
            not error
            or error.startswith("SourceRevisionNeedsReview")
            or SETTLING in error
            or any(old in error for old in RETIRED_REASONS)
        ):
            return ""
        return filing_sentence(error)

    async def file_again(self, payload: dict[str, Any]) -> dict[str, Any]:
        """File a reviewed note straight away, and say what became of it."""
        note_id = str(payload.get("id", ""))
        if not is_valid_id(note_id):
            raise ValueError("That note is no longer listed.")
        async with self.context.store.session_factory() as session:
            row = await session.get(Note, note_id)
        if row is None or row.state != "ok":
            raise NotFound(note_id)
        result = await self.context.store.admin.notes.check_again({"id": note_id})
        return {
            "filed": bool(result.get("filed")),
            "problem": "" if result.get("filed") else self.filing_problem(note_id),
        }

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
        # A note filed from a write-up exists only once the write-up was written.
        if job.state != "succeeded":
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
            status = self.context.read(STATUS + job.id, {})
            if status.get("suggestions"):
                return _step("written", when=job.succeeded_at)
            if status.get("suggestion_error"):
                return _step("failed", status["suggestion_error"], job.succeeded_at)
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
        if not self.in_review(note):
            return {
                "state": "failed",
                "finished_at": _now(),
                "error": "The note was reviewed or moved out of Review while it was being "
                "written up, so the new write-up was not applied. The note is unchanged.",
            }
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

    def in_review(self, note: Any) -> bool:
        """Whether the note is still waiting in Review: not reviewed, not moved elsewhere."""
        reviewed = self.context.store.control.schema().role("reviewed_key")
        return note.frontmatter.get(reviewed) is not True and self.context.in_review_folder(
            note.path
        )

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
        write_status(context, job.id, fresh)
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
        if not self.in_review(note):
            raise ValueError(
                "This note has been reviewed or moved out of Review, so it is left as it is."
            )
        saved = context.read("recording-writeups/" + job.id, {})
        body = proposed.pop("filed_body")
        await self.put(note, job, saved, proposed, body)
        context.write(STATE + row.id, {"state": "done", "finished_at": _now()})
        return {"started": False, "replaced": True}
