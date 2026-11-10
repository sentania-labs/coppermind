"""Speaker candidates and exact label replacement for recording transcripts."""

from __future__ import annotations

import copy
import re
from typing import Any


def speaker_labels(transcript: dict[str, Any]) -> list[str]:
    """Only diarized labels are eligible for a name proposal."""
    return sorted(
        {
            segment["speaker"]
            for segment in transcript.get("segments", [])
            if isinstance(segment.get("speaker"), str)
            and re.fullmatch(r"Speaker [1-9][0-9]*", segment["speaker"])
        },
        key=lambda label: int(label.split()[1]),
    )


def rename_speakers(transcript: dict[str, Any], mapping: dict[str, str]) -> dict[str, Any]:
    """Replace whole segment labels, never words in the spoken text."""
    labels = set(speaker_labels(transcript))
    if not set(mapping) <= labels:
        raise ValueError("speaker no longer appears in the transcript")
    if any(not name.strip() or any(c in name for c in "\n\r\0") for name in mapping.values()):
        raise ValueError("speaker names must be nonblank single lines")
    result = copy.deepcopy(transcript)
    for segment in result["segments"]:
        if segment.get("speaker") in mapping:
            segment["speaker"] = mapping[segment["speaker"]]
    return result


async def evidence(context: Any, note: Any, events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Collect only a recording attached to this note and its candidate people."""
    import json

    recording = next(
        (r for r in context.recordings.jobs() if r.note_id == note.id and r.source_id), None
    )
    if recording is None:
        return None
    manifest = await context.store.get_source(recording.source_id)
    artifact = await context.store.get_source_artifact(
        recording.source_id, manifest.current_revision, "transcript.json"
    )
    transcript = json.loads(artifact.content or "{}")
    labels = speaker_labels(transcript)
    if not labels:
        return None
    candidates: dict[str, dict[str, Any]] = {}
    assignment = context.config().assignments["enrichment"]
    linked = set(context.read("associations", {}).get(note.id, {}).get("events", []))
    if assignment.event_field and note.frontmatter.get(assignment.event_field):
        linked.add(note.frontmatter[assignment.event_field])
    for event in events:
        if event["event_id"] not in linked:
            continue
        for person in event.get("people", []):
            identity = person.get("contact_id")
            name = person.get("name")
            if identity and name:
                candidates["contact:" + identity] = {
                    "id": "contact:" + identity,
                    "name": name,
                    "email": person.get("address", "").removeprefix("mailto:"),
                    "path": "",
                }
    from coppermind_store.entities import email

    by_email: dict[str, list[str]] = {}
    for entity in context.store.entities.load().entities.values():
        if entity.kind == "person" and not entity.retired_at and not entity.redirect:
            for address in entity.emails:
                canonical = email(address)
                if canonical:
                    by_email.setdefault(canonical, []).append(entity.path)
            candidates["person:" + entity.id] = {
                "id": "person:" + entity.id,
                "name": entity.name,
                "email": entity.emails[0] if entity.emails else "",
                "path": entity.path,
            }
    for candidate in candidates.values():
        paths = by_email.get(email(candidate["email"]) or "", [])
        if not candidate["path"] and len(paths) == 1:
            candidate["path"] = paths[0]
    from coppermind_store.recordings import render_transcript

    return {
        "source_id": recording.source_id,
        "revision": manifest.current_revision,
        "speakers": labels,
        "candidates": list(candidates.values()),
        "transcript": render_transcript(transcript),
    }


def people_changes(
    context: Any, frontmatter: dict[str, Any], people: list[dict[str, Any]]
) -> dict[str, Any]:
    """The attendee and contact values a confirmed mapping adds, checked before any receipt."""
    from coppermind_store.dossier_context import blank

    assignment = context.config().assignments["enrichment"]
    if not assignment.attendees_field:
        raise ValueError("set up attendee properties before confirming speakers")
    links = ["[[" + p["path"].removesuffix(".md") + "]]" for p in people if p["path"]]
    attendees = [
        p["email"] or ("[[" + p["path"].removesuffix(".md") + "]]" if p["path"] else p["name"])
        for p in people
    ]
    if not assignment.contacts_field:
        attendees.extend(links)
    changes: dict[str, Any] = {}
    for field, values in (
        (assignment.attendees_field, attendees),
        (assignment.contacts_field, links),
    ):
        if not field:
            continue
        existing = changes.get(field, frontmatter.get(field))
        if blank(existing):
            existing = []
        if not isinstance(existing, list):
            raise ValueError("people fields must be lists")
        changes[field] = list(existing) + [v for v in dict.fromkeys(values) if v not in existing]
    if context.store.control.schema().validate_frontmatter({**frontmatter, **changes}):
        raise ValueError("confirmed people do not fit the note properties")
    return changes


async def apply(context: Any, receipt: dict[str, Any], note: Any) -> Any:
    """Apply a recorded mapping, replaying safely after a partial review."""
    import json

    from coppermind import frontmatter as fm
    from coppermind.store_protocol import (
        IngestArtifact,
        IngestNote,
        IngestRequest,
        IngestSource,
        PatchFrontmatter,
    )
    from coppermind_store.fs import content_hash, resolve
    from coppermind_store.recordings import render_transcript

    operation = receipt["speakers"]
    source_id, revision = operation["source_id"], operation["revision"]
    changes = operation["changes"]
    if "after_hash" not in operation:
        if note.content_hash != receipt["expected_hash"]:
            raise ValueError("note changed during review; inspect the recorded operation")
        raw = resolve(context.store.notes_root, note.path).read_bytes()
        from coppermind_store.notes import _with_kinds

        predicted = fm.patch(raw.decode(), _with_kinds(changes, context.store.control.schema()))
        operation["after_hash"] = content_hash(predicted.encode())
        context.write("review-completions/" + note.id, receipt)
    if note.content_hash not in {receipt["expected_hash"], operation["after_hash"]}:
        raise ValueError("note changed during review; inspect the recorded operation")
    manifest = await context.store.get_source(source_id)
    original = await context.store.get_source_artifact(source_id, revision, "transcript.json")
    transcript = rename_speakers(json.loads(original.content or "{}"), operation["mapping"])
    text = render_transcript(transcript)
    structured = json.dumps(transcript, ensure_ascii=False, indent=1)
    if manifest.current_revision != revision:
        current_json = await context.store.get_source_artifact(
            source_id, manifest.current_revision, "transcript.json"
        )
        current_text = await context.store.get_source_artifact(
            source_id, manifest.current_revision, "transcript.txt"
        )
        if (
            manifest.current_revision != revision + 1
            or current_json.content != structured
            or current_text.content != text
        ):
            raise ValueError("transcript changed during review; inspect the recorded operation")
    original_revision = next(r for r in manifest.revisions if r.revision == revision)
    await context.store.ingest(
        IngestRequest(
            source=IngestSource(
                provider=manifest.provider,
                external_source_id=manifest.external_source_id,
                source_type="transcript",
                origin=manifest.origin,
                captured_at=original_revision.captured_at,
                metadata=original_revision.metadata,
                artifacts=[
                    IngestArtifact(name="transcript.txt", mime_type="text/plain", content=text),
                    IngestArtifact(
                        name="transcript.json", mime_type="application/json", content=structured
                    ),
                ],
            ),
            note=IngestNote(title=note.title, body=note.body, frontmatter=note.frontmatter),
        ),
        expected_revision=manifest.current_revision,
    )
    if note.content_hash == operation["after_hash"]:
        return note
    if note.content_hash != receipt["expected_hash"]:
        raise ValueError("note changed during review; inspect the recorded operation")
    return await context.store.patch_frontmatter(
        note.id, PatchFrontmatter(set=changes), if_match=note.content_hash
    )
