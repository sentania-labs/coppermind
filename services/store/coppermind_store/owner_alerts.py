"""Telling the owner when a recording failed or a note has no summary.

Nothing here keeps a list of its own. What needs the owner is what the recording records
already say: `Recordings.waiting()` names the failed recordings, the ones still on their way
and the notes whose summary could not be written. This module keeps two outward copies of
that one answer current, so the owner finds problems where they already are (a note in the
vault is not one of them):

* an optional outbound webhook for the three events an automation cares about: a recording
  failed for good, a summary failed for good, and a recording has been stuck too long;
* the counters in `GET /v1/status`.

The webhook is off until an address is saved in Admin. An event is sent once for each final
failure, and only for failures that happen after the address was saved, so switching it on
never replays history.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from coppermind import frontmatter as fm
from coppermind.naming import sanitize_folder
from coppermind.store_protocol import StoreError
from coppermind_store.fs import resolve

if TYPE_CHECKING:
    from coppermind_store.context_service import ContextService
    from coppermind_store.recordings import TranscriptionJob

log = logging.getLogger(__name__)

PROBLEMS_NOTE = "Needs attention.md"
# Frontmatter that marks a file Coppermind writes and owns. The reconciler never adopts it.
GENERATED_KEY = "coppermind-generated"
GENERATED_VALUE = "needs-attention"
CONFIG = "attention-webhook"
EVENTS = "attention-events"
NOTE_AT = "attention-note"
STUCK_HOURS = 6
TICK_SECONDS = 60
TRIES = 5
RETRY_AFTER = timedelta(minutes=5)
EVENTS_KEPT = 500
SENT_TIMEOUT_S = 10

FAILED = "recording.failed"
SUMMARY_FAILED = "summary.failed"
STUCK = "recording.stuck"


def _now() -> datetime:
    return datetime.now(UTC)


def _moment(text: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def check_address(address: str) -> str:
    """The address as it will be used, or a plain reason it cannot be."""
    address = address.strip()
    if not address:
        return ""
    parts = urlsplit(address)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("Use a full web address that starts with http:// or https://.")
    if parts.username or parts.password:
        raise ValueError(
            "Leave the user name and password out of the address. If the receiver needs a "
            "secret, put it in the path or the query of the address."
        )
    if len(address) > 2000:
        raise ValueError("That address is too long.")
    return address


def shown_address(address: str) -> str:
    """Where the webhook goes, without the path or query, which may hold a secret."""
    parts = urlsplit(address)
    return f"{parts.scheme}://{parts.netloc}" if parts.netloc else ""


class OwnerAlerts:
    def __init__(self, context: ContextService) -> None:
        self.context = context

    # -- setup ---------------------------------------------------------------------------

    def config(self) -> dict[str, Any]:
        found = self.context.read(CONFIG, {})
        hours = found.get("stuck_hours", STUCK_HOURS)
        return {
            "url": str(found.get("url", "")),
            "stuck_hours": hours if isinstance(hours, int) and 1 <= hours <= 168 else STUCK_HOURS,
            "since": str(found.get("since", "")),
        }

    async def view(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The webhook setup as Admin shows it. The address itself never leaves the store."""
        found = self.config()
        return {
            "on": bool(found["url"]),
            "where": shown_address(found["url"]),
            "stuck_hours": found["stuck_hours"],
            "recent": self.recent_events(),
        }

    async def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Set the webhook address and how long a recording may take before it is called stuck."""
        from coppermind_store.ai_services import Refused

        found = self.config()
        if "url" in payload:
            try:
                url = check_address(str(payload["url"]))
            except ValueError as error:
                raise Refused("url", str(error)) from error
            if url != found["url"]:
                # Failures from before this moment are not sent: switching it on is not a replay.
                found["since"] = _now().isoformat() if url else ""
            found["url"] = url
        if "stuck_hours" in payload:
            try:
                hours = int(payload["stuck_hours"])
            except (TypeError, ValueError):
                hours = 0
            if not 1 <= hours <= 168:
                raise Refused("stuck_hours", "Use a number of hours from 1 to 168.")
            found["stuck_hours"] = hours
        self.context.write(CONFIG, found)
        return await self.view({})

    def recent_events(self) -> list[dict[str, Any]]:
        """The newest events sent or given up on, for the one line Admin shows under setup."""
        events = self.context.read(EVENTS, {})
        newest = sorted(events.values(), key=lambda item: item.get("at", ""), reverse=True)[:5]
        return [
            {"event": item["event"], "title": item.get("title", ""), "state": item["state"]}
            for item in newest
        ]

    # -- the one list --------------------------------------------------------------------

    async def counts(self) -> dict[str, int]:
        waiting = await self.context.recordings.waiting()
        return {
            "recordings_failed": len(waiting["failed"]),
            "notes_without_summary": len(waiting["missing_summaries"]),
        }

    async def write_up_missing(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Write up every recording note that has no summary, in one step.

        Each note goes through Write the summary again, so there is one way a summary is
        made. That never writes into a note: the new summary waits in the note's review
        and joins the body when the note is reviewed, which keeps every byte already there,
        so a body the owner edited by hand is never overwritten.
        """
        from coppermind_store.ai_services import Refused

        notes = self.context.store.admin.notes
        queued = already = skipped = 0
        for item in await self.context.recordings.missing_summaries():
            try:
                result = await notes.summary_again({"id": item["id"]})
            except Refused as refusal:
                if not item["can_retry"]:
                    raise ValueError(refusal.message) from refusal
                skipped += 1
                continue
            except StoreError:
                skipped += 1
                continue
            if result.get("already"):
                already += 1
            else:
                queued += 1
        return {"queued": queued, "already": already, "skipped": skipped}

    # -- the old problems note ----------------------------------------------------------

    def remove_old_note(self) -> None:
        """Remove a `Needs attention` note an earlier version wrote; never one the owner wrote.

        Only a file that carries Coppermind's own marker is removed, in the review folder as
        it is now and where it was last written.
        """
        settings = self.context.store.control.settings()
        folder = sanitize_folder(settings.notes.review_folder)
        places = {f"{folder}/{PROBLEMS_NOTE}", self.context.read(NOTE_AT, {}).get("path", "")}
        removed = True
        for relative in sorted(place for place in places if place):
            try:
                path = resolve(self.context.store.notes_root, relative)
                if path.is_file() and self._ours(path.read_bytes()):
                    path.unlink()
                    log.info("removed the generated %s", PROBLEMS_NOTE)
            except ValueError:
                continue
            except OSError:
                removed = False
                log.warning("the generated %s could not be removed", PROBLEMS_NOTE)
        if removed:
            self.context.write(NOTE_AT, {})

    def _ours(self, data: bytes) -> bool:
        try:
            frontmatter, _ = fm.parse(data.decode("utf-8"))
        except (UnicodeDecodeError, fm.FrontmatterError):
            return False
        return frontmatter.get(GENERATED_KEY) == GENERATED_VALUE

    # -- the webhook ---------------------------------------------------------------------

    def due_events(self, since: datetime) -> list[tuple[str, dict[str, Any]]]:
        """Events for what failed for good, or got stuck, after `since`: a key and the payload."""
        from coppermind_store.recordings import DELETED_RULE, repeats_only

        recordings = self.context.recordings
        config = self.config()
        stuck_after = timedelta(hours=config["stuck_hours"])
        now = _now()
        events: list[tuple[str, dict[str, Any]]] = []
        jobs = recordings.jobs()
        latest = recordings.latest_enrichment() if any(job.note_id for job in jobs) else {}

        def recording(job: TranscriptionJob) -> dict[str, Any]:
            return {
                "id": job.id,
                "provider": job.provider,
                "external_source_id": job.external_source_id,
                "title": job.title or job.filename,
                "stage": job.stage,
            }

        for job in jobs:
            if job.state == "failed" and job.error_rule != DELETED_RULE and not job.dismissed:
                final = job.attempts >= job.attempt_limit or repeats_only(job.error_rule)
                failed_at = job.failed_at or job.created_at
                moment = _moment(failed_at)
                if final and moment and moment >= since:
                    events.append(
                        (
                            f"{FAILED}:{job.id}:{failed_at}",
                            {
                                "event": FAILED,
                                "recording": recording(job),
                                "error": job.display_error,
                                "error_rule": job.error_rule,
                                "failed_at": failed_at,
                            },
                        )
                    )
            elif job.state in {"queued", "running"}:
                began = job.started_at or job.created_at
                moment = _moment(began)
                if moment and now - moment > stuck_after and moment + stuck_after >= since:
                    events.append(
                        (
                            f"{STUCK}:{job.id}:{job.created_at}",
                            {
                                "event": STUCK,
                                "recording": recording(job),
                                "since": began,
                                "stuck_hours": config["stuck_hours"],
                            },
                        )
                    )
            elif (
                job.state == "succeeded"
                and job.generation_mode != "markdown-v1"
                and job.note_id
                and job.source_id
            ):
                summary = recordings.summary_of(job, latest)
                finished = summary["summary_finished_at"]
                moment = _moment(finished)
                if summary["summary_state"] == "failed" and moment and moment >= since:
                    events.append(
                        (
                            f"{SUMMARY_FAILED}:{job.note_id}:{finished}",
                            {
                                "event": SUMMARY_FAILED,
                                "recording": recording(job),
                                "note": {"id": job.note_id, "path": job.note_path},
                                "error": summary["summary_error"],
                                "error_rule": summary["summary_error_rule"],
                                "failed_at": finished,
                            },
                        )
                    )
        return events

    async def send_events(self) -> None:
        """Send each event not yet sent, trying again later when the receiver does not answer."""
        config = self.config()
        if not config["url"]:
            return
        since = _moment(config["since"]) or _now()
        events = self.context.read(EVENTS, {})
        now = _now()
        changed = False
        due = await asyncio.to_thread(self.due_events, since)
        async with httpx.AsyncClient(timeout=SENT_TIMEOUT_S, follow_redirects=False) as client:
            for key, payload in due:
                record = events.get(key, {})
                if record.get("state") in {"sent", "gave-up"}:
                    continue
                retry_at = _moment(record.get("next", ""))
                if retry_at and retry_at > now:
                    continue
                payload = {
                    **payload,
                    "at": now.isoformat(),
                    "id": hashlib.sha256(key.encode()).hexdigest()[:32],
                }
                tries = int(record.get("tries", 0)) + 1
                try:
                    answer = await client.post(config["url"], json=payload)
                    delivered = answer.is_success
                    problem = "" if delivered else f"answered {answer.status_code}"
                except httpx.HTTPError as exc:
                    delivered, problem = False, type(exc).__name__
                state = "sent" if delivered else ("gave-up" if tries >= TRIES else "pending")
                events[key] = {
                    "event": payload["event"],
                    "title": payload["recording"]["title"],
                    "state": state,
                    "tries": tries,
                    "next": "" if delivered else (now + RETRY_AFTER * tries).isoformat(),
                    "at": now.isoformat(),
                }
                changed = True
                if not delivered:
                    log.warning(
                        "webhook %s not delivered (%s), try %d", payload["event"], problem, tries
                    )
        if changed:
            # A marker for an event that `due_events` can still return is the only thing that
            # stops it being sent again, so it is never pruned; only markers for events that
            # are gone (the recording was retried, the summary written) are cut to a bound.
            live = {key for key, _ in due}
            gone = sorted(
                ((k, v) for k, v in events.items() if k not in live),
                key=lambda pair: pair[1].get("at", ""),
                reverse=True,
            )
            self.context.write(
                EVENTS,
                {**{k: v for k, v in events.items() if k in live}, **dict(gone[:EVENTS_KEPT])},
            )

    # -- the worker ----------------------------------------------------------------------

    async def tick(self) -> None:
        await self.send_events()

    async def run(self) -> None:
        try:
            self.remove_old_note()
        except Exception:
            log.exception("removing the old problems note failed")
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("telling the owner what needs attention failed")
            await asyncio.sleep(TICK_SECONDS)
