"""One-shot suggestions, explicit review completion and rebuildable confirmed facts."""

from __future__ import annotations

import asyncio
import time as clock
from typing import TYPE_CHECKING, Any

from coppermind.ids import is_valid_id
from coppermind.meeting_summary import MeetingSummary
from coppermind.schema import (
    date_field_problem,
    field_suggestion_changes,
    note_date,
    note_start_time,
    parse_note_time,
)
from coppermind.store_protocol import (
    NoteDocument,
    NoteQuery,
    NotFound,
    PatchFrontmatter,
    StoreError,
)
from coppermind_store import meeting_summary, speaker_names

if TYPE_CHECKING:
    from coppermind_store.context_service import ContextService


def validate_review(
    note: NoteDocument, schema: Any, default_timezone: str = "America/Chicago"
) -> None:
    """Validate a note's date and start time for review completion and filing.

    `date` carries the day, and the local start time too when it is a
    date-and-time (`YYYY-MM-DDTHH:MM:SS`, no UTC offset, in the configured
    timezone). A `date` carrying a UTC offset is rejected. A work note needs a
    start time, from `date` or from a legacy `HH:MM` `time`, unless
    `time_unknown: true`, which needs only a valid day. A legacy `timezone` on
    the note is ignored. `default_timezone` is kept for callers that still pass
    it.
    """
    values = note.frontmatter
    problem = date_field_problem(values, schema)
    if problem:
        raise ValueError(problem)
    if note_date(values, schema) is None:
        raise ValueError("a note date is required in date")
    if values.get(schema.role("type_key")) != "work":
        return
    legacy = parse_note_time(values.get("time"))
    if legacy.present and not legacy.valid:
        raise ValueError("enter a valid local meeting time")
    if values.get("time_unknown") is True:
        return
    if note_start_time(values, schema) is None:
        raise ValueError("confirm the meeting time, or select Time unknown")


