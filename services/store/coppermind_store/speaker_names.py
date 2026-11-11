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
            and re.fullmatch(r"(?:Speaker|Unknown voice) [1-9][0-9]*", segment["speaker"])
        },
        key=lambda label: (int(label.rsplit(" ", 1)[1]), label),
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


SAMPLE_BUDGET = 12000
MIN_SAMPLE = 1000
MAX_CANDIDATES = 40
MAX_RECENT = 10
TURN_CHARS = 300
_ADDRESSING = re.compile(
    r"\b(?:[Hh]i|[Hh]ello|[Hh]ey|[Tt]hanks|[Tt]hank you),?\s+[A-Z][a-z]+|,\s+[A-Z][a-z]+\s*[?!.]"
)


def leftover_notice(count: int) -> str:
    noun = "speaker has" if count == 1 else "speakers have"
    return f"{count} more {noun} no suggestion. You can name them by hand below."


OMITTED_NOTICE = (
    "Speaker names could not be suggested for this recording because it is too long; "
    "you can still name speakers yourself."
)
ATTENDEES_UNSET_NOTICE = (
    "Attendees will not be updated because no attendees property is set up. Speaker names "
    "are still written to the transcript. To update attendees too, open Coppermind Admin, "
    "go to Model connections, and choose a property under Confirmed attendees."
)
REFRESH_NOTICE = (
    "Speaker suggestions could not be refreshed after the calendar event was linked; "
    "you can still choose people yourself."
)


def person_candidate(entity: Any) -> dict[str, Any]:
    return {
        "id": "person:" + entity.id,
        "name": entity.name,
        "email": entity.emails[0] if entity.emails else "",
        "path": entity.path,
    }


def current_person(people: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any] | None:
    """The candidate as it stands now, or None when that person has been retired or merged."""
    entity_id = candidate.get("person_id")
    if candidate["id"].startswith("person:"):
        entity_id = candidate["id"].removeprefix("person:")
    if not entity_id:
        return candidate
    entity = people.get(entity_id)
    if entity is None or entity.retired_at or entity.redirect:
        return None
    return {**person_candidate(entity), "id": candidate["id"], "person_id": entity_id}


