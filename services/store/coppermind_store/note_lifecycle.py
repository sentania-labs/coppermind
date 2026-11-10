"""One-shot suggestions, explicit review completion and rebuildable confirmed facts."""

from __future__ import annotations

import asyncio
import time as clock
from typing import TYPE_CHECKING, Any

from coppermind.ids import is_valid_id
from coppermind.schema import (
    date_field_problem,
    field_suggestion_changes,
    note_date,
    note_start_time,
    parse_note_time,
)
from coppermind.store_protocol import NoteDocument, PatchFrontmatter, StoreError
from coppermind_store import speaker_names

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
                    if (current := speaker_names.current_person(self.context, candidate))
                    is not None
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
        if any(r.note_id == note.id and r.source_id for r in self.context.recordings.jobs()):
            manual = await speaker_names.evidence(
                self.context, note, await self.context.calendar_candidates(note)
            )
        if manual:
            manual = {
                key: manual[key] for key in ("source_id", "revision", "speakers", "candidates")
            }
            manual["candidates"] = [
                person
                for candidate in manual["candidates"]
                if (person := speaker_names.current_person(self.context, candidate)) is not None
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
            "suggestions": suggestions,
            "unresolved": job.result.unresolved if job and job.result else [],
            "roles": self.context.store.control.schema().roles,
            "finalization": self.context.read("finalized", {}).get(note.id),
            "calendar_update": update,
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
            request_hash = fingerprint(
                {
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
            )
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
                if selected:
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
                    mapping = {}
                    chosen = []
                    for i in speaker_indexes:
                        suggestion = job.result.suggestions[i]
                        person_id = choices.get(str(i), suggestion.value or "")
                        if not person_id:
                            continue
                        person = speaker_names.chosen_person(self.context, person_id, candidates)
                        if person is None:
                            person_name = candidates.get(person_id, {}).get("name", "This person")
                            raise ValueError(
                                f"{suggestion.field}: {person_name} is no "
                                "longer an active person. Choose someone else or leave this "
                                "speaker unnamed."
                            )
                        mapping[suggestion.field] = person["name"]
                        chosen.append(person)
                    if mapping:
                        speaker_operation = {
                            "source_id": speaker_data["source_id"],
                            "revision": speaker_data["revision"],
                            "mapping": mapping,
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
                            "people": [],
                        }
                    for label, person_id in manual_choices.items():
                        person = speaker_names.chosen_person(self.context, person_id, candidates)
                        if person is None:
                            raise ValueError(
                                f"{label}: this person is no longer active. Choose someone else."
                            )
                        speaker_operation["mapping"][label] = person["name"]
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
                receipt = {
                    "speakers": speaker_operation,
                    "note_id": identity,
                    "request": request_hash,
                    "state": "applying",
                    "expected_hash": note.content_hash,
                    "job_id": payload.get("job_id"),
                    "selected": selected,
                    "applied": [],
                    "created_at": now(),
                    "payload": payload,
                    "accept_link_risk": payload.get("accept_link_risk") is True,
                }
                self.context.write(name, receipt)
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
                current = await speaker_names.apply(self.context, receipt, current)
                receipt["expected_hash"] = current.content_hash
                receipt["speakers_applied"] = True
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
