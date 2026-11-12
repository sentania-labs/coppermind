"""Conservative speaker-label updates for recording voice assignments."""

from __future__ import annotations

import contextlib
import copy
import re
from typing import Any


def relabel_transcript(
    transcript: dict[str, Any], positions: list[int], old: str, new: str
) -> dict[str, Any]:
    """Change only the recorded turns that still have their expected speaker label."""
    result = copy.deepcopy(transcript)
    segments = result.get("segments", [])
    if not positions:
        raise ValueError("recording speaker positions are missing")
    for index in positions:
        if not isinstance(index, int) or index < 0 or index >= len(segments):
            raise ValueError("recording speaker positions changed")
        if segments[index].get("speaker") in (old, new):
            segments[index]["speaker"] = new
    return result


def relabel_generated_lines(
    body: str, baseline: str, generated: str, start: int, old: str, new: str
) -> tuple[str, str, str]:
    """Change only unique unchanged lines in the original generated append."""
    from difflib import SequenceMatcher

    lines = body.splitlines(keepends=True)
    saved = baseline.splitlines(keepends=True)
    original = generated.splitlines(keepends=True)
    pattern = re.compile(r"(?<!\w)" + re.escape(old) + r"(?!\w)")
    for block in SequenceMatcher(a=saved, b=lines, autojunk=False).get_matching_blocks():
        for offset in range(block.size):
            saved_index, current_index = block.a + offset, block.b + offset
            line = saved[saved_index]
            if saved_index < start or original.count(line) != 1:
                continue
            if saved.count(line) != 1 or lines.count(line) != 1:
                continue
            replacement = pattern.sub(lambda _: new, line)
            if replacement != line:
                lines[current_index] = saved[saved_index] = replacement
                original[original.index(line)] = replacement
    return "".join(lines), "".join(saved), "".join(original)


async def relabel_recording(
    context: Any, assignment: dict[str, Any], old: str | None, new: str
) -> None:
    """Update the transcript and unchanged generated summary lines for one appearance."""
    import json

    from coppermind.meeting_summary import MeetingSummary
    from coppermind.store_protocol import (
        IngestArtifact,
        IngestNote,
        IngestRequest,
        IngestSource,
        NotFound,
        ReplaceNote,
    )
    from coppermind_store.recordings import render_transcript
    from coppermind_store.voice_store import VoiceStore

    if assignment["note_id"].startswith("recording:"):
        job_id = assignment["note_id"].removeprefix("recording:")
        key = "voice-mappings/" + job_id
        mapping = context.read(key, {})
        mapping[assignment["label"]] = new
        context.write(key, mapping)
        return
    try:
        note = await context.store.get_note(assignment["note_id"])
    except NotFound:
        VoiceStore(context).purge(set(), {assignment["note_id"]})
        return
    recording = next(
        (
            r
            for r in context.recordings.jobs()
            if r.source_id
            and (
                r.source_id == assignment.get("source_id")
                if assignment.get("source_id")
                else context.recordings.parent_note_id(r) == note.id
            )
        ),
        None,
    )
    if recording is None:
        return
    manifest = await context.store.get_source(recording.source_id)
    artifact = await context.store.get_source_artifact(
        recording.source_id, manifest.current_revision, "transcript.json"
    )
    transcript = json.loads(artifact.content or "{}")
    if old is None:
        positions = assignment.get("segments", [])
        segments = transcript.get("segments", [])
        if not positions or any(
            type(i) is not int or i < 0 or i >= len(segments) for i in positions
        ):
            raise ValueError("recording speaker positions changed")
        labels = {segments[i].get("speaker") for i in positions}
        if len(labels) != 1 or not isinstance(next(iter(labels)), str):
            raise ValueError("recording speaker labels changed")
        old = labels.pop()
    renamed = relabel_transcript(transcript, assignment.get("segments", []), old, new)
    if renamed != transcript:
        revision = next(r for r in manifest.revisions if r.revision == manifest.current_revision)
        await context.store.ingest(
            IngestRequest(
                source=IngestSource(
                    provider=manifest.provider,
                    external_source_id=manifest.external_source_id,
                    source_type="transcript",
                    origin=manifest.origin,
                    captured_at=revision.captured_at,
                    metadata=revision.metadata,
                    artifacts=[
                        IngestArtifact(
                            name="transcript.txt",
                            mime_type="text/plain",
                            content=render_transcript(renamed),
                        ),
                        IngestArtifact(
                            name="transcript.json",
                            mime_type="application/json",
                            content=json.dumps(renamed, ensure_ascii=False, indent=1),
                        ),
                    ],
                ),
                note=IngestNote(title=note.title, body=note.body, frontmatter=note.frontmatter),
            ),
            expected_revision=manifest.current_revision,
        )
    pending_key = "voice-pending/" + recording.id
    pending = context.read(pending_key, [])
    for speaker in pending:
        if isinstance(speaker, dict) and speaker.get("label") == old:
            speaker["label"] = new
    if pending:
        context.write(pending_key, pending)
    receipt = context.read("review-completions/" + note.id, {})
    summary = receipt.get("summary") or {}
    if summary.get("generated") and receipt.get("summary_applied"):
        pending_update = summary.get("voice_pending")
        if pending_update and note.body == pending_update["body"]:
            summary.update(pending_update["state"])
            del summary["voice_pending"]
            context.write("review-completions/" + note.id, receipt)
        generated = summary.get("voice_generated")
        if generated is None:
            generated = MeetingSummary.model_validate(summary["generated"]).markdown() + "\n"
        baseline = summary.get("voice_body", summary.get("body", ""))
        appended = MeetingSummary.model_validate(summary["value"]).markdown()
        marker = "\n\n" + appended + "\n"
        start = summary.get("voice_start")
        if start is None:
            position = baseline.rfind(marker) if appended else -1
            if position < 0:
                return
            start = len(baseline[: position + 2].splitlines(keepends=True))
        body, saved, updated = relabel_generated_lines(
            note.body, baseline, generated, start, old, new
        )
        state = {"voice_generated": updated, "voice_body": saved, "voice_start": start}
        if body != note.body:
            summary["voice_pending"] = {"body": body, "state": state}
            context.write("review-completions/" + note.id, receipt)
            await context.store.replace_note(
                note.id,
                ReplaceNote(frontmatter=note.frontmatter, body=body),
                if_match=note.content_hash,
            )
        summary.update(state)
        summary.pop("voice_pending", None)
        context.write("review-completions/" + note.id, receipt)


