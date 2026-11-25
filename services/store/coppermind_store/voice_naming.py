"""Naming a voice from the note it was heard in.

The owner types a name under an unidentified voice in a recording note's Speakers section
and marks the note reviewed. That does exactly what combining the voice with a person in
People does: the voice is attached to the person, this note and every other note that used
the voice have the label replaced where the text was not hand-edited, and later recordings
recognise the voice. Nothing else is needed, and nothing is asked a second time.

A name that matches nobody in People makes a new person. A name that matches more than one
person names nobody: the note says why, and what was typed stays where it was typed.
"""

from __future__ import annotations

import logging
from typing import Any

from coppermind import speakers_section as sections
from coppermind.store_protocol import NotFound, ReplaceNote, StoreError, VersionConflict

log = logging.getLogger(__name__)

LOCAL_NOTE = " Too little speech to recognise this voice in later recordings."
NEW_NOTE = " New person added to People."


def candidates(people: list[Any], name: str, page: str = "") -> list[Any]:
    """The people this typed name could mean: the page a completed link points at, else an
    exact name or alias, else, for a single word, anyone with that first name."""
    if page:
        wanted = page.casefold()
        linked = [
            p
            for p in people
            if p.path
            and (
                p.path.removesuffix(".md").casefold() == wanted
                or p.path.removesuffix(".md").rsplit("/", 1)[-1].casefold() == wanted
            )
        ]
        if linked:
            return linked
    key = name.casefold()
    exact = [p for p in people if key in {p.name.casefold(), *(a.casefold() for a in p.aliases)}]
    if exact:
        return exact
    if " " not in key:
        return [p for p in people if p.name.casefold().split()[:1] == [key]]
    return []


def _active_people(context: Any) -> list[Any]:
    return [
        e
        for e in context.store.entities.load().entities.values()
        if e.kind == "person" and not e.retired_at and not e.redirect
    ]


def _ambiguous(name: str, found: list[Any]) -> str:
    names = ", ".join(sorted({p.name for p in found})[:4])
    return (
        f"more than one person in People matches {name} ({names}). Type the full name "
        "of the right one and save the note."
    )


def _once(found: Any) -> list[Any]:
    """One entry per voice and typed name, however many Speakers sections repeat it."""
    first: dict[tuple[str, str], Any] = {}
    for entry in found:
        first.setdefault((entry.label, sections.clean_name(entry.typed)), entry)
    return list(first.values())


async def name_voices(context: Any, note: Any) -> Any:
    """Apply every name typed into this note's Speakers section. Returns the note as it now
    stands. The caller holds the note lifecycle lock; the context lock is taken here."""
    from coppermind.entities import EntityInput
    from coppermind.store_protocol import ValidationFailed

    typed = _once(e for e in sections.entries(note.body) if sections.clean_name(e.typed))
    ticked = _once(
        e for e in sections.entries(note.body) if e.me and not sections.clean_name(e.typed)
    )
    if not typed and not ticked:
        return note
    problems: list[tuple[str, str, str]] = []
    plans: list[dict[str, Any]] = []
    for entry in ticked:
        problems.append(
            (entry.label, entry.typed, 'Type your name after "Name this voice:" as well.')
        )
    for entry in typed:
        name, page = sections.read_name(entry.typed)
        found = candidates(_active_people(context), name, page)
        if len(found) > 1:
            problems.append(
                (entry.label, sections.clean_name(entry.typed), _ambiguous(name, found))
            )
            continue
        created = False
        if found:
            person = found[0]
        else:
            try:
                made = await context.store.entities.create(EntityInput(kind="person", name=name))
            except (ValidationFailed, ValueError) as exc:
                problems.append(
                    (
                        entry.label,
                        sections.clean_name(entry.typed),
                        f"{name} cannot be added to People: {_why(exc)}",
                    )
                )
                continue
            person = context.store.entities.load().entities[made["id"]]
            created = True
        plans.append(
            {
                "label": entry.label,
                "typed": sections.clean_name(entry.typed),
                "person": person,
                "created": created,
                "me": entry.me,
            }
        )
    async with context.lock:
        for plan in plans:
            try:
                plan["local"] = await _attach(context, note, plan)
            except ValueError as exc:
                problems.append((plan["label"], plan["typed"], _why(exc)))
                plan["failed"] = True
        return await _write(context, note, plans, problems)


