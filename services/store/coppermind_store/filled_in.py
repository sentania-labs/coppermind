"""Notes arrive filled in: what a recording's write-up answers goes straight into the waiting
note's properties.

The write-up gives the note's type, account and tags. The voices it recognised and the meeting
it matched give the attendees and the meeting. Each answer goes in only where the owner has not
put something of their own: a blank, a value the schema gives every note, or a value Coppermind
itself filled and the owner has not changed since. So writing a note up again refreshes what
Coppermind filled and leaves everything the owner typed or changed alone.

`what-was-filled` keeps, per note, the value Coppermind last put in each property. That is the
only record of who wrote a value, and nothing about it is shown to the owner.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa

from coppermind.db.models import Note
from coppermind.naming import sanitize_folder
from coppermind.schema import FrontmatterSchema, field_suggestion_changes
from coppermind.store_protocol import ReplaceNote
from coppermind_store.dossier_context import blank

log = logging.getLogger("coppermind.store.filled_in")

FILLED = "what-was-filled/"
# An invited list longer than this is a company-wide event, not who was in the room.
MOST_INVITED = 25


def _answered(saved: dict[str, Any], field: str) -> Any:
    for item in saved.get("suggestions", []):
        if item.get("field") == field and not blank(item.get("value")):
            return item["value"]
    return None


def _entities(context: Any) -> list[Any]:
    return [
        e
        for e in context.store.entities.load().entities.values()
        if e.kind == "person" and not e.retired_at and not e.redirect
    ]


def _link(person: Any) -> str:
    return "[[" + person.path.removesuffix(".md") + "]]" if person.path else person.name


def attendees(context: Any, saved: dict[str, Any]) -> list[str]:
    """Who was there: the people whose voices were recognised, and the people on the invitation
    of the meeting the recording matched. A name two people share is left out: it does not say
    which of them it was."""
    people = _entities(context)
    by_name: dict[str, list[Any]] = {}
    by_email: dict[str, Any] = {}
    for person in people:
        by_name.setdefault(person.name, []).append(person)
        for email in person.emails:
            by_email.setdefault(email.casefold(), person)
    found: list[str] = []

    def add(value: str) -> None:
        if value and value not in found:
            found.append(value)

    for name in dict.fromkeys(saved.get("voice_map", {}).values()):
        group = by_name.get(name, [])
        if len(group) == 1:
            add(_link(group[0]))
    event = (saved.get("calendar") or {}).get("event") or {}
    invited = event.get("people") or []
    if len(invited) <= MOST_INVITED:
        for who in invited:
            address = str(who.get("address", "")).partition(":")[2]
            person = by_email.get(address.casefold())
            if person is not None:
                add(_link(person))
            elif address:
                add(address)
    return found


def answers(context: Any, saved: dict[str, Any]) -> dict[str, Any]:
    """The values for the note's properties from a write-up, by the property's own name. Only
    properties the note schema has are named."""
    schema = context.store.control.schema()
    assignment = context.config().assignments["enrichment"]
    have = {key.name for key in schema.keys}
    found: dict[str, Any] = {}
    account = _answered(saved, "account")
    kind = _answered(saved, "type")
    tags = _answered(saved, "tags")
    if account:
        found[schema.role("account_key")] = account if isinstance(account, list) else [account]
        # An account means a customer meeting, and a customer meeting is work.
        kind = kind or "work"
    if kind:
        found[schema.role("type_key")] = kind
    if tags:
        tags = schema.normalize_tags(tags if isinstance(tags, list) else [tags])
        if schema.tag_mode == "closed":
            tags = [t for t in tags if t in schema.tags]
        if tags:
            found[schema.role("tags_key")] = tags
    # The properties Coppermind owns for people and the meeting are filled whenever the note
    # has them, whether or not the owner ever turned the suggestion step on.
    attendees_field = assignment.attendees_field or "attendees"
    event_field = assignment.event_field or "calendar_event"
    people = attendees(context, saved)
    if people:
        found[attendees_field] = people
    event = ((saved.get("calendar") or {}).get("event") or {}).get("event_id")
    if event:
        found[event_field] = event
    return {key: value for key, value in found.items() if key in have}


def untouched(schema: FrontmatterSchema, key: str, current: Any, filled: dict[str, Any]) -> bool:
    """Whether the owner has not put their own value here."""
    if blank(current):
        return True
    if key in filled and current == filled[key]:
        return True
    default = next((k.default for k in schema.keys if k.name == key), None)
    return not blank(default) and current == default


def fill(
    context: Any, frontmatter: dict[str, Any], found: dict[str, Any], filled: dict[str, Any]
) -> dict[str, Any]:
    """The properties to write: each answer whose place is untouched, and that the note's
    schema accepts. Nothing the owner typed or changed is in it."""
    schema = context.store.control.schema()
    changes: dict[str, Any] = {}
    # A note that is only being made has not been given the values the schema gives every note
    # yet; the rules are checked as if it had.
    given = {k.name: k.default for k in schema.keys if k.default is not None}
    refused = set(schema.invalid_keys({**given, **frontmatter}))
    for key, value in found.items():
        current = frontmatter.get(key)
        if current == value or not untouched(schema, key, current, filled):
            continue
        probe = {**given, **frontmatter, **changes, key: value}
        if (set(schema.invalid_keys(probe)) - refused) or schema.tag_problems(probe, frontmatter):
            continue
        changes[key] = value
    return changes


def recorded(context: Any, note_id: str) -> dict[str, Any]:
    return dict(context.read(FILLED + note_id, {}))


def remember(context: Any, note_id: str, frontmatter: dict[str, Any], keys: Any) -> None:
    """Keep what the note now says in each property Coppermind answered for."""
    kept = recorded(context, note_id)
    kept.update({key: frontmatter.get(key) for key in keys})
    context.write(FILLED + note_id, kept)


UPGRADED = "filled-in-upgrade"


async def upgrade(context: Any) -> int:
    """Once, when Coppermind is first started with notes that arrive filled in: fill the notes
    already waiting in Review with the answers that were waiting for the owner to accept, where
    the owner has put nothing of their own. Returns how many notes were filled in."""
    if context.read(UPGRADED, None) is not None:
        return 0
    folder = sanitize_folder(context.store.control.settings().notes.review_folder)
    filled = 0
    if folder:
        async with context.store.session_factory() as session:
            ids = list(
                (
                    await session.scalars(
                        sa.select(Note.id).where(
                            Note.state == "ok",
                            Note.reviewed.is_(False),
                            Note.path.startswith(f"{folder}/"),
                        )
                    )
                ).all()
            )
        newest: dict[str, Any] = {}
        for job in sorted(context.jobs(), key=lambda j: j.created_at):
            if job.purpose != "dossier" and job.state == "suggested" and job.result:
                newest[job.note_id] = job
        written = {
            r.note_id: r
            for r in context.recordings.jobs()
            if r.note_id and r.source_id and r.generation_mode == "markdown-v1"
        }
        for note_id in ids:
            try:
                filled += await _fill_waiting(context, note_id, newest.get(note_id), written)
            except Exception:  # noqa: BLE001
                log.exception("the waiting note %s could not be filled in", note_id)
    context.write(UPGRADED, {"at": datetime.now(UTC).isoformat(), "notes": filled})
    return filled


async def _fill_waiting(context: Any, note_id: str, job: Any, written: dict[str, Any]) -> int:
    note = await context.store.get_note(note_id)
    schema = context.store.control.schema()
    found: dict[str, Any] = {}
    recording = written.get(note_id)
    if recording is not None:
        found = answers(context, context.read("recording-writeups/" + recording.id, {}))
    if job is not None:
        allowed = context.allowed_fields(note_id)
        decided = {d.get("index") for d in job.decisions}
        for index, item in enumerate(job.result.suggestions):
            if item.kind == "field" and item.field in allowed and index not in decided:
                changes, _ = field_suggestion_changes(
                    note.frontmatter, schema, item.field, item.value
                )
                found.update({k: v for k, v in changes.items() if k not in found})
    kept = recorded(context, note_id)
    changes = fill(context, note.frontmatter, found, kept)
    if not changes:
        return 0
    await context.store.replace_note(
        note_id,
        ReplaceNote(frontmatter={**note.frontmatter, **changes}, body=note.body),
        if_match=note.content_hash,
    )
    after = await context.store.get_note(note_id)
    remember(context, note_id, after.frontmatter, changes)
    return 1