async def unfinished_review(context: Any, note_id: str) -> str | None:
    """Title of a note whose recorded review is still being applied."""
    from coppermind.store_protocol import NotFound

    if note_id.startswith("recording:"):
        return None
    if context.read("review-completions/" + note_id, {}).get("state") != "applying":
        return None
    try:
        return (await context.store.get_note(note_id)).title
    except NotFound:
        return None


async def apply_deferred(context: Any, note_id: str | None = None) -> bool:
    """Rewrite deferred appearances whose reviews have finished. Callers hold the locks."""
    from coppermind_store.voice_store import VoiceStore

    voice = VoiceStore(context)
    ran = False
    for item in voice.deferred_relabels():
        if note_id not in (None, item["note_id"]):
            continue
        if await unfinished_review(context, item["note_id"]):
            continue
        with contextlib.suppress(ValueError):
            await relabel_recording(context, item["assignment"], None, item["name"])
        voice.clear_deferred(
            item["note_id"], item["label"], item.get("assignment", {}).get("source_id")
        )
        ran = True
    return ran


async def combine(context: Any, source: str, target: str, reviewing: str | None = None) -> None:
    """Relabel all appearances before moving the voice's private signatures."""
    from coppermind_store.voice_store import VoiceStore

    voice = VoiceStore(context)
    unknowns = voice.unknown_summaries()
    if source == target:
        raise ValueError("Choose a different voice or person.")
    if target.startswith("unknown:"):
        if target not in unknowns:
            raise ValueError("Target voice not found")
        name = unknowns[target]["name"]
    elif target.startswith("person:"):
        entity = context.store.entities.load().entities.get(target.removeprefix("person:"))
        if not entity or entity.kind != "person" or entity.retired_at or entity.redirect:
            raise ValueError("Target person not found")
        name = entity.name
    else:
        raise ValueError("Target voice not found")
    if source not in unknowns:
        voice.restore_unknown(source)
    deferred = []
    for assignment in voice.assignments(source):
        if assignment["note_id"] != reviewing and await unfinished_review(
            context, assignment["note_id"]
        ):
            deferred.append(assignment)
        else:
            await relabel_recording(context, assignment, None, name)
    for assignment in deferred:
        voice.defer_relabel(assignment, name)
    voice.move_voice(source, target)


async def separate(
    context: Any, voice_id: str, note_id: str, label: str, source_id: str | None = None
) -> str:
    """Separate a single recording appearance from its current voice."""
    from coppermind_store.voice_store import VoiceStore

    voice = VoiceStore(context)
    assignment = next(
        (
            a
            for a in voice.assignments(voice_id)
            if (a["note_id"], a["label"], a.get("source_id")) == (note_id, label, source_id)
        ),
        None,
    )
    if assignment is None:
        raise ValueError("Speaker assignment not found")
    if title := await unfinished_review(context, note_id):
        raise ValueError(
            f"Finish the review of {title} first. A voice cannot be separated from a note "
            "while its review is unfinished."
        )
    serial = voice._load().get("next_unknown", 1)
    new_label = f"Unknown voice {serial}"
    await relabel_recording(context, assignment, None, new_label)
    result = voice.split_assignment(voice_id, note_id, label, source_id)
    if voice_id.startswith("person:"):
        voice.reject(note_id, new_label, voice_id.removeprefix("person:"), source_id)
    return result
