"""The notes waiting in Review: one list of them, and what an owner asks of a card.

*Resummarize* writes a recording's note up again from the transcript already saved, with no new
transcription. It fills the note's properties again from the new write-up, but only the ones the
owner has not put their own value in (see `filled_in`); it never changes whether the note is
reviewed.

A note written up in one step has its write-up as its body. A rewrite replaces that body while it
is still exactly what was generated, and the version it replaces is kept. A body the owner edited
is replaced only when the request says `overwrite`, which the companion sends after one short
confirm; the replaced version is kept the same way. A failed rewrite leaves the note as it was.

One run can use another model than the configured one (`model`); nothing else changes.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from coppermind.db.models import Note
from coppermind.ids import is_valid_id
from coppermind.naming import sanitize_folder
from coppermind.recording_writeup import failure_reason
from coppermind.store_protocol import (
    MoveNote,
    NotFound,
    PatchFrontmatter,
    RenameNote,
    ReplaceNote,
    StoreError,
)
from coppermind_store.note_lifecycle import SETTLING

if TYPE_CHECKING:
    from coppermind_store.context_service import ContextService

log = logging.getLogger("coppermind.store.review_notes")

STATE = "note-resummarize/"
PAGE = 100
MAX_PAGE = 200
EARLIER_KEPT = 5
EDITED = "You edited this note while it was being written up, so it was left as it is."


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _instant(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _distance(event: dict[str, Any], captured: datetime) -> float:
    start = _instant(event.get("start"))
    return abs((start - captured).total_seconds()) if start else float("inf")


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
        """Every note in the Review folder, with where its write-up stands."""
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
            "writeup_on": writeup_on,
            "choices": self.choices(),
        }

    def choices(self) -> dict[str, Any]:
        """What a card's pickers offer, and the names of the properties they write."""
        from coppermind_store.recording_writeup import vocabulary

        context = self.context
        schema = context.store.control.schema()
        vocab = vocabulary(context)
        assignment = context.config().assignments["enrichment"]
        return {
            "keys": {
                "type": schema.role("type_key"),
                "account": schema.role("account_key"),
                "tags": schema.role("tags_key"),
                "reviewed": schema.role("reviewed_key"),
                "meeting": assignment.event_field or "calendar_event",
                "attendees": assignment.attendees_field or "attendees",
            },
            "types": vocab["types"],
            "accounts": sorted(set(vocab["accounts"].values()), key=str.casefold),
            "tags": sorted(set(vocab["tags"].values()), key=str.casefold),
            "tags_closed": schema.tag_mode == "closed",
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
        }
        if row.reviewed and (problem := self.filing_problem(row.id)):
            item["filing"] = {"problem": problem}
        item["writeup"] = self.writeup_step(row.id, job, enrichment)
        if job is not None and not row.reviewed:
            try:
                note = await self.context.store.get_note(row.id)
            except StoreError:
                note = None
            if note is not None:
                item["edited"] = self.edited(note, job)
                item["meeting"] = await self.meeting(note, job)
        return item

    def edited(self, note: Any, job: Any) -> bool:
        """Whether the owner has changed the body Coppermind wrote."""
        if job.generation_mode != "markdown-v1":
            return False
        saved = self.context.read("recording-writeups/" + job.id, {})
        baseline = saved.get("voice_body", saved.get("filed_body"))
        return baseline is not None and note.body.strip() != baseline.strip()

    async def meeting(self, note: Any, job: Any) -> dict[str, Any] | None:
        """The meeting the note names and the meetings of that day, nearest the recording first."""
        context = self.context
        field = context.config().assignments["enrichment"].event_field or "calendar_event"
        try:
            events = await context.calendar_candidates(note)
        except Exception:  # noqa: BLE001
            return None
        selected = note.frontmatter.get(field) or ""
        captured = _instant(job.captured_at)
        if captured is not None:
            events = sorted(events, key=lambda e: _distance(e, captured))
        return {
            "selected": selected if isinstance(selected, str) else "",
            "candidates": [
                {
                    "event_id": e["event_id"],
                    "title": e.get("title", ""),
                    "start": e.get("start", ""),
                    "end": e.get("end", ""),
                }
                for e in events
            ],
        }

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

    # -- resummarize ---------------------------------------------------------------------

    async def resummarize(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Write a recording's note up again from its saved transcript.

        `model` is used for this run only. A body the owner edited is replaced only with
        `overwrite`."""
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
        model = str(payload.get("model") or "").strip()
        if len(model) > 200 or any(c in model for c in "\r\n\0"):
            raise ValueError("That is not a model this connection offers.")
        record = self.context.read(STATE + note_id, {})
        if record.get("state") == "running" and note_id in self.live:
            return {"started": True, "already": True}
        if not self.writeup_ready():
            raise ValueError(
                "Recordings are not set up to be written up. Choose a writing service and turn "
                "write-ups on in Coppermind Admin, under How recordings are written up."
            )
        self.context.write(STATE + note_id, {"state": "running", "started_at": _now()})
        task = asyncio.create_task(
            self.rewrite(note_id, job.id, model, payload.get("overwrite") is True),
            name="coppermind-resummarize",
        )
        self.live[note_id] = task
        task.add_done_callback(lambda _: self.live.pop(note_id, None))
        return {"started": True, "already": False}

    async def rewrite(
        self, note_id: str, recording_id: str, model: str = "", overwrite: bool = False
    ) -> None:
        started = self.context.read(STATE + note_id, {}).get("started_at", _now())
        try:
            async with self.turn:
                outcome = await self.write_again(note_id, recording_id, model, overwrite)
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

    async def write_again(
        self, note_id: str, recording_id: str, model: str = "", overwrite: bool = False
    ) -> dict[str, Any]:
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
            fresh = await generate(context, trial, transcript, voice_map, model=model)
        finally:
            for name in ("recording-writeups/", "recording-writeup-parts/"):
                (context.root / f"{name}{trial.id}.json").unlink(missing_ok=True)
        body = await recordings.with_references(job, fresh, fresh["body"])
        manifest = await context.store.get_source(job.source_id)
        link = str(manifest.projection_path or "").removesuffix(".md")
        from coppermind.recording_writeup import SOURCE_LINK_TOKEN, SOURCE_TARGET_TOKEN

        body = body.replace(SOURCE_LINK_TOKEN, f"[[{link}]]").replace(SOURCE_TARGET_TOKEN, link)
        note = await context.store.get_note(note_id)
        if not self.in_review(note):
            return {
                "state": "failed",
                "finished_at": _now(),
                "error": "The note was reviewed or moved out of Review while it was being "
                "written up, so the new write-up was not applied. The note is unchanged.",
            }
        note = await self.refill(note, fresh)
        baseline = saved.get("voice_body", saved.get("filed_body", ""))
        finished = _now()
        if note.body.strip() == body.strip():
            return {"state": "done", "finished_at": finished}
        if note.body.strip() != baseline.strip() and not overwrite:
            return {"state": "failed", "finished_at": finished, "error": EDITED}
        await self.put(note, job, saved, fresh, body)
        return {"state": "done", "finished_at": finished}

    async def refill(self, note: Any, written: dict[str, Any]) -> Any:
        """Fill the note's properties from the new write-up where the owner has not put their
        own value, and return the note as it now stands."""
        from coppermind_store import filled_in

        context = self.context
        changes = filled_in.fill(
            context,
            note.frontmatter,
            filled_in.answers(context, written),
            filled_in.recorded(context, note.id),
        )
        if not changes:
            return note
        await context.store.replace_note(
            note.id,
            ReplaceNote(frontmatter={**note.frontmatter, **changes}, body=note.body),
            if_match=note.content_hash,
        )
        before = note
        note = await context.store.get_note(note.id)
        filled_in.remember(context, note.id, note.frontmatter, changes)
        await filled_in.keep_job_current(context, before, note)
        return note

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
        context.write(
            "recording-writeups/" + job.id,
            {**fresh, "filed_body": body, "earlier_bodies": earlier[-EARLIER_KEPT:]},
        )
        job.boundaries = [RecordingBoundary.model_validate(b) for b in fresh["boundaries"]]
        context.recordings.save(job)

    # -- the card's other actions --------------------------------------------------------

    async def models(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The models the write-up connection offers, and the one it uses unless told otherwise."""
        from coppermind_store.model_provider import ProviderError
        from coppermind_store.recording_writeup import assignment

        context = self.context
        async with context.lock:
            limits = assignment(context)
        offered: list[str] = []
        problem = ""
        if limits.connection:
            try:
                offered = await context.provider.models(
                    context.connection(limits.connection), context.key(limits.connection)
                )
            except (ProviderError, KeyError, ValueError):
                problem = "The AI server did not list its models."
        if limits.model and limits.model not in offered:
            offered = [limits.model, *offered]
        return {"default": limits.model, "models": offered, "problem": problem}

    async def set_meeting(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The owner picked another meeting for a waiting note.

        The note names the meeting, the people on its invitation fill in the attendees where
        the owner has not put their own, and the title follows the meeting unless the owner
        changed the title."""
        import re

        from coppermind_store import filled_in
        from coppermind_store.recording_writeup import clean_title

        context = self.context
        note_id, event_id = str(payload.get("id", "")), str(payload.get("event_id", ""))
        if not is_valid_id(note_id):
            raise ValueError("That note is no longer listed.")
        job = next((j for j in context.recordings.jobs() if j.note_id == note_id), None)
        note = await context.store.get_note(note_id)
        if job is None or not self.in_review(note):
            raise ValueError("This note is not waiting for review.")
        events = await context.calendar_candidates(note)
        event = next((e for e in events if e["event_id"] == event_id), None)
        if event is None:
            raise ValueError("That meeting is not on the calendar for this day.")
        assignment = context.config().assignments["enrichment"]
        field = assignment.event_field or "calendar_event"
        attendees = assignment.attendees_field or "attendees"
        previous = next((e for e in events if e["event_id"] == note.frontmatter.get(field)), None)
        saved = context.read("recording-writeups/" + job.id, {})
        found = filled_in.attendees(
            context, {"voice_map": saved.get("voice_map", {}), "calendar": {"event": event}}
        )
        changes: dict[str, Any] = {field: event_id}
        if found:
            changes.update(
                filled_in.fill(
                    context,
                    note.frontmatter,
                    {attendees: found},
                    filled_in.recorded(context, note_id),
                )
            )
        await context.store.patch_frontmatter(
            note_id, PatchFrontmatter(set=changes), if_match=note.content_hash
        )
        for key in changes:
            if key != field:
                after = await context.store.get_note(note_id)
                filled_in.remember(context, note_id, after.frontmatter, [key])
        # The title follows the meeting only while it is still the one Coppermind made.
        stamp = re.match(r"\d{4}-\d{2}-\d{2}(?= )", note.title)
        if stamp:

            def titled(name: str) -> str:
                name = re.sub(r"^\d{4}-\d{2}-\d{2}\s+", "", name.strip())
                return f"{stamp[0]} {' '.join(name.split()).lstrip('# ') or 'Recording'}"

            from datetime import date

            made = (
                titled(previous["title"])
                if previous
                else f"{stamp[0]} {clean_title(job.title, date.fromisoformat(stamp[0]))}"
            )
            if note.title == made and titled(event["title"]) != note.title:
                current = await context.store.get_note(note_id)
                await context.store.rename_note(
                    note_id, RenameNote(title=titled(event["title"])), current.content_hash
                )
        return {"meeting": event_id}

    async def reopen(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Undo Reviewed: the note goes back to Review, waiting to be checked again."""
        context = self.context
        note_id = str(payload.get("id", ""))
        if not is_valid_id(note_id):
            raise ValueError("That note is no longer listed.")
        note = await context.store.get_note(note_id)
        reviewed = context.store.control.schema().role("reviewed_key")
        folder = sanitize_folder(context.store.control.settings().notes.review_folder)
        if not folder:
            raise ValueError("There is no Review folder to put the note back in.")
        if note.frontmatter.get(reviewed) is True:
            note = await context.store.patch_frontmatter(
                note_id, PatchFrontmatter(set={reviewed: False}), if_match=note.content_hash
            )
        if not note.path.startswith(f"{folder}/"):
            note = await context.store.move_note(note_id, MoveNote(target_folder=folder), None)
        # A receipt of the earlier filing would make the next one look like a hand move.
        state = context.store.control.store.state_dir
        for kind in ("filing", "reviews"):
            (state / kind / f"{note_id}.json").unlink(missing_ok=True)
        return {"reopened": True, "path": note.path}
