"""Suggestions, transcription and search by meaning, as Admin sees them.

Each feature only chooses one of the AI services and says whether it is on.
Whether the service answers, and what it can do, is recorded once by
`ai_services` and read from there, so a feature page and the service's own page
can never disagree.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from coppermind.context_config import (
    SUMMARY_DIRECTIONS_MAX,
    EmbeddingAssignment,
    FeatureAssignment,
)
from coppermind.db.models import Note
from coppermind.schema import KeyDefinition
from coppermind.store_protocol import StoreError
from coppermind_store import meeting_summary
from coppermind_store.ai_services import UNREACHABLE, Refused

if TYPE_CHECKING:
    from coppermind_store.context_service import ContextService

# The properties Coppermind owns for people and calendar links. They are created when
# they are first needed, and nobody is asked which property holds what.
OWNED = (
    ("attendees", "list", "People who were actually there. Filled in by Coppermind."),
    (
        "invitees",
        "list",
        "People who were invited. Being invited does not mean they came. Filled in by Coppermind.",
    ),
    (
        "calendar_event",
        "string",
        "The calendar meeting the note belongs to. Filled in by Coppermind.",
    ),
    ("time_unknown", "bool", "Set when the time of a work meeting is not known."),
)
WAITS_S = (60, 120, 300, 600)
WAITS_TRANSCRIPTION_S = (1800, 3600, 7200, 14400)
WEEK = timedelta(days=7)


def _since(stamp: str, span: timedelta) -> bool:
    try:
        return bool(stamp) and datetime.now(UTC) - datetime.fromisoformat(stamp) <= span
    except ValueError:
        return False


def feature_state(assignment: Any, service: dict[str, Any] | None) -> str:
    """One word for a feature: `none`, `on`, `paused` or `failing`."""
    if assignment is None or not assignment.enabled or not assignment.connection:
        return "none"
    if assignment.paused if hasattr(assignment, "paused") else False:
        return "paused"
    if service is None or not service["answering"]:
        return "failing"
    return "on"


class Features:
    def __init__(self, context: ContextService) -> None:
        self.context = context

    @property
    def services(self) -> Any:
        return self.context.services

    def _service(self, identity: str) -> dict[str, Any] | None:
        try:
            return self.services.get(identity) if identity else None
        except ValueError:
            return None

    # -- what suggestions may fill in ------------------------------------------------------

    def fill_options(self) -> list[dict[str, str]]:
        """Every blank suggestions may fill, built-in first, then the owner's own properties."""
        schema = self.context.store.control.schema()
        roles = {schema.role(r): r for r in schema.roles}
        system = {
            schema.role(r) for r in ("id_key", "reviewed_key", "sources_key", "schema_version_key")
        } | {schema.optional_role("kind_key"), schema.optional_role("artifacts_key")}
        named = {
            "date_key": ("date", "Date", "When the meeting or note happened."),
            "type_key": ("type", "Type", "Decides which folder the note is filed in."),
            "context_key": (
                "context",
                "Context",
                "Internal, personal, or with someone outside such as a customer.",
            ),
            "account_key": ("account", "Account", "Which customer or company it was with."),
            "tags_key": ("tags", "Tags", "Your own labels."),
        }
        options = []
        own = []
        for key in schema.keys:
            if key.name in system or key.name in {name for name, _, _ in OWNED}:
                continue
            role = roles.get(key.name)
            if role in named:
                ident, label, hint = named[role]
                options.append({"key": ident, "label": label, "hint": hint, "field": key.name})
            else:
                own.append(
                    {
                        "key": "p:" + key.name,
                        "label": key.name,
                        "hint": (key.description + " " if key.description else "")
                        + "Added by you.",
                        "field": key.name,
                    }
                )
        order = ["date", "type", "context", "account", "tags"]
        options.sort(key=lambda item: order.index(item["key"]))
        options += [
            {
                "key": "people",
                "label": "Attendees and invitees",
                "hint": "Who was there and who was invited, matched to your calendar.",
                "field": "",
            },
            {
                "key": "event",
                "label": "Meeting",
                "hint": "Which calendar meeting the note belongs to.",
                "field": "",
            },
        ]
        return options + own

    def fill_now(self, assignment: FeatureAssignment) -> dict[str, bool]:
        ticked = {}
        for option in self.fill_options():
            if option["key"] == "people":
                ticked["people"] = bool(assignment.attendees_field or assignment.invitees_field)
            elif option["key"] == "event":
                ticked["event"] = bool(assignment.event_field)
            else:
                ticked[option["key"]] = option["field"] in assignment.fields
        return ticked

    def _apply_fill(self, assignment: FeatureAssignment, keys: set[str]) -> None:
        fields = []
        for option in self.fill_options():
            if option["field"] and option["key"] in keys:
                fields.append(option["field"])
        assignment.fields = fields[:20]
        assignment.attendees_field = "attendees" if "people" in keys else ""
        assignment.invitees_field = "invitees" if "people" in keys else ""
        assignment.event_field = "calendar_event" if "event" in keys else ""

    def ensure_owned_properties(self) -> None:
        """Create the properties Coppermind owns, and switch the note workflow on."""
        schema = self.context.store.control.schema()
        have = {key.name: key for key in schema.keys}
        added = False
        for name, kind, description in OWNED:
            if name not in have:
                schema.keys.append(
                    KeyDefinition.model_validate(
                        {"name": name, "kind": kind, "description": description}
                    )
                )
                added = True
        if added:
            state = self.context.store.control.store.read("schema")
            self.context.store.control.store.write(
                "schema", schema.model_dump(mode="json"), if_revision=state.revision
            )

    # -- suggestions ----------------------------------------------------------------------

    def _latest_jobs(self) -> list[Any]:
        latest: dict[str, Any] = {}
        for job in sorted(self.context.jobs(), key=lambda item: item.created_at):
            if job.purpose != "dossier":
                latest[job.note_id] = job
        return list(latest.values())

    async def _waiting_notes(self) -> list[dict[str, Any]]:
        """Notes still waiting for review, newest first, from the mirror."""
        review = self.context.store.control.settings().notes.review_folder
        async with self.context.store.session_factory() as session:
            rows = (
                await session.scalars(
                    sa.select(Note)
                    .where(Note.state == "ok", Note.reviewed.is_(False))
                    .order_by(Note.first_seen_at.desc())
                    .limit(500)
                )
            ).all()
        return [
            {
                "id": row.id,
                "title": row.title or Path(row.path).stem,
                "path": row.path,
                "arrived": row.first_seen_at.isoformat(),
                "in_review": row.path.startswith(review + "/"),
            }
            for row in rows
        ]

    async def older_notes(self) -> list[dict[str, Any]]:
        """Waiting notes with no suggestions: never asked for, or the answer could not be used.

        A note whose service was unreachable is not one of these. It is waiting, and is
        done by itself when the service answers again.
        """
        latest = {job.note_id: job for job in self._latest_jobs()}
        # The notes that were already waiting when suggestions were turned on. A newer note
        # with nothing asked yet is simply next in line, and is asked for within a minute.
        before = set(self.context.read("automatic_baseline", {}).get("ids", []))
        without = []
        for note in await self._waiting_notes():
            job = latest.get(note["id"])
            if job is None:
                if note["id"] in before:
                    without.append(note | {"why": "before"})
            elif job.state == "failed" and job.error_rule not in UNREACHABLE:
                without.append(note | {"why": "failed"})
        return without

    async def suggestions(self) -> dict[str, Any]:
        config = self.context.config()
        assignment = config.assignments["enrichment"]
        service = self._service(assignment.connection)
        state = feature_state(assignment, service)
        jobs = self._latest_jobs()
        done = [job for job in jobs if job.succeeded_at]
        last = max(done, key=lambda job: job.succeeded_at, default=None)
        waiting = [
            job
            for job in jobs
            if job.state in {"queued", "running"}
            or (job.state == "failed" and job.error_rule in UNREACHABLE)
        ]
        older = await self.older_notes() if state != "none" else []
        return {
            "state": state,
            "service": service,
            "model": assignment.model,
            "fill": self.fill_now(assignment),
            "fill_options": self.fill_options(),
            "descr": assignment.send_calendar_descriptions,
            "wait_s": assignment.timeout_s,
            "summary_custom": bool(assignment.summary_directions),
            "waiting": len(waiting),
            "older": older,
            "week": {
                "count": sum(1 for job in done if _since(job.succeeded_at, WEEK)),
                "failed": sum(
                    1
                    for job in jobs
                    if job.state == "failed"
                    and job.error_rule not in UNREACHABLE
                    and _since(job.created_at, WEEK)
                ),
            },
            "last": {
                "at": last.succeeded_at,
                "title": Path(last.path).stem,
            }
            if last
            else None,
            "revision": config.revision,
        }

    async def suggestions_save(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Turn suggestions on, or save a change of service, model or what is filled in."""
        service = self._service(str(payload.get("connection", "")))
        if service is None:
            raise Refused("service", "Choose a service first.")
        if not service["answering"]:
            raise Refused(
                "service",
                f"{service['name']} is not answering right now, so it cannot be used. Open it "
                "under Manage services to test or edit it, or choose another.",
            )
        working = [r["name"] for r in service["text"]["models"] if r["ok"]]
        model = str(payload.get("model", "")) or service["text"]["best"]
        if not service["text"]["ok"] or model not in working:
            raise Refused("model", "Choose one of the models that worked in the test.")
        keys = {str(item) for item in payload.get("fill", [])}
        async with self.context.lock:
            self.ensure_owned_properties()
            config = self.context.config()
            old = config.assignments["enrichment"]
            was = feature_state(old, self._service(old.connection))
            assignment = old.model_copy(
                update={
                    "connection": service["id"],
                    "model": model,
                    "enabled": True,
                    "paused": False,
                }
            )
            if "fill" in payload:
                self._apply_fill(assignment, keys)
                assignment.send_calendar_descriptions = bool(payload.get("descr"))
            from coppermind_store.context_service import now

            assignment.automatic_after = old.automatic_after or now()
            config.lifecycle_enabled = True
            config.assignments["enrichment"] = FeatureAssignment.model_validate(
                assignment.model_dump()
            )
            if not (self.context.root / "automatic_baseline.json").exists():
                # Notes that were already waiting are not sent anywhere by turning this on.
                self.context.write(
                    "automatic_baseline",
                    {"ids": [note["id"] for note in await self._waiting_notes()]},
                )
            self.context.save_config(config)
        resumed = await self.services.resume(service["id"]) if was == "failing" else {"notes": 0}
        return {"was": was, "model": model, "service": service["name"], "resumed": resumed}

    async def _update_enrichment(self, **changes: Any) -> FeatureAssignment:
        async with self.context.lock:
            config = self.context.config()
            assignment = FeatureAssignment.model_validate(
                {**config.assignments["enrichment"].model_dump(), **changes}
            )
            config.assignments["enrichment"] = assignment
            self.context.save_config(config)
            return assignment

    async def suggestions_pause(self, payload: dict[str, Any]) -> dict[str, Any]:
        await self._update_enrichment(paused=bool(payload.get("paused", True)))
        return {"waiting": (await self.suggestions())["waiting"]}

    async def suggestions_wait(self, payload: dict[str, Any]) -> dict[str, Any]:
        seconds = int(payload["seconds"])
        if seconds not in WAITS_S:
            raise ValueError("choose one of the listed waits")
        await self._update_enrichment(timeout_s=seconds)
        return {"seconds": seconds}

    # -- how the meeting summary is written -----------------------------------------------

    def summary_directions(self) -> dict[str, Any]:
        """Each section of a meeting summary with its directions, and what is always added."""
        own = self.context.config().assignments["enrichment"].summary_directions
        sent = meeting_summary.directions(own)
        return {
            "sections": [
                {
                    "key": section.key,
                    "name": section.name,
                    "text": sent[section.key],
                    "default": section.default,
                    "custom": sent[section.key] != section.default,
                }
                for section in meeting_summary.SECTIONS
            ],
            "locked": list(meeting_summary.LOCKED_WORDS),
            "max": SUMMARY_DIRECTIONS_MAX,
        }

    async def summary_directions_save(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Save the directions for each section. An empty box, or the default, means the default.

        Nothing already written is touched: the next summary uses the new directions, and
        a summary that exists is only written again when its owner asks for that.
        """
        given = payload.get("directions") or {}
        kept: dict[str, str] = {}
        emptied: list[str] = []
        for section in meeting_summary.SECTIONS:
            if section.key not in given:
                # A section the page did not send keeps what it had.
                current = self.context.config().assignments["enrichment"].summary_directions
                if current.get(section.key):
                    kept[section.key] = current[section.key]
                continue
            text = meeting_summary._clean(given[section.key])  # noqa: SLF001
            if not text:
                emptied.append(section.name)
                continue
            if len(text) > SUMMARY_DIRECTIONS_MAX:
                raise Refused(
                    section.key,
                    f"The directions for {section.name} are {len(text):,} characters and the "
                    f"most is {SUMMARY_DIRECTIONS_MAX:,}. Shorten them and save again. "
                    "Nothing was changed.",
                )
            if text != section.default:
                kept[section.key] = text
        await self._update_enrichment(summary_directions=kept)
        return {"custom": sorted(kept), "emptied": emptied}

    async def suggestions_fill(self, payload: dict[str, Any]) -> dict[str, Any]:
        keys = {str(item) for item in payload.get("fill", [])}
        async with self.context.lock:
            self.ensure_owned_properties()
            config = self.context.config()
            assignment = config.assignments["enrichment"]
            self._apply_fill(assignment, keys)
            if "descr" in payload:
                assignment.send_calendar_descriptions = bool(payload["descr"])
            config.assignments["enrichment"] = FeatureAssignment.model_validate(
                assignment.model_dump()
            )
            self.context.save_config(config)
        return {"fill": self.fill_now(assignment)}

    async def suggestions_tick(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Tick or untick one blank, from the property's own page or the Tags page."""
        key, on = str(payload["key"]), bool(payload["on"])
        config = self.context.config()
        ticked = {k for k, v in self.fill_now(config.assignments["enrichment"]).items() if v}
        ticked = ticked | {key} if on else ticked - {key}
        return await self.suggestions_fill({"fill": sorted(ticked)})

    async def suggestions_older(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Ask for suggestions on waiting notes that never had any."""
        wanted = payload.get("note_ids")
        notes = await self.older_notes()
        if wanted is not None:
            notes = [note for note in notes if note["id"] in set(wanted)]
        started, refused = [], []
        for note in notes[:50]:
            try:
                await self.context.queue_note(note["id"], force=True, fresh=True)
                started.append(note["title"])
            except (ValueError, StoreError) as exc:
                refused.append({"title": note["title"], "why": str(exc)})
        return {"started": started, "left_out": refused}

    async def suggestions_off(self) -> None:
        async with self.context.lock:
            config = self.context.config()
            assignment = config.assignments["enrichment"]
            assignment.enabled, assignment.paused = False, False
            assignment.connection, assignment.model = "", ""
            self.context.save_config(config)

    # -- transcription --------------------------------------------------------------------

    async def transcription(self) -> dict[str, Any]:
        config = self.context.config()
        assignment = config.assignments.get("transcription")
        service = self._service(assignment.connection) if assignment else None
        jobs = sorted(self.context.recordings.jobs(), key=lambda j: j.created_at, reverse=True)
        week = [job for job in jobs if _since(job.created_at, WEEK)]
        done = [job for job in jobs if job.state == "succeeded"]
        last = None
        if done:
            newest = max(done, key=lambda job: job.succeeded_at)
            last = {
                "title": newest.title,
                "minutes": recording_minutes(newest),
                "took_minutes": took_minutes(newest),
            }
        return {
            "state": feature_state(assignment, service),
            "service": service,
            "model": assignment.model if assignment else "",
            "wait_s": assignment.wait_s if assignment else 3600,
            "attempts": assignment.attempts if assignment else 2,
            "week": {
                "done": sum(1 for job in week if job.state == "succeeded"),
                "waiting": sum(1 for job in jobs if job.state in {"queued", "running"})
                + sum(1 for job in jobs if _held(job)),
                "failed": sum(
                    1
                    for job in jobs
                    if job.state == "failed" and not job.dismissed and not _held(job)
                ),
            },
            "last": last,
            "revision": config.revision,
        }

    async def transcription_save(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Turn transcription on, or save a change of service. The reply format is the one
        the service's own test found to work; nobody is asked for it."""
        service = self._service(str(payload.get("connection", "")))
        if service is None:
            raise Refused("service", "Choose a service first.")
        if not service["answering"]:
            raise Refused(
                "service",
                f"{service['name']} is not answering right now, so it cannot be used. Open it "
                "under Manage services to test or edit it, or choose another.",
            )
        speech = service["speech"]
        if not speech["ok"]:
            raise Refused("service", f"{service['name']} cannot write up recordings.")
        model = str(payload.get("model", "")).strip()[:200]
        if not model:
            model = speech.get("model", "")
        reply_format = speech.get("formats", {}).get(model) or speech.get("format", "diarized_json")
        wait_s = int(payload.get("wait_s", 3600))
        if wait_s not in WAITS_TRANSCRIPTION_S:
            raise ValueError("choose one of the listed waits")
        async with self.context.lock:
            config = self.context.config()
            old = config.assignments.get("transcription") or FeatureAssignment()
            was = feature_state(
                config.assignments.get("transcription"), self._service(old.connection)
            )
            config.assignments["transcription"] = FeatureAssignment(
                connection=service["id"],
                model=model,
                enabled=True,
                paused=False,
                wait_s=wait_s,
                attempts=old.attempts,
                response_format=reply_format,
                automatic_after=old.automatic_after,
            )
            self.context.save_config(config)
        resumed = await self.services.resume(service["id"])
        return {"was": was, "service": service["name"], "resumed": resumed}

    async def _update_transcription(self, **changes: Any) -> None:
        async with self.context.lock:
            config = self.context.config()
            current = config.assignments.get("transcription")
            if current is None:
                raise ValueError("Transcription is not set up yet.")
            config.assignments["transcription"] = FeatureAssignment.model_validate(
                {**current.model_dump(), **changes}
            )
            self.context.save_config(config)

    async def transcription_pause(self, payload: dict[str, Any]) -> dict[str, Any]:
        await self._update_transcription(paused=bool(payload.get("paused", True)))
        return {}

    async def transcription_wait(self, payload: dict[str, Any]) -> dict[str, Any]:
        seconds = int(payload["seconds"])
        if seconds not in WAITS_TRANSCRIPTION_S:
            raise ValueError("choose one of the listed waits")
        await self._update_transcription(wait_s=seconds)
        return {"seconds": seconds}

    async def transcription_tries(self, payload: dict[str, Any]) -> dict[str, Any]:
        tries = int(payload["tries"])
        if tries not in (1, 2, 3):
            raise ValueError("choose 1, 2 or 3")
        await self._update_transcription(attempts=tries)
        return {"tries": tries}

    async def transcription_retry_failed(self, payload: dict[str, Any]) -> dict[str, Any]:
        retried = []
        for job in self.context.recordings.jobs():
            if job.state == "failed" and not job.dismissed:
                try:
                    await self.context.recordings.retry({"id": job.id})
                    retried.append(job.title)
                except ValueError:
                    continue
        return {"retried": retried}

    async def transcription_off(self) -> None:
        async with self.context.lock:
            config = self.context.config()
            current = config.assignments.get("transcription")
            if current is not None:
                current.enabled, current.paused = False, False
                current.connection, current.model = "", ""
                self.context.save_config(config)

    # -- search by meaning ----------------------------------------------------------------

    async def search(self) -> dict[str, Any]:
        config = self.context.config()
        assignment = config.embeddings
        service = self._service(assignment.connection)
        index = await self.context.store.get_search_index_status()
        on = assignment.enabled and bool(assignment.connection)
        return {
            "state": "on" if on else "off",
            "service": service if on else None,
            "model": assignment.model,
            "index": index.model_dump(mode="json"),
            "revision": config.revision,
        }

    async def search_on(self, payload: dict[str, Any]) -> dict[str, Any]:
        service = self._service(str(payload.get("connection", "")))
        if service is None:
            raise Refused("service", "Choose a service first.")
        if not service["answering"]:
            raise Refused(
                "service",
                f"{service['name']} is not answering right now, so it cannot be used. Open it "
                "under Manage services to test or edit it, or choose another.",
            )
        if not service["embed"]["ok"]:
            raise Refused("service", f"{service['name']} cannot do search by meaning.")
        config = self.context.config()
        await self.context.store.embeddings.save(
            {
                "revision": config.revision,
                "assignment": EmbeddingAssignment(
                    connection=service["id"], model=service["embed"]["model"], enabled=True
                ).model_dump(),
            }
        )
        return {"service": service["name"]}

    async def search_off(self) -> None:
        config = self.context.config()
        await self.context.store.embeddings.save(
            {
                "revision": config.revision,
                "assignment": EmbeddingAssignment().model_dump(),
            }
        )

    # -- taking over from a service that is being removed -----------------------------------

    async def take_over(self, feature: str, target: str) -> str:
        """Point one feature at another service, or turn it off. Returns what was done."""
        if target == "off":
            if feature == "sugg":
                await self.suggestions_off()
                return "Suggestions are off."
            if feature == "trans":
                await self.transcription_off()
                return "Transcription is off. Recordings are kept until it is on again."
            await self.search_off()
            return "Search by meaning is off."
        service = self._service(target)
        if service is None:
            raise Refused("rm_" + feature, "Choose one before removing.")
        if feature == "sugg":
            await self.suggestions_save({"connection": target, "model": service["text"]["best"]})
            return f"Suggestions now use {service['name']} ({service['text']['best']})."
        if feature == "trans":
            config = self.context.config()
            current = config.assignments.get("transcription") or FeatureAssignment()
            await self.transcription_save({"connection": target, "wait_s": current.wait_s})
            return f"Transcription now uses {service['name']}."
        await self.search_on({"connection": target})
        return f"Search by meaning now uses {service['name']}."


def _held(job: Any) -> bool:
    """A recording that stopped only because its service was not answering."""
    return job.state == "failed" and job.error_rule in UNREACHABLE and not job.dismissed


def recording_minutes(job: Any) -> int | None:
    """How long a recording runs, in whole minutes, when anything recorded says."""
    for source in (job.usage, job.metadata):
        for key in ("duration_seconds", "duration", "audio_seconds"):
            value = source.get(key) if isinstance(source, dict) else None
            if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
                return max(1, round(value / 60))
    return None


def took_minutes(job: Any) -> int | None:
    try:
        started = datetime.fromisoformat(job.started_at or job.created_at)
        ended = datetime.fromisoformat(job.succeeded_at)
    except ValueError:
        return None
    return max(1, round((ended - started).total_seconds() / 60))
