"""Durable Markdown generation before ordinary recording ingest."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from coppermind.context_config import FeatureAssignment
from coppermind.logging import get_logger
from coppermind.recording_writeup import (
    BOUNDARY_END,
    BOUNDARY_RULES,
    BOUNDARY_START,
    DEFAULT_PROMPT,
    LOCKED_RULES,
    RecordingBoundary,
    failure_message,
)
from coppermind.schema import parse_note_time
from coppermind_store import meeting_match
from coppermind_store.model_provider import ProviderError


def assignment(context: Any) -> FeatureAssignment:
    config = context.config()
    if "writeup" not in config.assignments:
        old = config.assignments["enrichment"]
        config.assignments["writeup"] = old.model_copy(
            update={
                "enabled": True,
                "paused": False,
                "timeout_s": old.summary_timeout_s,
            }
        )
        context.save_config(config)
    return config.assignments["writeup"]


def validate(document: str, title: str, labels: list[str]) -> str:
    text = document.strip()
    lines = text.splitlines()
    if not lines or lines[0] != "# " + title or not "\n".join(lines[1:]).strip():
        raise ValueError("The write-up must start with the supplied title and contain a body.")
    if not any(line.strip() and not line.lstrip().startswith("#") for line in lines[1:]):
        raise ValueError("The write-up has headings but no readable content.")
    if any(line.startswith("# ") for line in lines[1:]):
        raise ValueError("The write-up returned more than one document title.")
    if (
        "\u2014" in "\n".join(lines[1:])
        or re.search(r"<[^>]+>|^---\s*$|^```", text, re.M)
        or text.startswith(("{", "["))
    ):
        raise ValueError("The write-up must be Markdown without YAML, HTML or code wrapping.")
    # Placeholder identities are an exact contract, including recording-local qualifiers.
    for label in re.findall(r"Unknown voice \d+(?: \(recording-local\))?", text, re.I):
        if label not in labels:
            raise ValueError("The write-up used a speaker label that was not supplied.")
    if re.search(r"\bSpeaker\s+\d+\b|\bSPEAKER_\d+\b", text):
        raise ValueError("The write-up replaced a stable speaker label.")
    action_section = re.search(r"^## Action items\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    if action_section:
        for owner in re.findall(r"^### (.+)$", action_section.group(1), re.M):
            if owner.strip() not in labels:
                raise ValueError("An action item group names a speaker that was not supplied.")
    return "\n".join(lines[1:]).lstrip() + "\n"


def boundaries(document: str, duration: float) -> tuple[str, list[dict[str, Any]]]:
    if document.count(BOUNDARY_START) != 1 or document.count(BOUNDARY_END) != 1:
        raise ValueError("The write-up boundary block is missing or repeated.")
    prose, block = document.split(BOUNDARY_START)
    encoded, trailing = block.split(BOUNDARY_END)
    if trailing.strip():
        raise ValueError("The boundary block must end the response.")
    rows = json.loads(encoded)
    if not isinstance(rows, list) or len(rows) > 50:
        raise ValueError("The boundary block must be a list of at most 50 ranges.")
    parsed = [RecordingBoundary.model_validate(row) for row in rows]
    if any(row.end_seconds > duration for row in parsed):
        raise ValueError("A boundary extends beyond the timestamped transcript.")
    if parsed and "## Recording boundaries" not in prose:
        raise ValueError("The write-up must explain the recording boundaries in readable prose.")
    return prose, [row.model_dump() for row in parsed]


# A date the recorder put at the front of its own title, in any of the forms it uses.
_LEADING_DATE = re.compile(
    r"^(?:(?P<year>\d{4})[-/.])?(?P<month>\d{1,2})[-/.](?P<day>\d{1,2})(?!\d)[\s:_\-]*"
)
# A bare recorder prefix says nothing once the date is there ("Operations Meeting:" does).
_GENERIC_PREFIX = re.compile(r"^Meeting\s*:\s*", re.I)


def clean_title(title: str, day: date) -> str:
    """The recorder's title without a leading date that is the recording's own date."""
    title = " ".join(title.split()).lstrip("# ")
    found = _LEADING_DATE.match(title)
    if found and (
        int(found["month"]) == day.month
        and int(found["day"]) == day.day
        and (found["year"] is None or int(found["year"]) == day.year)
    ):
        title = title[found.end() :]
    generic = _GENERIC_PREFIX.match(title)
    if generic and title[generic.end() :].strip():
        title = title[generic.end() :]
    return title.strip() or "Recording"


def local_instant(value: Any, zone: ZoneInfo) -> datetime | None:
    """A timestamp in the lab's zone; one without an offset is already lab-local."""
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed.replace(tzinfo=zone) if parsed.tzinfo is None else parsed.astimezone(zone)


def unresolved_reason(captured: bool, found: int, error: Exception | None) -> str:
    """Plain words for why no calendar event was chosen."""
    if not captured:
        return "the recording has no capture time to look up"
    if error is not None:
        return f"the calendar lookup failed ({type(error).__name__}); review can try again"
    if found == 0:
        return "no calendar event overlaps the recording's start"
    return f"{found} calendar events overlap the recording's start and none stood out"