def chosen_person(
    people: dict[str, Any], identity: str, candidates: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    """Resolve a supplied candidate or an explicitly selected active People entry."""
    candidate = candidates.get(identity)
    if candidate is not None:
        return current_person(people, candidate)
    if identity.startswith("person:"):
        entity = people.get(identity.removeprefix("person:"))
        if (
            entity is not None
            and entity.kind == "person"
            and not entity.retired_at
            and not entity.redirect
        ):
            return person_candidate(entity)
    return None


def _is_contact(person: dict[str, Any]) -> bool:
    return not person["id"].startswith("person:") and not person.get("person_id")


def _contact_match(context: Any, person: dict[str, Any]) -> Any:
    from coppermind_store.entities import email

    address = email(person.get("email", ""))
    name = person["name"].strip().casefold()
    people = [e for e in context.store.entities.load().entities.values() if e.kind == "person"]
    active = [e for e in people if not e.retired_at and not e.redirect]
    match = next(
        (e for e in active if address and address in {email(a) for a in e.emails}), None
    ) or next(
        (e for e in active if e.name.strip().casefold() == name and (not address or not e.emails)),
        None,
    )
    if match is None and address:
        for entity in people:
            if (
                entity.retired_at
                and not entity.redirect
                and address in {email(a) for a in entity.emails}
            ):
                raise ValueError(
                    f"{person['name']} matches a person who was removed. Choose someone else "
                    "or restore that person."
                )
    return match


def find_contact(context: Any, person: dict[str, Any]) -> dict[str, Any]:
    """An existing person for a contact-only choice, else the contact itself; never creates."""
    if not _is_contact(person):
        return person
    match = _contact_match(context, person)
    if match is None:
        return person
    return {**person_candidate(match), "id": person["id"], "person_id": match.id}


async def create_contact_people(context: Any, operation: dict[str, Any]) -> None:
    """Give each still-pending contact a People entry, reusing one an earlier attempt made."""
    from coppermind.entities import EntityInput
    from coppermind_store.entities import email

    for index, person in enumerate(operation["people"]):
        if not _is_contact(person):
            continue
        match = _contact_match(context, person)
        if match is None:
            address = email(person.get("email", ""))
            created = await context.store.entities.create(
                EntityInput(kind="person", name=person["name"], emails=[address] if address else [])
            )
            match = context.store.entities.load().entities[created["id"]]
        label = next(
            label
            for label, name in operation["mapping"].items()
            if name == person["name"] and not operation["person_ids"].get(label)
        )
        operation["people"][index] = {
            **person_candidate(match),
            "id": person["id"],
            "person_id": match.id,
        }
        operation["mapping"][label] = match.name
        operation["person_ids"][label] = match.id


def same_recording(left: dict[str, Any] | None, right: dict[str, Any] | None) -> bool:
    """Speakers follow from the immutable transcript revision; People do not matter here."""

    def identity(value: dict[str, Any] | None) -> tuple[Any, Any]:
        return (value or {}).get("source_id"), (value or {}).get("revision")

    return identity(left) == identity(right)


def sample(transcript: dict[str, Any], names: list[str], budget: int) -> list[str]:
    """A bounded share of turns per speaker: first, addressing or naming someone, longest."""
    from coppermind_store.recordings import render_transcript

    segments = transcript.get("segments", [])
    labels = speaker_labels(transcript)
    tokens = {t for name in names for t in [name, *name.split()[:1]] if len(t) >= 3}
    named = (
        re.compile(r"\b(?:" + "|".join(map(re.escape, sorted(tokens))) + r")\b", re.IGNORECASE)
        if tokens
        else None
    )

    def line(index: int) -> str:
        segment = dict(segments[index], text=str(segments[index].get("text", ""))[:TURN_CHARS])
        return render_transcript({"segments": [segment]}).rstrip("\n")

    chosen: set[int] = set()
    for label in labels:
        turns = [i for i, s in enumerate(segments) if s.get("speaker") == label]
        addressed = [
            i
            for i in turns
            if _ADDRESSING.search(str(segments[i].get("text", "")))
            or (named and named.search(str(segments[i].get("text", ""))))
        ]
        longest = sorted(turns, key=lambda i: (-len(str(segments[i].get("text", ""))), i))[:3]
        spent = 0
        for index in [*turns[:3], *addressed, *longest]:
            cost = len(line(index)) + 1
            if index not in chosen and spent + cost <= budget // len(labels):
                chosen.add(index)
                spent += cost
    return [line(i) for i in sorted(chosen)]


async def evidence(
    context: Any,
    note: Any,
    events: list[dict[str, Any]],
    budget: int = SAMPLE_BUDGET,
    keep: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any] | None:
    """A recording attached to this note, a bounded sample of it and a bounded list of people."""
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
    base = {"source_id": recording.source_id, "revision": manifest.current_revision}
    if budget < MIN_SAMPLE:
        return {**base, "speakers": [], "candidates": [], "excerpts": [], "omitted": True}
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

    by_email: dict[str, list[Any]] = {}
    people = []
    for entity in context.store.entities.load().entities.values():
        if entity.kind != "person":
            continue
        for address in entity.emails:
            canonical = email(address)
            if canonical:
                by_email.setdefault(canonical, []).append(entity)
        if not entity.retired_at and not entity.redirect:
            people.append(entity)
    haystack = " ".join(
        [
            note.title,
            note.body,
            json.dumps(note.frontmatter, ensure_ascii=False, default=str),
            *(str(s.get("text", "")) for s in transcript.get("segments", [])),
        ]
    ).casefold()
    words = set(re.findall(r"\w+", haystack))

    def mentioned(entity: Any) -> int:
        if entity.name.casefold() in haystack:
            return 2
        first = entity.name.casefold().split()[:1]
        return 1 if first and len(first[0]) >= 3 and first[0] in words else 0

    def met(entity: Any) -> str:
        return max((f.evidence.date for f in entity.facts if f.state == "confirmed"), default="")

    kept = [e for e in people if "person:" + e.id in keep]
    rest = [e for e in people if "person:" + e.id not in keep]
    named = sorted((e for e in rest if mentioned(e)), key=lambda e: (-mentioned(e), e.name, e.id))
    recent = sorted(
        (e for e in rest if met(e) and not mentioned(e)), key=lambda e: (met(e), e.id), reverse=True
    )[:MAX_RECENT]
    room = max(0, MAX_CANDIDATES - len(candidates) - len(kept))
    for entity in [*kept, *[*named, *recent][:room]]:
        candidates["person:" + entity.id] = person_candidate(entity)
    for identity, candidate in list(candidates.items()):
        matches = by_email.get(email(candidate["email"]) or "", [])
        if not candidate["path"] and len(matches) == 1:
            if matches[0].retired_at or matches[0].redirect:
                del candidates[identity]
                continue
            candidate["path"] = matches[0].path
            candidate["person_id"] = matches[0].id
    listed = list(candidates.values())
    return {
        **base,
        "speakers": labels,
        "candidates": listed,
        "excerpts": sample(transcript, [c["name"] for c in listed], budget),
    }


def people_changes(
    context: Any, frontmatter: dict[str, Any], people: list[dict[str, Any]]
) -> dict[str, Any]:
    """The attendee and contact values a confirmed mapping adds, checked before any receipt."""
    from coppermind_store.dossier_context import blank

    assignment = context.config().assignments["enrichment"]
    if not assignment.attendees_field:
        return {}
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
    if "after_hash" not in operation:
        if note.content_hash != receipt["expected_hash"]:
            raise ValueError("note changed during review; inspect the recorded operation")
        operation["changes"] = people_changes(context, note.frontmatter, operation["people"])
        raw = resolve(context.store.notes_root, note.path).read_bytes()
        from coppermind_store.notes import _with_kinds

        predicted = fm.patch(
            raw.decode(), _with_kinds(operation["changes"], context.store.control.schema())
        )
        operation["after_hash"] = content_hash(predicted.encode())
        context.write("review-completions/" + note.id, receipt)
    changes = operation["changes"]
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


def voice_suggestions(context: Any, note_id: str) -> dict[str, list[dict[str, Any]]]:
    """Return names and strength only; pending and saved vectors stay private."""
    from coppermind_store.voice_store import VoiceStore

    recording = next((r for r in context.recordings.jobs() if r.note_id == note_id), None)
    if recording is None:
        return {}
    pending = context.read("voice-pending/" + recording.id, [])
    settings = context.read("voice-settings", {"strong": 0.60, "maybe": 0.45})
    people = context.store.entities.load().entities
    results = {}
    for speaker in pending:
        if not isinstance(speaker, dict) or not isinstance(speaker.get("label"), str):
            continue
        embeddings = speaker.get("embeddings")
        if not isinstance(embeddings, list):
            continue
        matches = VoiceStore(context).match(embeddings, **settings)
        candidates = []
        for match in matches:
            if VoiceStore(context).rejected(note_id, speaker["label"], match["person_id"]):
                continue
            person = people.get(match["person_id"])
            if not person or person.kind != "person" or person.retired_at or person.redirect:
                continue
            candidates.append({**person_candidate(person), "strength": match["strength"]})
        if candidates:
            results[speaker["label"]] = candidates
    return results
