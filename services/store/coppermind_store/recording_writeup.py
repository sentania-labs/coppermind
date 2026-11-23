"""Durable Markdown generation before ordinary recording ingest."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from coppermind.context_config import FeatureAssignment
from coppermind.recording_writeup import (
    BOUNDARY_END,
    BOUNDARY_RULES,
    BOUNDARY_START,
    COMBINE_RULES,
    DEFAULT_PROMPT,
    LOCKED_RULES,
    PART_RULES,
    RecordingBoundary,
    failure_message,
)
from coppermind_store import meeting_match
from coppermind_store.answer_shape import FENCE
from coppermind_store.model_provider import ProviderError

log = logging.getLogger(__name__)

# How many times a long recording's notes are condensed before it is called too long.
CONDENSE_ROUNDS = 3
# Room left in a request for the JSON around the text.
SLACK_CHARS = 400


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
        or re.search(r"</?[A-Za-z][^<>]*>|<!--|^---\s*$|^```", text, re.M)
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


UNKNOWN_VOICE = re.compile(r"Unknown voice (\d+)( \(recording-local\))?", re.I)
YAML_OPENER = re.compile(r"^---[ \t]*\n[\w-]+:", re.M)


BARE_FENCE = re.compile(r"```[\w-]*[ \t]*")


def _unfence(text: str) -> tuple[str, bool]:
    """The note without a code fence round it. A fence line just before the title opens one;
    the last fence line outside the boundary block closes it."""
    lines = text.splitlines()
    title = next((i for i, line in enumerate(lines) if re.match(r"# \S", line)), None)
    if title is None:
        return text, False
    opening = next(
        (i for i in range(title - 1, max(title - 5, -1), -1) if BARE_FENCE.fullmatch(lines[i])),
        None,
    )
    if opening is None:
        return text, False
    start = next((i for i, line in enumerate(lines) if BOUNDARY_START in line), len(lines))
    end = next((i for i, line in enumerate(lines) if BOUNDARY_END in line), start)
    closing = [
        i
        for i, line in enumerate(lines)
        if i > title and not start <= i <= end and re.fullmatch(r"```[ \t]*", line)
    ]
    drop = {opening, *closing[-1:]}
    return "\n".join(line for i, line in enumerate(lines) if i not in drop), True


def _split_boundary_block(document: str) -> tuple[str, str | None]:
    """The prose, and the text between the boundary markers (None when there is no block)."""
    if BOUNDARY_START not in document:
        return document, None
    prose, _, rest = document.partition(BOUNDARY_START)
    encoded = rest.partition(BOUNDARY_END)[0]
    return prose, encoded


def _boundary_rows(encoded: str | None, duration: float, repairs: list[str]) -> list[Any]:
    """The usable boundary records. A part that cannot be read is dropped, never the note."""
    if encoded is None:
        repairs.append("the boundary block was missing and was read as none")
        return []
    text = encoded.strip()
    fenced = FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
        repairs.append("removed a code fence around the boundary block")
    try:
        rows = json.loads(text or "[]")
    except ValueError:
        start, end = text.find("["), text.rfind("]")
        try:
            rows = json.loads(text[start : end + 1]) if 0 <= start < end else None
        except ValueError:
            rows = None
        if rows is None:
            repairs.append("the boundary block could not be read and was read as none")
            return []
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        repairs.append("the boundary block was not a list and was read as none")
        return []
    kept: list[Any] = []
    for row in rows[:50]:
        try:
            fixed = _boundary_row(row, duration)
            RecordingBoundary.model_validate(fixed)
        except (ValueError, TypeError, AttributeError, KeyError):
            continue
        if fixed != row:
            repairs.append("mended a boundary record")
        kept.append(fixed)
    if len(kept) != len(rows):
        repairs.append(f"dropped {len(rows) - len(kept)} boundary record(s) that were not usable")
    return kept


def _number(value: Any) -> Any:
    if isinstance(value, str):
        return float(value.strip())
    return value


def _boundary_row(row: Any, duration: float) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError("not a record")
    fixed = {key: row[key] for key in RecordingBoundary.model_fields if key in row}
    for key in ("start_seconds", "end_seconds"):
        fixed[key] = _number(fixed[key])
    for key in ("kind", "confidence"):
        if isinstance(fixed.get(key), str):
            fixed[key] = "_".join(fixed[key].strip().lower().replace("-", " ").split())
    if isinstance(fixed.get("evidence"), str):
        fixed["evidence"] = fixed["evidence"].strip()[:2000]
    if duration and fixed["end_seconds"] > duration:
        fixed["end_seconds"] = duration
    return fixed


def _boundary_summary(rows: list[Any]) -> str:
    lines = ["## Recording boundaries", ""]
    for row in rows:
        start, end = _stamp(row["start_seconds"]), _stamp(row["end_seconds"])
        lines.append(f"- {start} to {end}: {row['kind'].replace('_', ' ')}. {row['evidence']}")
    return "\n".join(lines) + "\n"


def _stamp(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 3600:02d}:{whole % 3600 // 60:02d}:{whole % 60:02d}"


def tidy(document: str, title: str, labels: list[str], duration: float) -> tuple[str, list[str]]:
    """Mend what a model that follows instructions loosely gets wrong, without changing what
    the note says: a code fence round it, a sentence before it, the wrong title line, dashes,
    divider lines, a speaker label written without its qualifier, and a missing or damaged
    machine-readable boundary block. Each mend is named, so a model that needs them can be
    told apart from one that does not. Anything this cannot mend is left for validate."""
    repairs: list[str] = []
    text, unwrapped = _unfence(document.strip())
    if unwrapped:
        repairs.append("removed a code fence around the note")
    prose, encoded = _split_boundary_block(text)
    if encoded is not None and text.rpartition(BOUNDARY_END)[2].strip():
        repairs.append("removed text after the boundary block")
    lines = prose.strip().splitlines()
    first = next((i for i, line in enumerate(lines) if re.match(r"# \S", line)), None)
    if first is not None and 0 < first <= 4:
        lines = lines[first:]
        repairs.append("removed text before the title")
    if lines and re.match(r"# \S", lines[0]):
        if lines[0] != "# " + title:
            lines[0] = "# " + title
            repairs.append("restored the supplied title")
        rest = lines[1:]
    elif lines and (lines[0].startswith("##") or not lines[0].startswith(("#", "{", "["))):
        rest = lines
        repairs.append("the title line was missing and was supplied")
    else:
        return document, repairs
    body = "\n".join(rest)
    if not YAML_OPENER.search(body):
        cleaned = re.sub(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*(?:\n|\Z)", "", body, flags=re.M)
        if cleaned != body:
            body = cleaned
            repairs.append("removed divider lines")
    if "\u2014" in body:
        body = re.sub(r"\s*\u2014\s*", ", ", body)
        repairs.append("replaced em-dashes")
    if re.search(r"^# \S", body, re.M):
        body = re.sub(r"^# (?=\S)", "## ", body, flags=re.M)
        repairs.append("made a second title a heading")

    def label(found: re.Match[str]) -> str:
        whole = found.group(0)
        if whole in labels:
            return whole
        for candidate in (
            f"Unknown voice {found.group(1)} (recording-local)",
            f"Unknown voice {found.group(1)}",
        ):
            if candidate in labels:
                repairs.append("completed a speaker label")
                return candidate
        return whole

    body = UNKNOWN_VOICE.sub(label, body)

    def owner(found: re.Match[str]) -> str:
        name = found.group(1)
        if name.strip() in labels:
            return found.group(0)
        plain = re.sub(r"[*_`:]+|\s*\([^)]*\)\s*$", "", name).strip().casefold()
        matches = [item for item in labels if item.casefold() == plain]
        if len(matches) == 1:
            repairs.append("matched an action item owner to a supplied name")
            return "### " + matches[0]
        return found.group(0)

    def owners(found: re.Match[str]) -> str:
        return re.sub(r"^### (.+)$", owner, found.group(0), flags=re.M)

    body = re.sub(r"^## Action items\s*$.*?(?=^## |\Z)", owners, body, flags=re.M | re.S)
    rows = _boundary_rows(encoded, duration, repairs)
    if rows and not re.search(r"^#{2,3}\s*recording boundaries\s*$", body, re.M | re.I):
        body = body.rstrip() + "\n\n" + _boundary_summary(rows)
        repairs.append("wrote the boundary section from the boundary records")
    elif rows:
        body = re.sub(
            r"^#{2,3}\s*recording boundaries\s*$",
            "## Recording boundaries",
            body,
            flags=re.M | re.I,
        )
    mended = (
        f"# {title}\n" + body.rstrip() + f"\n\n{BOUNDARY_START}\n{json.dumps(rows)}\n{BOUNDARY_END}"
    )
    return mended, repairs


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


async def resolve_title(context: Any, job: Any, transcript: str) -> tuple[str, dict[str, Any]]:
    zone = ZoneInfo(context.store.control.settings().general.timezone)
    fields = dict(job.frontmatter)
    date_key = context.store.control.schema().role("date_key")
    supplied = job.captured_at or fields.get(date_key) or job.created_at
    try:
        stamp = datetime.fromisoformat(str(supplied))
    except ValueError:
        stamp = datetime.fromisoformat(job.created_at)
    stamp = stamp.replace(tzinfo=zone) if stamp.tzinfo is None else stamp.astimezone(zone)
    fields.setdefault(date_key, stamp.isoformat())
    evidence: dict[str, Any] = {"matched": False}
    try:
        events = await context.calendar_candidates(SimpleNamespace(frontmatter=fields))
        events = meeting_match.overlapping(events, job.captured_at)
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
    except Exception:
        # Calendar failure cannot stop audio intake. Review may resolve the event later.
        evidence = {"matched": False}
    title = evidence["event"]["title"] if evidence["matched"] else job.title
    title = re.sub(r"^\d{4}-\d{2}-\d{2}\s+", "", title.strip())
    title = " ".join(title.split()).lstrip("# ") or "Recording"
    return f"{stamp.date().isoformat()} {title}", evidence


class WriteupError(ProviderError):
    """A write-up failed. `detail` is a sentence that says what was wrong, in words that never
    repeat what the model wrote."""

    def __init__(self, rule: str, detail: str = "", **kwargs: Any) -> None:
        super().__init__(failure_message(rule, detail), rule, **kwargs)
        self.detail = detail


def feedback(problem: str) -> str:
    return (
        "That answer could not be used: "
        + problem.rstrip(".")
        + ". Write the whole note again as one Markdown document, followed by the "
        "boundary block, fixing this."
    )


def add_usage(total: dict[str, Any], usage: dict[str, Any]) -> None:
    for name, value in usage.items():
        if type(value) in (int, float) and type(total.get(name)) in (int, float):
            total[name] += value
        elif name not in total:
            total[name] = value


def size_of(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False))


def split_transcript(transcript: dict[str, Any], budget: int) -> list[dict[str, Any]]:
    """Consecutive pieces of the transcript, each of which fits in `budget` characters once
    it is sent. A turn of speech is never split unless it alone is larger than a piece."""
    from coppermind_store.recordings import render_transcript

    pieces: list[dict[str, Any]] = []
    lines: list[str] = []
    first = last = 0.0
    used = 2  # the quotes round the text

    def close() -> None:
        nonlocal lines, used
        if lines:
            pieces.append({"text": "\n".join(lines) + "\n", "first": first, "last": last})
        lines, used = [], 2

    def cut(line: str, room: int) -> list[str]:
        """The line in parts whose sent size, with the line break after each, fits `room`."""
        found, part, cost = [], "", 2
        for char in line:
            width = len(json.dumps(char, ensure_ascii=False)) - 2
            if part and cost + width > room:
                # Break after the last space, so a word is not split.
                at = part.rfind(" ") + 1
                found.append(part[:at] if at else part)
                part = part[at:] if at else ""
                cost = 2 + sum(len(json.dumps(c, ensure_ascii=False)) - 2 for c in part)
            part += char
            cost += width
        return [*found, part]

    for segment in transcript["segments"]:
        line = render_transcript({"segments": [segment]}).rstrip("\n")
        start = float(segment.get("start") or 0)
        end = float(segment.get("end") or start)
        sizes = size_of(line) - 2 + 2
        for part in [line] if sizes <= budget - 2 else cut(line, budget - 2):
            cost = size_of(part) - 2 + 2
            if lines and used + cost > budget:
                close()
            if not lines:
                first = start
            lines.append(part)
            used += cost
            last = end
    close()
    return pieces


async def ask_text(
    context: Any, limits: FeatureAssignment, system: str, evidence: dict[str, Any], usage: dict
) -> str:
    try:
        text, used = await context.provider.write_text(
            context.connection(limits.connection),
            context.key(limits.connection),
            limits.model_copy(update={"max_input_chars": limits.summary_max_input_chars}),
            system,
            evidence,
        )
    except ProviderError as exc:
        rule = (
            "writeup-transient"
            if exc.waiting or exc.rule == "provider-dropped"
            else "writeup-" + exc.rule
        )
        raise WriteupError(rule, reply=exc.reply, cut_short=exc.cut_short, usage=exc.usage) from exc
    add_usage(usage, used)
    return text


async def condense(
    context: Any,
    limits: FeatureAssignment,
    job: Any,
    evidence: dict[str, Any],
    transcript: dict[str, Any],
    text: str,
    usage: dict[str, Any],
) -> dict[str, Any]:
    """The evidence for one request: the whole transcript when it fits, else notes written
    for consecutive parts of it, condensed further until they fit."""
    limit = limits.summary_max_input_chars
    if size_of({**evidence, "transcript": text}) <= limit:
        return {**evidence, "transcript": text}
    budget = limit - size_of({**evidence, "transcript": ""}) - SLACK_CHARS
    if budget < 500:
        raise WriteupError("writeup-input-limit")
    saved_key = "recording-writeup-parts/" + job.id
    saved = context.read(saved_key, {})
    notes: list[dict[str, Any]] = []
    pieces = split_transcript(transcript, budget)
    base = {key: evidence[key] for key in ("allowed_speaker_labels", "duration_seconds")}

    async def written(request: dict[str, Any]) -> str:
        key = hashlib.sha256(
            json.dumps([request, limits.model], sort_keys=True).encode()
        ).hexdigest()
        if not saved.get(key):
            saved[key] = (await ask_text(context, limits, PART_RULES, request, usage)).strip()
            if not saved[key]:
                raise WriteupError("writeup-provider-empty-reply")
            context.write(saved_key, saved)
        return saved[key]

    for number, piece in enumerate(pieces, 1):
        notes.append(
            {
                "part": number,
                "of": len(pieces),
                "covers": f"{_stamp(piece['first'])} to {_stamp(piece['last'])}",
                "notes": await written(
                    {
                        **base,
                        "part": number,
                        "of": len(pieces),
                        "covers": f"{_stamp(piece['first'])} to {_stamp(piece['last'])}",
                        "transcript": piece["text"],
                    }
                ),
            }
        )
    rest = {key: value for key, value in evidence.items() if key != "transcript"}
    for attempt in range(CONDENSE_ROUNDS + 1):
        if size_of({**rest, "part_notes": notes}) <= limit - SLACK_CHARS:
            break
        if attempt == CONDENSE_ROUNDS:
            raise WriteupError("writeup-input-limit")
        room = limit - size_of({**base, "part_notes": []}) - SLACK_CHARS
        groups: list[list[dict[str, Any]]] = [[]]
        for note in notes:
            if groups[-1] and size_of(groups[-1] + [note]) > room:
                groups.append([])
            groups[-1].append(note)
        if len(groups) == len(notes):
            # No two notes fit together. Condense each one on its own, and fail only if
            # they still cannot fit after the rounds are used.
            notes = [
                {**note, "notes": await written({**base, "part_notes": [note]})} for note in notes
            ]
            continue
        notes = [
            {
                "part": index,
                "of": len(groups),
                "covers": f"{group[0]['covers'].split(' to ')[0]} to "
                f"{group[-1]['covers'].split(' to ')[-1]}",
                "notes": group[0]["notes"]
                if len(group) == 1
                else await written({**base, "part_notes": group}),
            }
            for index, group in enumerate(groups, 1)
        ]
    return {**rest, "part_notes": notes}


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
        raise WriteupError("writeup-settings")
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
    directions = limits.writeup_prompt or DEFAULT_PROMPT
    system = (
        directions.replace("{resolved_title}", title)
        + "\n\n"
        + LOCKED_RULES
        + "\n\n"
        + BOUNDARY_RULES
    )
    usage: dict[str, Any] = {}
    evidence = await condense(context, limits, job, evidence, transcript, text, usage)
    if "part_notes" in evidence:
        system += "\n\n" + COMBINE_RULES
    correction = None
    repairs: list[str] = []
    for turn in (1, 2):
        try:
            document, used = await context.provider.write_text(
                context.connection(limits.connection),
                context.key(limits.connection),
                limits.model_copy(update={"max_input_chars": limits.summary_max_input_chars}),
                system,
                evidence,
                **({"correction": correction} if correction else {}),
            )
        except ProviderError as exc:
            rule = (
                "writeup-transient"
                if exc.waiting or exc.rule == "provider-dropped"
                else "writeup-" + exc.rule
            )
            raise WriteupError(
                rule, reply=exc.reply, cut_short=exc.cut_short, usage=exc.usage
            ) from exc
        add_usage(usage, used)
        try:
            tidied, repairs = tidy(document, title, labels, duration)
            markdown, ranges = boundaries(tidied, duration)
            body = validate(markdown, title, labels)
            break
        except ValueError as exc:
            # Only this module's own wording is kept: a parser's message can echo the answer.
            problem = (
                str(exc)
                if type(exc) is ValueError and str(exc)
                else "The boundary block could not be read."
            )
            if turn == 2:
                raise WriteupError(
                    "writeup-invalid", problem.rstrip(".") + ".", reply=document
                ) from exc
            correction = (document, feedback(problem))
    if repairs or correction:
        log.info(
            "recording write-up %s needed tidying: %s%s",
            job.id,
            "; ".join(repairs) or "none",
            " (after one retry)" if correction else "",
        )
    if not calendar["matched"]:
        body += "\n_Calendar matching was unresolved; the recording title was used._\n"
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
        "repairs": repairs,
        "parts": len(evidence["part_notes"]) if "part_notes" in evidence else 0,
    }
    context.write(key, saved)
    context.write("recording-writeup-parts/" + job.id, {})
    return saved