async def resolve_title(context: Any, job: Any, transcript: str) -> tuple[str, dict[str, Any]]:
    zone = ZoneInfo(context.store.control.settings().general.timezone)
    fields = dict(job.frontmatter)
    date_key = context.store.control.schema().role("date_key")
    captured = local_instant(job.captured_at, zone)
    stamp = (
        captured or local_instant(fields.get(date_key), zone) or local_instant(job.created_at, zone)
    )
    assert stamp is not None
    # Notes keep their date as local time without an offset; an offset-qualified value is
    # not a usable note date, so every calendar lookup would find nothing.
    if not parse_note_time(fields.get(date_key)).valid:
        fields[date_key] = stamp.replace(tzinfo=None, microsecond=0).isoformat()
    evidence: dict[str, Any] = {"matched": False}
    found, error = 0, None
    try:
        events = await context.calendar_candidates(SimpleNamespace(frontmatter=fields))
        events = meeting_match.overlapping(events, captured.isoformat()) if captured else []
        found = len(events)
        linked_field = context.config().assignments["enrichment"].event_field
        linked = fields.get(linked_field) if linked_field else None
        chosen = [event for event in events if event["event_id"] == linked] if linked else []
        if not chosen:
            if len(events) == 1:
                chosen = events
            else:
                suggested = meeting_match.suggest(events, transcript)
                chosen = [event for event in events if event["event_id"] == suggested]
        if len(chosen) == 1:
            evidence = {"matched": True, "event": chosen[0]}
    except Exception as exc:
        # Calendar failure cannot stop audio intake. Review may resolve the event later.
        error = exc
        get_logger("coppermind-store").warning(
            "recording_calendar_lookup_failed", recording=job.id, error=type(exc).__name__
        )
    if not evidence["matched"]:
        reason = unresolved_reason(captured is not None, found, error)
        evidence["reason"] = reason
        get_logger("coppermind-store").info(
            "recording_calendar_unresolved", recording=job.id, reason=reason
        )
    if evidence["matched"]:
        title = re.sub(r"^\d{4}-\d{2}-\d{2}\s+", "", evidence["event"]["title"].strip())
        title = " ".join(title.split()).lstrip("# ") or "Recording"
    else:
        title = clean_title(job.title, stamp.date())
    return f"{stamp.date().isoformat()} {title}", evidence


async def generate(
    context: Any, job: Any, transcript: dict[str, Any], voice_map: dict[str, str]
) -> dict[str, Any]:
    from coppermind_store.recordings import render_transcript

    key = "recording-writeups/" + job.id
    saved = context.read(key, {})
    if saved.get("body"):
        return saved
    async with context.lock:
        limits = assignment(context)
    if not limits.enabled or limits.paused or not limits.connection:
        raise ProviderError(failure_message("writeup-settings"), "writeup-settings")
    text = render_transcript(transcript)
    title, calendar = await resolve_title(context, job, text)
    labels = sorted(
        {segment["speaker"] for segment in transcript["segments"] if segment.get("speaker")}
    )
    duration = max(
        [
            float(segment.get("end") or segment.get("start") or 0)
            for segment in transcript["segments"]
        ]
        + [float(transcript.get("duration") or 0)]
    )
    evidence = {
        "resolved_title": title,
        "allowed_speaker_labels": labels,
        "transcript": text,
        "calendar": calendar,
        "duration_seconds": duration,
    }
    if len(json.dumps(evidence, ensure_ascii=False)) > limits.summary_max_input_chars:
        raise ProviderError(failure_message("writeup-input-limit"), "writeup-input-limit")
    directions = limits.writeup_prompt or DEFAULT_PROMPT
    system = (
        directions.replace("{resolved_title}", title)
        + "\n\n"
        + LOCKED_RULES
        + "\n\n"
        + BOUNDARY_RULES
    )
    try:
        document, usage = await context.provider.write_text(
            context.connection(limits.connection),
            context.key(limits.connection),
            limits.model_copy(update={"max_input_chars": limits.summary_max_input_chars}),
            system,
            evidence,
        )
        markdown, ranges = boundaries(document, duration)
        body = validate(markdown, title, labels)
    except ProviderError as exc:
        rule = (
            "writeup-transient"
            if exc.waiting or exc.rule == "provider-dropped"
            else "writeup-" + exc.rule
        )
        raise ProviderError(
            failure_message(rule),
            rule,
            reply=exc.reply,
            cut_short=exc.cut_short,
        ) from exc
    except ValueError as exc:
        raise ProviderError(failure_message("writeup-invalid"), "writeup-invalid") from exc
    if not calendar["matched"]:
        body += (
            f"\n_Calendar matching was unresolved: {calendar['reason']}; "
            "the recording title was used._\n"
        )
    saved = {
        "version": 1,
        "title": title,
        "document": document,
        "boundaries": ranges,
        "body": body,
        "transcript": transcript,
        "transcript_hash": hashlib.sha256(text.encode()).hexdigest(),
        "voice_map": voice_map,
        "voice_map_hash": hashlib.sha256(
            json.dumps(voice_map, sort_keys=True).encode()
        ).hexdigest(),
        "prompt": system,
        "prompt_hash": hashlib.sha256(system.encode()).hexdigest(),
        "model": limits.model,
        "usage": usage,
        "calendar": calendar,
    }
    context.write(key, saved)
    return saved
