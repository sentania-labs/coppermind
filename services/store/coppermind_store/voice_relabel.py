"""Conservative speaker-label updates for recording voice assignments."""

from __future__ import annotations

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


async def relabel_recording(context: Any, assignment: dict[str, Any], old: str, new: str) -> None:
    """Update the transcript and unchanged generated summary lines for one appearance."""
    import json

    from coppermind.meeting_summary import MeetingSummary
    from coppermind.store_protocol import (
        IngestArtifact,
        IngestNote,
        IngestRequest,
        IngestSource,
        ReplaceNote,
    )
    from coppermind_store.recordings import render_transcript

    note = await context.store.get_note(assignment["note_id"])
    recording = next(
        (r for r in context.recordings.jobs() if r.note_id == note.id and r.source_id), None
    )
    if recording is None:
        raise ValueError("Recording no longer exists")
    manifest = await context.store.get_source(recording.source_id)
    artifact = await context.store.get_source_artifact(
        recording.source_id, manifest.current_revision, "transcript.json"
    )
    transcript = json.loads(artifact.content or "{}")
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


async def combine(context: Any, source: str, target: str) -> None:
    """Relabel all appearances before moving the voice's private signatures."""
    from coppermind_store.voice_store import VoiceStore

    voice = VoiceStore(context)
    unknowns = voice.unknown_summaries()
    if source not in unknowns or source == target:
        raise ValueError("Unknown voice not found")
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
    for assignment in voice.assignments(source):
        await relabel_recording(context, assignment, unknowns[source]["name"], name)
    voice.move_voice(source, target)


async def separate(context: Any, voice_id: str, note_id: str, label: str) -> str:
    """Separate a single recording appearance from its current voice."""
    from coppermind_store.voice_store import VoiceStore

    voice = VoiceStore(context)
    assignment = next(
        (a for a in voice.assignments(voice_id) if a["note_id"] == note_id and a["label"] == label),
        None,
    )
    if assignment is None:
        raise ValueError("Speaker assignment not found")
    if voice_id.startswith("unknown:"):
        old = voice.unknown_summaries()[voice_id]["name"]
    else:
        entity = context.store.entities.load().entities.get(voice_id.removeprefix("person:"))
        if not entity:
            raise ValueError("Person not found")
        old = entity.name
    serial = voice._load().get("next_unknown", 1)
    new_label = f"Unknown voice {serial}"
    await relabel_recording(context, assignment, old, new_label)
    result = voice.split_assignment(voice_id, note_id, label)
    if voice_id.startswith("person:"):
        voice.reject(note_id, new_label, voice_id.removeprefix("person:"))
    return result