class NoteLifecycle:
    def __init__(self, context: ContextService) -> None:
        self.context = context
        self.lock = asyncio.Lock()
        self.observed: dict[str, tuple[str, float]] = {}
        self.calendar_generation = ""

    def _default_timezone(self) -> str:
        return self.context.store.control.settings().general.timezone

    async def merge_candidates(self, note: NoteDocument, recording: Any) -> list[dict[str, Any]]:
        """Find human notes linked to the same event on the same day."""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from coppermind_store.meeting_match import overlapping

        schema = self.context.store.control.schema()
        if (
            not self.context.in_review_folder(note.path)
            or note.frontmatter.get(schema.role("reviewed_key")) is True
        ):
            return []
        if self.context.read("recording-merge-dismissals", {}).get(recording.source_id):
            return []
        field = self.context.config().assignments["enrichment"].event_field
        choice = self.context.read("recording-meetings", {}).get(recording.source_id, {})
        event_id = choice.get("event_id") or (note.frontmatter.get(field) if field else None)
        associations = self.context.read("associations", {})
        linked_events = associations.get(note.id, {}).get("events", [])
        if not event_id and len(linked_events) == 1:
            event_id = linked_events[0]
        day = note_date(note.frontmatter, schema)
        if not event_id or day is None:
            return []
        zone = ZoneInfo(self._default_timezone())
        events = await self.context.calendar_candidates(note)

        def on_day(event: dict[str, Any]) -> bool:
            try:
                start = datetime.fromisoformat(event["start"])
                return (start.astimezone(zone) if start.tzinfo else start).date() == day
            except (KeyError, TypeError, ValueError):
                return False

        occurrences = [e for e in events if e["event_id"] == event_id and on_day(e)]
        selected_occurrence = None
        if len(occurrences) > 1:
            captured = getattr(recording, "captured_at", "")
            if not captured and (start := note_start_time(note.frontmatter, schema)):
                captured = datetime.combine(day, start, zone).isoformat()
            matching = overlapping(occurrences, captured)
            if len(matching) != 1:
                return []
            selected_occurrence = matching[0]
        generated = {r.note_id for r in self.context.recordings.jobs() if r.note_id}
        matches = []
        cursor = None
        while True:
            page = await self.context.store.list_notes(
                NoteQuery.model_validate(
                    {"from": day, "to": day, "state": "ok", "limit": 200, "cursor": cursor}
                )
            )
            for item in page.items:
                if item.id == note.id or item.id in generated:
                    continue
                candidate = await self.context.store.get_note(item.id)
                if selected_occurrence is not None:
                    start = note_start_time(candidate.frontmatter, schema)
                    if not start or not overlapping(
                        [selected_occurrence], datetime.combine(day, start, zone).isoformat()
                    ):
                        continue
                source_opening = False
                for source_id in candidate.sources:
                    if not is_valid_id(source_id):
                        source_opening = True
                        break
                    try:
                        import json

                        manifest = json.loads(
                            (
                                self.context.store.sources_root / source_id / "manifest.json"
                            ).read_text()
                        )
                    except (OSError, ValueError):
                        source_opening = True
                        break
                    merged = self.context.read("recording-merges/" + source_id, {})
                    if manifest.get("note_id") == candidate.id and not (
                        merged.get("state") == "complete"
                        and merged.get("target_id") == candidate.id
                    ):
                        source_opening = True
                        break
                if source_opening:
                    continue
                linked = associations.get(item.id, {}).get("events", [])
                if event_id in linked or (field and candidate.frontmatter.get(field) == event_id):
                    matches.append(
                        {
                            "id": candidate.id,
                            "title": candidate.title,
                            "path": candidate.path,
                            "content_hash": candidate.content_hash,
                        }
                    )
            if not page.next_cursor:
                return matches
            cursor = page.next_cursor

    async def choose_recording_meeting(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Confirm an overlapping event, rechecking the note and calendar at the write."""
        async with self.lock:
            note = await self.context.store.get_note(payload["note_id"])
            if note.content_hash != payload.get("if_match"):
                raise ValueError("note changed; refresh the review")
            if await self.context.calendar_evidence_hash(note) != payload.get("calendar_hash"):
                raise ValueError("calendar changed; refresh the review")
            recording = next(
                (r for r in self.context.recordings.jobs() if r.note_id == note.id and r.source_id),
                None,
            )
            if not recording:
                raise ValueError("recording is no longer attached to this note")
            event_id = payload.get("event_id")
            candidates = await self.context.recording_calendar_candidates(note)
            selected_event = next(
                (event for event in candidates if event["event_id"] == event_id), None
            )
            if selected_event is None:
                raise ValueError("choose an event from the current matching meetings")
            async with self.context.lock:
                choices = self.context.read("recording-meetings", {})
                previous_choice = choices.get(recording.source_id, {})
                previous = previous_choice.get("event_id")
                associations = self.context.read("associations", {})
                record = associations.setdefault(
                    note.id, {"events": [], "contacts": [], "attendees": []}
                )
                if any(value not in {previous, event_id} for value in record.get("events", [])):
                    raise ValueError("the note already names a different meeting; edit it first")
                field = self.context.config().assignments["enrichment"].event_field
                existing = note.frontmatter.get(field) if field else None
                if existing and existing not in {previous, event_id}:
                    raise ValueError("the note already names a different meeting; edit it first")
                title = str(selected_event.get("title", "")).strip()
                old_title = previous_choice.get("title") or recording.title
                prefix = "# " + old_title + "\n"
                body = note.body
                if title and not any(c in title for c in "\r\n\0") and body.startswith(prefix):
                    body = "# " + title + "\n" + body[len(prefix) :]
                if body != note.body:
                    import base64

                    from coppermind.store_protocol import ReplaceNote
                    from coppermind_store.fs import content_hash

                    data = (await self.context.store._locate(note.id))[1].read_bytes()
                    if content_hash(data) != note.content_hash:
                        raise ValueError("note changed; refresh the review")
                    self.context.write(
                        "recording-meeting-history/"
                        + recording.source_id
                        + "/"
                        + note.content_hash.removeprefix("sha256:"),
                        {
                            "note_id": note.id,
                            "path": note.path,
                            "content": base64.b64encode(data).decode(),
                        },
                    )
                    frontmatter = dict(note.frontmatter)
                    if field:
                        frontmatter[field] = event_id
                    note = await self.context.store.replace_note(
                        note.id,
                        ReplaceNote(frontmatter=frontmatter, body=body),
                        if_match=note.content_hash,
                    )
                elif field and existing != event_id:
                    note = await self.context.store.patch_frontmatter(
                        note.id, PatchFrontmatter(set={field: event_id}), if_match=note.content_hash
                    )
                record["events"] = [event_id]
                self.context.write("associations", associations)
                choices[recording.source_id] = {"event_id": event_id, "title": title}
                self.context.write("recording-meetings", choices)
            queued = False
            if getattr(self.context.config().assignments["enrichment"], "enabled", False):
                try:
                    await self.context.queue_note(note.id)
                    queued = True
                except (ValueError, StoreError):
                    pass
            return {
                "note_id": note.id,
                "event_id": event_id,
                "note_hash": note.content_hash,
                "suggestions_queued": queued,
            }

    async def dismiss_recording_merge(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self.lock:
            note = await self.context.store.get_note(payload["note_id"])
            if note.content_hash != payload.get("if_match"):
                raise ValueError("note changed; refresh the review")
            recording = next(
                (r for r in self.context.recordings.jobs() if r.note_id == note.id and r.source_id),
                None,
            )
            if not recording:
                raise ValueError("there is no current merge offer")
            if self.context.read("recording-merge-dismissals", {}).get(recording.source_id):
                return {"dismissed": True}
            if not await self.merge_candidates(note, recording):
                raise ValueError("there is no current merge offer")
            async with self.context.lock:
                dismissed = self.context.read("recording-merge-dismissals", {})
                dismissed[recording.source_id] = True
                self.context.write("recording-merge-dismissals", dismissed)
            return {"dismissed": True}

    async def merge_recording(self, payload: dict[str, Any]) -> dict[str, Any]:
        import base64

        from coppermind_store import artifacts, recording_merge
        from coppermind_store.fs import content_hash
        from coppermind_store.recordings import SUMMARY_PENDING, SUMMARY_PENDING_EARLIER

        async with self.lock:
            note = await self.context.store.get_note(payload["note_id"])
            recording = next(
                (r for r in self.context.recordings.jobs() if r.note_id == note.id and r.source_id),
                None,
            )
            if not recording:
                raise ValueError("The recording is no longer available for this note.")
            key = "recording-merges/" + recording.source_id
            receipt = self.context.read(key, {})
            if receipt:
                if receipt["target_id"] != payload.get("target_id"):
                    raise ValueError("This recording already has a merge in progress.")
                result = await recording_merge.apply(self.context, receipt)
                return {"state": result["state"], "target_id": result["target_id"]}
            if payload.get("confirmation") != "Merge recording":
                raise ValueError("Confirm the merge before continuing.")
            if note.content_hash != payload.get("if_match"):
                raise ValueError("The recording note changed. Refresh the review.")
            candidates = await self.merge_candidates(note, recording)
            candidate = next((c for c in candidates if c["id"] == payload.get("target_id")), None)
            if not candidate or candidate["content_hash"] != payload.get("target_hash"):
                raise ValueError("The matching note changed. Refresh the review.")
            job = self.context.newest_job(note.id)
            if (
                not job
                or job.id != payload.get("job_id")
                or job.state != "suggested"
                or not job.result
                or not job.result.meeting_summary
                or job.note_hash != note.content_hash
            ):
                raise ValueError("The recording summary is not ready to merge. Refresh the review.")
            summary = MeetingSummary.model_validate(payload.get("meeting_summary"))
            manifest = await self.context.store.get_source(recording.source_id)
            if (
                job.source_versions.get(recording.source_id) != manifest.current_revision
                or job.evidence_hash != await self.context.note_evidence_hash(note)
            ):
                raise ValueError("The recording or calendar changed. Refresh the review.")
            recording_merge.append_summary("", summary, manifest.projection_path or "")
            paths = await artifacts.known_paths(self.context.store, [recording.artifact_id])
            artifact = artifacts.read_manifest(self.context.store, recording.artifact_id)
            artifact_link = artifacts.link_for(
                paths.get(recording.artifact_id), str(artifact["title"]), artifact.get("url")
            )
            projection = str(manifest.projection_path).removesuffix(".md")
            chosen_title = (
                self.context.read("recording-meetings", {})
                .get(recording.source_id, {})
                .get("title")
            )
            expected_bodies = {
                artifacts.body_with_links(
                    f"# {title}\n\n[[{projection}]]\n{pending}", [artifact_link]
                ).strip()
                for pending in (SUMMARY_PENDING, SUMMARY_PENDING_EARLIER)
                for title in {recording.title, chosen_title}
                if title
            }
            if note.body.strip() not in expected_bodies:
                raise ValueError("The recording note has edits. Move them before merging.")
            generated_path = (await self.context.store._locate(note.id))[1]
            target = await self.context.store.get_note(candidate["id"])
            target_path = (await self.context.store._locate(target.id))[1]
            generated_bytes, target_bytes = generated_path.read_bytes(), target_path.read_bytes()
            if (
                content_hash(generated_bytes) != note.content_hash
                or content_hash(target_bytes) != target.content_hash
                or target.content_hash != candidate["content_hash"]
            ):
                raise ValueError("A note changed. Refresh the review.")
            marker = f"<!-- coppermind-recording:{recording.source_id} -->"
            if marker in target.body:
                raise ValueError("This note already contains a recording merge marker.")
            manifest_bytes = (
                self.context.store.sources_root / recording.source_id / "manifest.json"
            ).read_bytes()
            receipt = {
                "state": "applying",
                "step": "prepared",
                "source_id": recording.source_id,
                "source_revision": manifest.current_revision,
                "generated_id": note.id,
                "artifact_id": recording.artifact_id,
                "generated_path": note.path,
                "generated_hash": note.content_hash,
                "generated_snapshot": base64.b64encode(generated_bytes).decode(),
                "target_id": target.id,
                "target_hash": target.content_hash,
                "target_snapshot": base64.b64encode(target_bytes).decode(),
                "manifest_snapshot": base64.b64encode(manifest_bytes).decode(),
                "summary": summary.model_dump(),
                "projection_path": manifest.projection_path,
            }
            self.context.write(key, receipt)
            result = await recording_merge.apply(self.context, receipt)
            return {"state": result["state"], "target_id": result["target_id"]}

    async def resume_recording_merges(self) -> None:
        from sqlalchemy.exc import SQLAlchemyError

        from coppermind_store import recording_merge

        async with self.lock:
            for path in sorted((self.context.root / "recording-merges").glob("*.json")):
                if not is_valid_id(path.stem):
                    continue
                try:
                    receipt = self.context.read("recording-merges/" + path.stem, {})
                except (ValueError, OSError):
                    continue
                if receipt.get("state") != "applying":
                    continue
                try:
                    await recording_merge.apply(self.context, receipt)
                except (
                    ValueError,
                    StoreError,
                    OSError,
                    SQLAlchemyError,
                    KeyError,
                    TypeError,
                ) as exc:
                    if isinstance(exc, (ValueError, NotFound, FileNotFoundError)):
                        receipt["state"] = "attention"
                    receipt["error"] = (
                        str(exc)
                        if isinstance(exc, ValueError)
                        else (
                            "A note or recording is unavailable. It was kept; "
                            "try again when it returns."
                        )
                    )
                    self.context.write("recording-merges/" + path.stem, receipt)

    async def retry_recording_merge(self, payload: dict[str, Any]) -> dict[str, Any]:
        from coppermind_store import recording_merge

        async with self.lock:
            note = await self.context.store.get_note(payload["note_id"])
            if note.content_hash != payload.get("if_match"):
                raise ValueError("The recording note changed. Refresh the review.")
            recording = next(
                (r for r in self.context.recordings.jobs() if r.note_id == note.id and r.source_id),
                None,
            )
            if not recording:
                raise ValueError("The recording is unavailable.")
            key = "recording-merges/" + recording.source_id
            receipt = self.context.read(key, {})
            if receipt.get("state") not in {"attention", "applying", "complete"}:
                raise ValueError("There is no recording merge to retry.")
            if receipt.get("state") != "complete":
                receipt["state"] = "applying"
                self.context.write(key, receipt)
            try:
                result = await recording_merge.apply(self.context, receipt)
            except (NotFound, FileNotFoundError) as exc:
                receipt["state"] = "attention"
                receipt["error"] = (
                    "The destination note is unavailable. The recording note was kept."
                )
                self.context.write(key, receipt)
                raise ValueError(receipt["error"]) from exc
            target = await self.context.store.get_note(result["target_id"])
            return {
                "state": result["state"],
                "target_id": target.id,
                "target_path": target.path,
                "target_title": target.title,
            }

    async def detail(self, payload: dict[str, Any]) -> dict[str, Any]:
        from coppermind_store.context_service import fingerprint

        note = await self.context.store.get_note(payload["note_id"])
        jobs = sorted(
            [j for j in self.context.jobs() if j.note_id == note.id and j.purpose != "dossier"],
            key=lambda j: j.created_at,
            reverse=True,
        )
        job = jobs[0] if jobs else None
        suggestions = []
        if job and job.result:
            decided = {d.get("index") for d in job.decisions}
            suggestions = [
                dict(s.model_dump(), index=i)
                for i, s in enumerate(job.result.suggestions)
                if i not in decided
            ]
        people = self.context.store.entities.load().entities
        for suggestion in suggestions:
            labels = []
            for event in job.evidence.get("events", []) if job else []:
                if event["event_id"] in suggestion["event_ids"]:
                    labels.append(event["title"])
                for person in event.get("people", []):
                    if person["contact_id"] in suggestion["contact_ids"]:
                        labels.append(person["address"])
            if suggestion["kind"] == "speaker":
                saved = (
                    (job.evidence.get("speaker_review") or {}).get("candidates", []) if job else []
                )
                candidate_list = [
                    current
                    for candidate in saved
                    if (current := speaker_names.current_person(people, candidate)) is not None
                ]
                if suggestion["value"] and not any(
                    candidate["id"] == suggestion["value"] for candidate in candidate_list
                ):
                    suggestion["value"] = None
                    suggestion["reason"] = (
                        "The suggested person is no longer active. Choose someone else "
                        "or leave this speaker unnamed."
                    )
                    suggestion["confidence"] = 0
                suggestion["candidates"] = candidate_list
                suggestion["display_value"] = next(
                    (c["name"] for c in candidate_list if c["id"] == suggestion["value"]),
                    "No person suggested",
                )
                continue
            value = suggestion["value"]
            suggestion["display_value"] = (
                ", ".join(dict.fromkeys(labels))
                if value is None
                else ", ".join(str(v) for v in value)
                if isinstance(value, list)
                else str(value)
            )
        stale = bool(job and job.note_hash != note.content_hash)
        if job and not stale:
            try:
                stale = job.evidence_hash != await self.context.note_evidence_hash(note)
            except ValueError:
                stale = True
        problem = ""
        try:
            validate_review(note, self.context.store.control.schema(), self._default_timezone())
        except ValueError as exc:
            problem = str(exc)
        from coppermind_store.reconciler import note_collisions

        copies = await note_collisions(self.context.store, note.id)
        if copies:
            problem = " ".join(dict.fromkeys([*([problem] if problem else []), *copies]))
        update = self.context.read("calendar-followups", {}).get(note.id)
        if update is not None:
            update = dict(update, candidates=await self.context.calendar_candidates(note))
        manual = None
        recording = next(
            (r for r in self.context.recordings.jobs() if r.note_id == note.id and r.source_id),
            None,
        )
        meeting = None
        merge = None
        if recording:
            candidates = await self.context.recording_calendar_candidates(note)
            suggested = [
                s.event_ids[0]
                for s in (job.result.suggestions if job and job.result and not stale else [])
                if s.kind == "event" and len(s.event_ids) == 1 and s.confidence >= 0.8
            ]
            meeting = {
                "candidates": candidates,
                "suggested_event_id": suggested[0] if len(suggested) == 1 else None,
                "calendar_hash": await self.context.calendar_evidence_hash(note),
                "selected_event_id": self.context.read("recording-meetings", {})
                .get(recording.source_id, {})
                .get("event_id"),
            }
            manual = await speaker_names.evidence(self.context, note, candidates)
            receipt = self.context.read("recording-merges/" + recording.source_id, {})
            if receipt:
                merge = {
                    "state": receipt.get("state"),
                    "error": receipt.get("error", ""),
                    "target_id": receipt.get("target_id"),
                }
            elif job and job.result and job.result.meeting_summary and not stale:
                matches = await self.merge_candidates(note, recording)
                if matches:
                    merge = {"state": "offered", "candidates": matches}
        if manual:
            from coppermind_store.voice_store import VoiceStore

            people = self.context.store.entities.load().entities
            manual = {
                key: manual[key] for key in ("source_id", "revision", "speakers", "candidates")
            }
            manual["voice_matches"] = speaker_names.voice_suggestions(self.context, note.id)
            recording = next(r for r in self.context.recordings.jobs() if r.note_id == note.id)
            voice_map = self.context.read("voice-mappings/" + recording.id, None)
            manual["voice_unavailable"] = (
                [label for label in manual["speakers"] if label.startswith("Speaker ")]
                if isinstance(voice_map, dict)
                else []
            )
            manual["voice_appearances"] = {
                voice["name"]: len(voice["assignments"])
                for voice in VoiceStore(self.context).unknown_summaries().values()
                if voice["name"] in manual["speakers"]
            }
            manual["candidates"] = [
                person
                for candidate in manual["candidates"]
                if (person := speaker_names.current_person(people, candidate)) is not None
            ]
        return {
            "manual_speaker_review": manual,
            "review_problem": problem,
            "note": note.model_dump(mode="json"),
            "job_id": job.id if job else None,
            "state": job.state if job else "awaiting_suggestions",
            "suggestions_enabled": self.context.config().assignments["enrichment"].enabled,
            "suggestion_error": job.error if job and job.state == "failed" else "",
            "stale": stale,
            "speaker_notice": speaker_names.OMITTED_NOTICE
            if job and (job.evidence.get("speaker_review") or {}).get("omitted")
            else job.notice
            if job
            else "",
            "speaker_attendees_notice": ""
            if self.context.config().assignments["enrichment"].attendees_field
            else speaker_names.ATTENDEES_UNSET_NOTICE,
            "meeting_summary": job.result.meeting_summary.model_dump()
            if job and job.result and job.result.meeting_summary
            else None,
            "suggestions": suggestions,
            "unresolved": job.result.unresolved if job and job.result else [],
            "roles": self.context.store.control.schema().roles,
            "finalization": self.context.read("finalized", {}).get(note.id),
            "calendar_update": update,
            "recording_meeting": meeting,
            "recording_merge": merge,
            "body_hash": fingerprint(note.body),
            "completion": self.context.read("review-completions/" + note.id, {}),
        }

    async def complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        from coppermind_store.context_service import fingerprint, now, same_evidence

        if not self.context.config().lifecycle_enabled:
            raise ValueError("enable the note lifecycle in Admin first")
        identity = payload.get("note_id", "")
        if not is_valid_id(identity):
            raise ValueError("invalid note identity")
        if payload.get("confirmation") != "Complete review":
            raise ValueError("explicit review completion is required")
        selected = payload.get("selected", [])
        if not isinstance(selected, list) or any(type(i) is not int or i < 0 for i in selected):
            raise ValueError("invalid suggestion selection")
        selected = sorted(set(selected))
        async with self.lock:
            name = "review-completions/" + identity
            receipt = self.context.read(name, {})
            request = {
                k: payload.get(k)
                for k in (
                    "note_id",
                    "if_match",
                    "job_id",
                    "selected",
                    "speaker_choices",
                    "manual_speakers",
                    "manual_source_revision",
                    "manual_source_id",
                    "accept_link_risk",
                )
            }
            # Keep pre-summary completion receipts replayable after an upgrade.
            if payload.get("meeting_summary") is not None:
                request["meeting_summary"] = payload["meeting_summary"]
            request_hash = fingerprint(request)
            note = await self.context.store.get_note(identity)
            schema = self.context.store.control.schema()
            if receipt.get("request") == request_hash and receipt.get("state") == "complete":
                return receipt
            if receipt.get("state") == "applying" and receipt.get("request") != request_hash:
                raise ValueError("a recorded review is unfinished; resume that operation first")
            if not receipt or receipt.get("state") != "applying":
                if note.content_hash != payload.get("if_match"):
                    raise ValueError(
                        "note changed or has not synced; reload before finishing review"
                    )
                validate_review(note, schema, self._default_timezone())
                if note.frontmatter.get(schema.role("reviewed_key")) is True:
                    raise ValueError("note is already reviewed")
                summary = payload.get("meeting_summary")
                if summary is not None:
                    summary = MeetingSummary.model_validate(summary).model_dump()
                if selected or summary is not None:
                    job = self.context.load_job(str(payload.get("job_id", "")))
                    if job.note_id != identity or not job.result:
                        raise ValueError("suggestions do not belong to this note")
                    if any(i >= len(job.result.suggestions) for i in selected):
                        raise ValueError("invalid suggestion selection")
                    if any(d.get("index") in selected for d in job.decisions):
                        raise ValueError("a selected suggestion was already decided")
                    if job.state != "suggested":
                        raise ValueError("job is not awaiting review")
                    if (
                        any(job.result.suggestions[i].kind == "filename" for i in selected)
                        and payload.get("accept_link_risk") is not True
                    ):
                        raise ValueError("confirm filename links may need manual correction")
                    if summary is not None and job.result.meeting_summary is None:
                        raise ValueError("this job has no meeting summary to review")
                    self.context.validate_result(job.result, job.evidence)
                    prospective = note.model_copy(deep=True)
                    for i in selected:
                        s = job.result.suggestions[i]
                        if s.kind == "field":
                            written, removed = field_suggestion_changes(
                                prospective.frontmatter, schema, s.field, s.value
                            )
                            prospective.frontmatter.update(written)
                            for key in removed:
                                prospective.frontmatter.pop(key, None)
                    validate_review(prospective, schema, self._default_timezone())
                    if not same_evidence(await self.context.evidence(job), job.evidence):
                        raise ValueError("source evidence changed; regenerate the suggestion")
                    selected.sort(
                        key=lambda i: (
                            job.result.suggestions[i].field in {schema.role("date_key"), "time"}
                        )
                    )
                speaker_operation = None
                if selected:
                    speaker_indexes = [
                        i for i in selected if job.result.suggestions[i].kind == "speaker"
                    ]
                    choices = payload.get("speaker_choices") or {}
                    if not isinstance(choices, dict) or any(
                        not isinstance(k, str) or not isinstance(v, str) for k, v in choices.items()
                    ):
                        raise ValueError("invalid speaker choices")
                    speaker_data = job.evidence.get("speaker_review") or {}
                    candidates = {c["id"]: c for c in speaker_data.get("candidates", [])}
                    people = self.context.store.entities.load().entities
                    mapping = {}
                    person_ids = {}
                    chosen = []
                    for i in speaker_indexes:
                        suggestion = job.result.suggestions[i]
                        person_id = choices.get(str(i), suggestion.value or "")
                        if not person_id:
                            continue
                        person = speaker_names.chosen_person(people, person_id, candidates)
                        if person is None:
                            person_name = candidates.get(person_id, {}).get("name", "This person")
                            raise ValueError(
                                f"{suggestion.field}: {person_name} is no "
                                "longer an active person. Choose someone else or leave this "
                                "speaker unnamed."
                            )
                        person = speaker_names.find_contact(self.context, person)
                        mapping[suggestion.field] = person["name"]
                        person_ids[suggestion.field] = (
                            person.get("person_id") or person["id"].removeprefix("person:")
                            if person.get("person_id") or person["id"].startswith("person:")
                            else ""
                        )
                        chosen.append(person)
                    if mapping:
                        speaker_operation = {
                            "source_id": speaker_data["source_id"],
                            "revision": speaker_data["revision"],
                            "mapping": mapping,
                            "person_ids": person_ids,
                            "people": chosen,
                        }
                        speaker_names.people_changes(self.context, prospective.frontmatter, chosen)
                    selected = [i for i in selected if i not in speaker_indexes]
                manual_choices = payload.get("manual_speakers") or {}
                if (
                    not isinstance(manual_choices, dict)
                    or any(
                        not isinstance(k, str) or not isinstance(v, str)
                        for k, v in manual_choices.items()
                    )
                    or len(manual_choices) > 30
                ):
                    raise ValueError("invalid manual speaker choices")
                manual_choices = {
                    label: person for label, person in manual_choices.items() if person
                }
                if manual_choices:
                    manual = await speaker_names.evidence(
                        self.context, note, await self.context.calendar_candidates(note)
                    )
                    if not manual or (manual["source_id"], manual["revision"]) != (
                        payload.get("manual_source_id"),
                        payload.get("manual_source_revision"),
                    ):
                        raise ValueError("the transcript changed; refresh before completing review")
                    if not set(manual_choices) <= set(manual["speakers"]):
                        raise ValueError("a selected speaker no longer appears in the transcript")
                    people = self.context.store.entities.load().entities
                    candidates = {c["id"]: c for c in manual["candidates"]}
                    if (
                        speaker_operation
                        and set(manual_choices) & speaker_operation["mapping"].keys()
                    ):
                        raise ValueError("choose only one person for each speaker")
                    if speaker_operation and (
                        speaker_operation["source_id"],
                        speaker_operation["revision"],
                    ) != (manual["source_id"], manual["revision"]):
                        raise ValueError("the transcript changed; refresh before completing review")
                    if not speaker_operation:
                        speaker_operation = {
                            "source_id": manual["source_id"],
                            "revision": manual["revision"],
                            "mapping": {},
                            "person_ids": {},
                            "people": [],
                        }
                    for label, person_id in manual_choices.items():
                        person = speaker_names.chosen_person(people, person_id, candidates)
                        if person is None:
                            raise ValueError(
                                f"{label}: this person is no longer active. Choose someone else."
                            )
                        person = speaker_names.find_contact(self.context, person)
                        speaker_operation["mapping"][label] = person["name"]
                        speaker_operation["person_ids"][label] = person.get("person_id") or (
                            person["id"].removeprefix("person:")
                            if person["id"].startswith("person:")
                            else ""
                        )
                        speaker_operation["people"].append(person)
                    speaker_names.people_changes(
                        self.context,
                        prospective.frontmatter if selected else note.frontmatter,
                        speaker_operation["people"],
                    )
                if speaker_operation:
                    taken: dict[str, str] = {}
                    for label, chosen in speaker_operation["mapping"].items():
                        if (earlier := taken.setdefault(chosen.casefold(), label)) != label:
                            raise ValueError(
                                f"{chosen} is already chosen for {earlier}. Choose someone "
                                "else or leave this speaker unnamed."
                            )
                    if payload.get("can_create_people", True) is not True and any(
                        speaker_names._is_contact(person) for person in speaker_operation["people"]
                    ):
                        from coppermind.store_protocol import PermissionDenied

                        raise PermissionDenied(
                            "This key cannot create a person. Choose an existing person or use "
                            "a key with permission to update people."
                        )
                receipt = {
                    "summary": {
                        "value": summary,
                        "generated": job.result.meeting_summary.model_dump(),
                    }
                    if summary is not None
                    else None,
                    "speakers": speaker_operation,
                    "note_id": identity,
                    "request": request_hash,
                    "state": "applying",
                    "expected_hash": note.content_hash,
                    "job_id": payload.get("job_id"),
                    "selected": selected,
                    "applied": [],
                    "created_at": now(),
                    "payload": {k: v for k, v in payload.items() if k != "can_create_people"},
                    "accept_link_risk": payload.get("accept_link_risk") is True,
                }
                self.context.write(name, receipt)
            if payload.get("can_create_people", True) is not True and any(
                speaker_names._is_contact(person)
                for person in (receipt.get("speakers") or {}).get("people", [])
            ):
                from coppermind.store_protocol import PermissionDenied

                raise PermissionDenied(
                    "This key cannot create a person. Use a key with permission to update people "
                    "to resume this review."
                )
            if receipt["selected"]:
                job = self.context.load_job(receipt["job_id"])
                if job.pending_approval:
                    await self.context.resume_approval({"id": job.id})
                job = self.context.load_job(job.id)
                # Recover an approval completed immediately before a process interruption.
                for decision in job.decisions:
                    index = decision.get("index")
                    if index in receipt["selected"] and index not in receipt["applied"]:
                        receipt["applied"].append(index)
                        receipt["expected_hash"] = decision["after_hash"]
                self.context.write(name, receipt)
                for index in receipt["selected"]:
                    if index in receipt["applied"]:
                        continue
                    current = await self.context.store.get_note(identity)
                    if current.content_hash != receipt["expected_hash"]:
                        raise ValueError(
                            "note changed during review; recorded operation needs inspection"
                        )
                    await self.context.approve(
                        {
                            "id": job.id,
                            "index": index,
                            "confirmation": "Apply selected suggestion",
                            "accept_link_risk": receipt["accept_link_risk"],
                        }
                    )
                    job = self.context.load_job(job.id)
                    receipt["applied"].append(index)
                    receipt["expected_hash"] = job.decisions[-1]["after_hash"]
                    self.context.write(name, receipt)
            current = await self.context.store.get_note(identity)
            if receipt.get("speakers") and not receipt.get("speakers_applied"):
                await speaker_names.create_contact_people(self.context, receipt["speakers"])
                self.context.write(name, receipt)
                current = await speaker_names.apply(self.context, receipt, current)
                receipt["expected_hash"] = current.content_hash
                receipt["speakers_applied"] = True
                self.context.write(name, receipt)
            if receipt.get("speakers") and not receipt.get("voices_applied"):
                from coppermind_store.voice_relabel import combine
                from coppermind_store.voice_store import VoiceStore

                async with self.context.lock:
                    voice = VoiceStore(self.context)
                    for label, person_id in receipt["speakers"].get("person_ids", {}).items():
                        if not person_id:
                            continue
                        unknown = next(
                            (
                                key
                                for key, value in voice.unknown_summaries().items()
                                if value["name"] == label
                            ),
                            None,
                        )
                        if unknown:
                            await combine(self.context, unknown, "person:" + person_id, identity)
                    receipt["voices_applied"] = True
                    self.context.write(name, receipt)
            if receipt.get("summary") and not receipt.get("summary_applied"):
                current = await meeting_summary.apply(self.context, receipt, current)
                receipt["expected_hash"] = current.content_hash
                receipt["summary_applied"] = True
                self.context.write(name, receipt)
            validate_review(current, schema, self._default_timezone())
            reviewed_key = schema.role("reviewed_key")
            if current.frontmatter.get(reviewed_key) is not True:
                if current.content_hash != receipt["expected_hash"]:
                    raise ValueError("note changed during review; no further changes applied")
                # Store the exact expected reviewed bytes before writing, for crash recovery.
                from coppermind import frontmatter as fm
                from coppermind_store.fs import resolve

                raw = resolve(self.context.store.notes_root, current.path).read_bytes()
                from coppermind_store.fs import content_hash

                receipt["reviewed_hash"] = content_hash(
                    fm.patch(raw.decode(), {reviewed_key: True}).encode()
                )
                # patch_frontmatter uses the same document serializer and conditional write.
                receipt["reviewed_body"] = fingerprint(current.body)
                self.context.write(name, receipt)
                current = await self.context.store.patch_frontmatter(
                    identity,
                    PatchFrontmatter(set={reviewed_key: True}),
                    if_match=current.content_hash,
                )
            elif receipt.get("reviewed_hash") != current.content_hash:
                raise ValueError("reviewed note diverged from the recorded completion")
            from coppermind_store.voice_store import VoiceStore

            operation = receipt.get("speakers")
            async with self.context.lock:
                voice = VoiceStore(self.context)
                for recording in self.context.recordings.jobs():
                    if recording.note_id != identity:
                        continue
                    if operation and recording.source_id == operation["source_id"]:
                        pending = self.context.read("voice-pending/" + recording.id, [])
                        proposed = speaker_names.voice_suggestions(self.context, identity)
                        current_people = self.context.store.entities.load().entities
                        for label, candidates in proposed.items():
                            original_label = next(
                                (old for old, new in operation["mapping"].items() if new == label),
                                label,
                            )
                            chosen_id = operation.get("person_ids", {}).get(original_label)
                            if chosen_id:
                                for candidate in candidates:
                                    proposed_id = candidate["id"].removeprefix("person:")
                                    if proposed_id != chosen_id:
                                        voice.reject(identity, label, proposed_id)
                        for speaker in pending:
                            if not isinstance(speaker, dict):
                                continue
                            original_label = next(
                                (
                                    old
                                    for old, new in operation["mapping"].items()
                                    if new == speaker.get("label")
                                ),
                                speaker.get("label"),
                            )
                            person_id = operation.get("person_ids", {}).get(original_label)
                            person = current_people.get(person_id)
                            if (
                                person
                                and person.kind == "person"
                                and not person.retired_at
                                and not person.redirect
                            ):
                                voice.confirm(
                                    person_id,
                                    identity,
                                    now(),
                                    speaker.get("speech_seconds"),
                                    speaker.get("embeddings", []),
                                )
                    (self.context.root / "voice-pending" / (recording.id + ".json")).unlink(
                        missing_ok=True
                    )
            receipt["state"], receipt["finished_at"] = "complete", now()
            receipt["final_hash"] = current.content_hash
            self.context.write(name, receipt)
            for job in self.context.jobs():
                if job.note_id == identity and job.state in {"suggested", "queued", "stale"}:
                    job.state = "rejected"
                    job.decisions.append(
                        {"action": "review completed; remaining proposals dismissed", "at": now()}
                    )
                    self.context.save_job(job)
            await self.finalize(current)
            from coppermind_store.voice_relabel import apply_deferred

            async with self.context.lock:
                await apply_deferred(self.context, identity)
            return receipt

    async def finalize(self, note: NoteDocument) -> None:
        from coppermind_store.context_service import now

        schema = self.context.store.control.schema()
        validate_review(note, schema, self._default_timezone())
        completion = self.context.read("review-completions/" + note.id, {})
        if (
            completion.get("state") != "complete"
            or completion.get("final_hash") != note.content_hash
        ):
            previous_observation = self.observed.get(note.id)
            if previous_observation is None or previous_observation[0] != note.content_hash:
                self.observed[note.id] = (note.content_hash, clock.monotonic())
                raise ValueError("waiting for the reviewed file to remain stable")
            if clock.monotonic() - previous_observation[1] < 30:
                raise ValueError("waiting for the reviewed file to remain stable")
        assignment = self.context.config().assignments["enrichment"]
        fields = note.frontmatter
        # Only final human-owned metadata becomes a confirmed fact. No model
        # claims or calendar invitees are promoted to attendance by inference.
        people = fields.get(assignment.attendees_field, []) if assignment.attendees_field else []
        if not isinstance(people, list):
            raise ValueError("confirmed attendees must be a list")
        record = {
            "note_id": note.id,
            "note_hash": note.content_hash,
            "path": note.path,
            "date": str(note_date(fields, schema)),
            "properties": fields,
            "attendees": [p for p in people if isinstance(p, str)],
            "calendar_hash": await self.context.calendar_evidence_hash(note),
            "state": "confirmed",
            "at": now(),
        }
        async with self.context.lock:
            records = self.context.read("finalized", {})
            previous = records.get(note.id)
            if (
                previous
                and previous["note_hash"] == note.content_hash
                and previous.get("state") == "confirmed"
                and previous.get("attendees") == record["attendees"]
                and previous.get("properties") == record["properties"]
            ):
                if previous.get("path") != note.path:
                    previous["path"] = note.path
                    self.context.write("finalized", records)
                self.rebuild_dossiers(records)
                return
            if previous:
                record["calendar_hash"] = previous["calendar_hash"]
            records[note.id] = record
            self.context.write("finalized", records)
            self.rebuild_dossiers(records)

    async def follow(self, note: NoteDocument) -> bool:
        """Carry a confirmed record to the hash and path filing itself produced.

        Filing rewrites links and moves the note after it was confirmed, so the
        record and the dossier contribution would otherwise keep the pre-filing
        hash and path. That is not a new edit by the person, so no stability wait.
        """
        async with self.context.lock:
            records = self.context.read("finalized", {})
            record = records.get(note.id)
            if not record or record.get("state") != "confirmed":
                return False
            if record["note_hash"] != note.content_hash or record.get("path") != note.path:
                record["note_hash"] = note.content_hash
                record["path"] = note.path
                self.context.write("finalized", records)
                self.rebuild_dossiers(records)
            return True

    def rebuild_dossiers(self, records: dict[str, Any]) -> None:
        from coppermind_store.context_service import fingerprint

        # A rebuildable dossier projection, with one contribution per note.
        dossiers: dict[str, Any] = {}
        for contribution in records.values():
            if contribution.get("state") != "confirmed":
                continue
            for address in contribution["attendees"]:
                local, separator, domain = address.rpartition("@")
                if not separator or not local or not domain:
                    continue  # Keep unresolvable human text in the note, not an identity guess.
                canonical = local + "@" + domain.lower()
                person = dossiers.setdefault(
                    fingerprint(canonical), {"address": canonical, "notes": []}
                )
                person["notes"].append(
                    {
                        "id": contribution["note_id"],
                        "path": contribution["path"],
                        "date": contribution["date"],
                        "note_hash": contribution["note_hash"],
                        "properties": contribution["properties"],
                    }
                )
        self.context.write("dossiers", dossiers)

    async def check_calendars(self) -> None:
        from coppermind_store.context_service import fingerprint, now

        generation = fingerprint(
            [
                await asyncio.to_thread(self.context.evidence_hash),
                self.context.config().model_dump(),
                self.context.store.control.settings().general.timezone,
            ]
        )
        changed = generation != self.calendar_generation

        records = self.context.read("finalized", {})
        baseline = {k: dict(v) for k, v in records.items()}
        updates = {}
        for identity, record in records.items():
            if record.get("state") != "confirmed":
                continue
            try:
                note = await self.context.store.get_note(identity)
                if (
                    note.frontmatter.get(self.context.store.control.schema().role("reviewed_key"))
                    is not True
                ):
                    record["state"] = "withdrawn"
                    continue
                if not changed and note.content_hash == record["note_hash"]:
                    continue
                current = await self.context.calendar_evidence_hash(note)
                if current != record["calendar_hash"]:
                    updates[identity] = {
                        "note_id": identity,
                        "state": "needs_review",
                        "message": "Calendar context changed after review; the note is unchanged",
                        "calendar_hash": current,
                        "at": now(),
                    }
            except (ValueError, StoreError):
                # A missing note no longer supports a published dossier contribution.
                record["state"] = "unavailable"
        async with self.context.lock:
            current_records = self.context.read("finalized", {})
            current_updates = self.context.read("calendar-followups", {})
            for identity, record in records.items():
                if current_records.get(identity) != baseline[identity]:
                    continue  # A concurrent review owns its newer evidence boundary.
                current_records[identity] = record
                if identity in updates:
                    current_updates[identity] = updates[identity]
                elif changed or record.get("state") != "confirmed":
                    current_updates.pop(identity, None)
            self.context.write("finalized", current_records)
            self.rebuild_dossiers(current_records)
            self.context.write("calendar-followups", current_updates)
        self.calendar_generation = generation

    async def resolve_calendar(self, payload: dict[str, Any]) -> dict[str, Any]:
        from coppermind_store.context_service import now

        async with self.lock:
            identity = payload["note_id"]
            note = await self.context.store.get_note(identity)
            if note.content_hash != payload.get("if_match"):
                raise ValueError("note changed; reload the calendar update")
            current = await self.context.calendar_evidence_hash(note)
            if current != payload.get("calendar_hash"):
                raise ValueError("calendar changed again; reload the update")
            event = payload.get("event_id", "")
            candidates = await self.context.calendar_candidates(note)
            if event and event not in {c["event_id"] for c in candidates}:
                raise ValueError("choose an event from the current matching window")
            async with self.context.lock:
                records = self.context.read("finalized", {})
                if identity not in records:
                    raise ValueError("note has not completed review")
                record = records[identity]
                record["calendar_hash"] = current
                if event:
                    record["confirmed_calendar"] = {
                        "event_id": event,
                        "at": now(),
                        "note_hash": note.content_hash,
                    }
                self.context.write("finalized", records)
                updates = self.context.read("calendar-followups", {})
                updates.pop(identity, None)
                self.context.write("calendar-followups", updates)
            return {"resolved": identity, "note_changed": False}