def _why(exc: Exception) -> str:
    detail = getattr(exc, "details", None) or getattr(exc, "errors", None)
    text = "; ".join(str(d) for d in detail) if detail else str(exc)
    return text.strip().rstrip(".") + "."


async def _attach(context: Any, note: Any, plan: dict[str, Any]) -> bool:
    """Attach the voice to the person. True when the voice had no saved signature, so only
    this recording's transcript was relabelled."""
    from coppermind_store.context_service import now
    from coppermind_store.voice_relabel import add_late_attendee, combine, relabel_recording
    from coppermind_store.voice_store import VoiceStore

    label, person = plan["label"], plan["person"]
    voice = VoiceStore(context)
    unknown = next(
        (
            identity
            for identity, item in voice.unknown_summaries().items()
            if item["name"] == label and any(a["note_id"] == note.id for a in item["assignments"])
        ),
        None,
    )
    assignment: dict[str, Any] | None = None
    local = False
    if unknown:
        assignment = next(a for a in voice.assignments(unknown) if a["note_id"] == note.id)
        await combine(context, unknown, "person:" + person.id, note.id)
    else:
        local = True
        assignment = await _relabel_local(context, note, label, person.name, relabel_recording)
        if assignment is None:
            raise ValueError("This voice is not in the recording's transcript any more.")
    await add_late_attendee(context, assignment, person.id)
    if plan["me"] and not context.read("owner", {}).get("person_id"):
        context.write("owner", {"person_id": person.id, "at": now()})
    return local


async def _relabel_local(
    context: Any, note: Any, label: str, name: str, relabel: Any
) -> dict[str, Any] | None:
    for recording in context.recordings.jobs():
        if (
            context.recordings.parent_note_id(recording) != note.id
            or recording.generation_mode != "markdown-v1"
            or not recording.source_id
        ):
            continue
        saved = context.read("recording-writeups/" + recording.id, {})
        segments = saved.get("transcript", {}).get("segments", [])
        positions = [i for i, s in enumerate(segments) if s.get("speaker") == label]
        if not positions:
            continue
        original = next((k for k, v in saved.get("voice_map", {}).items() if v == label), label)
        assignment = {
            "note_id": note.id,
            "source_id": recording.source_id,
            "label": original,
            "segments": positions,
        }
        await relabel(context, assignment, label, name)
        return assignment
    return None


async def _write(
    context: Any, note: Any, plans: list[dict[str, Any]], problems: list[tuple[str, str, str]]
) -> Any:
    """Rewrite the section to say what happened. Retried once if the note was edited while
    the voices were being named: the names are already attached by then."""
    for attempt in range(2):
        try:
            current = await context.store.get_note(note.id)
        except NotFound:
            return note
        body = current.body
        owner_id = context.read("owner", {}).get("person_id", "")
        for plan in plans:
            if plan.get("failed"):
                continue
            person = plan["person"]
            how = "Named by you." + (NEW_NOTE if plan["created"] else "")
            if plan.get("local"):
                how += LOCAL_NOTE
            link = (context.store.entities.load().entities[person.id].path or "").removesuffix(
                ".md"
            )
            body = sections.name_voice(
                body,
                plan["label"],
                plan["typed"],
                person.name,
                link,
                owner=person.id == owner_id,
                how=how,
            )
        for label, typed, reason in problems:
            body = sections.note_problem(body, label, typed, reason)
        body = sections.tidy(body)
        if body == current.body:
            return current
        try:
            return await context.store.replace_note(
                note.id,
                ReplaceNote(frontmatter=current.frontmatter, body=body),
                if_match=current.content_hash,
            )
        except VersionConflict:
            if attempt:
                log.warning("speaker names for note %s were attached but the note moved", note.id)
        except StoreError as exc:
            log.warning("speaker section of note %s was not rewritten: %s", note.id, exc)
            break
    return await context.store.get_note(note.id)
