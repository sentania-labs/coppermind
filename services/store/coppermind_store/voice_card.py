"""Who spoke, on a waiting note's card: the voices heard in a recording and who each one is.

A card lists one row per voice with its talk time, one line it said and a choice of who it is:
the people invited to the note's meeting first (with their email, so naming one never makes a
second person), then people already known, then someone new. Naming a voice writes straight
through. The voice is attached to that person exactly as combining it with a person in People
always did, so other notes that used the voice and recordings to come follow, and the person
joins the note's attendees. Nothing is proposed, and the body of the note is only touched where
an earlier generated line still says the old label.

Voices are told apart by the label the transcription service gave them, which never changes.
What a voice is called now is read from the voice store, so a name given anywhere shows here.
"""

from __future__ import annotations

import re
from typing import Any

from coppermind.entities import EntityInput
from coppermind.store_protocol import NotFound, PatchFrontmatter, StoreError, VersionConflict
from coppermind_store import filled_in
from coppermind_store.dossier_context import blank
from coppermind_store.speaker_facts import owner_person_id, quotes, talk_seconds

# An invitation longer than this is a company-wide event, not who was in the room.
MOST_INVITED = filled_in.MOST_INVITED
LEDGER = "voice-card/"
_NUMBER = re.compile(r"(\d+)")


class Refusal(ValueError):
    """A choice that cannot be made, in a sentence for the owner."""


def active_people(context: Any) -> dict[str, Any]:
    return {
        e.id: e
        for e in context.store.entities.load().entities.values()
        if e.kind == "person" and not e.retired_at and not e.redirect
    }


def shown_name(name: str, fallback: int) -> str:
    """A voice nobody has named is "Voice 2", never the service's own label."""
    found = _NUMBER.search(name or "")
    return f"Voice {found.group(1) if found else fallback}"


def recordings(context: Any, note_id: str, jobs: list[Any] | None = None) -> list[Any]:
    """The recordings whose voices belong to this note: the one it came from, and any merged in.
    A list that listing many notes has already read can be passed in."""
    recorder = context.recordings
    parent = getattr(recorder, "parent_note_id", lambda job: job.note_id)
    return [
        job
        for job in (recorder.jobs() if jobs is None else jobs)
        if job.source_id and getattr(job, "transcribed", False) and parent(job) == note_id
    ]


class Facts:
    """What the transcript of a recording says about each voice. It never changes after the
    recording is transcribed, so it is worked out once."""

    def __init__(self) -> None:
        self.kept: dict[str, list[dict[str, Any]]] = {}

    def of(self, context: Any, job: Any) -> list[dict[str, Any]]:
        if job.id in self.kept:
            return self.kept[job.id]
        try:
            transcript = context.recordings.saved_transcript(job)
        except Exception:  # noqa: BLE001
            return []
        spent = talk_seconds(transcript)
        found = []
        for label, seconds in spent.items():
            said = quotes(transcript, label, 1)
            found.append(
                {
                    "label": label,
                    "seconds": round(seconds, 1),
                    "said": said[0][0] if said else "",
                    "said_at": said[0][1] if said else "",
                }
            )
        self.kept[job.id] = found
        return found


def ledger(context: Any, note_id: str) -> dict[str, str]:
    """Voices with no saved signature (too little speech), and who the owner said they were."""
    kept = context.read(LEDGER + note_id, {})
    return kept if isinstance(kept, dict) else {}


def appearance(
    snapshot: dict[str, Any], note_id: str, job: Any, label: str
) -> dict[str, Any] | None:
    """The voice this label is in the voice store: where it stands for this recording."""
    return next(
        (
            a
            for a in snapshot["assignments"]
            if a["note_id"] == note_id
            and a["label"] == label
            and a.get("source_id") in (None, job.source_id)
        ),
        None,
    )


def identify(
    context: Any,
    snapshot: dict[str, Any],
    note_id: str,
    job: Any,
    label: str,
    people: dict[str, Any],
) -> Any:
    """Who a voice is now, as a person from People, or None while nobody has said."""
    person = people.get(ledger(context, note_id).get(job.id + "|" + label, ""))
    if person is not None:
        return person
    found = appearance(snapshot, note_id, job, label)
    if found is not None and found["voice_id"].startswith("person:"):
        return people.get(found["voice_id"].removeprefix("person:"))
    return None


def unknown_name(context: Any, snapshot: dict[str, Any], note_id: str, job: Any, label: str) -> str:
    """The "Voice N" a voice goes by while it is unnamed."""
    found = appearance(snapshot, note_id, job, label)
    if found is not None and found["voice_id"].startswith("unknown:"):
        return str(snapshot["unknowns"].get(found["voice_id"], ""))
    return str(context.read("voice-mappings/" + job.id, {}).get(label, ""))


def voices(
    context: Any,
    facts: Facts,
    note_id: str,
    snapshot: dict[str, Any] | None = None,
    jobs: list[Any] | None = None,
    people: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """The voices on a note's card, the owner first, then by who spoke most."""
    from coppermind_store.voice_store import VoiceStore

    snapshot = snapshot or VoiceStore(context).snapshot()
    people = active_people(context) if people is None else people
    owner = owner_person_id(context)
    rows: list[dict[str, Any]] = []
    for job in recordings(context, note_id, jobs):
        for index, fact in enumerate(facts.of(context, job), 1):
            person = identify(context, snapshot, note_id, job, fact["label"], people)
            rows.append(
                {
                    "id": job.id + "|" + fact["label"],
                    # A voice nobody has named is "Voice 2", the number its generated lines use;
                    # once it is a person, that person's name is what it is called.
                    "label": person.name
                    if person
                    else shown_name(
                        unknown_name(context, snapshot, note_id, job, fact["label"]), index
                    ),
                    "seconds": fact["seconds"],
                    "said": fact["said"],
                    "said_at": fact["said_at"],
                    "person": {"id": person.id, "name": person.name} if person else None,
                    "you": bool(person and owner and person.id == owner),
                }
            )
    rows.sort(key=lambda r: (not r["you"], r["person"] is None, -r["seconds"], r["id"]))
    # Two recordings merged into one note can both have a "Voice 2".
    seen: dict[str, int] = {}
    for row in rows:
        seen[row["label"]] = seen.get(row["label"], 0) + 1
    count: dict[str, int] = {}
    for row in rows:
        if seen[row["label"]] > 1:
            count[row["label"]] = count.get(row["label"], 0) + 1
            row["label"] += f" ({count[row['label']]})"
    return rows


def people_choices(context: Any) -> list[dict[str, str]]:
    """Everyone already in People, by name, for the choice on a card."""
    return [
        {"id": e.id, "name": e.name}
        for e in sorted(active_people(context).values(), key=lambda e: (e.name.casefold(), e.id))
    ]


async def invitees(
    context: Any,
    note: Any,
    meeting_key: str,
    jobs: list[Any] | None = None,
    people: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """The people on the invitation of the meeting this note names, with their email. Each says
    which person in People it already is, so the card never lists one twice."""
    saved = {}
    job = next(iter(recordings(context, note.id, jobs)), None)
    if job is not None:
        saved = context.read("recording-writeups/" + job.id, {})
    event = None
    named = note.frontmatter.get(meeting_key)
    if isinstance(named, str) and named:
        try:
            events = await context.calendar_candidates(note)
        except Exception:  # noqa: BLE001
            events = []
        event = next((e for e in events if e.get("event_id") == named), None)
        if (
            event is None
            and ((saved.get("calendar") or {}).get("event") or {}).get("event_id") == named
        ):
            event = saved["calendar"]["event"]
    if event is None:
        return []
    invited = event.get("people") or []
    if len(invited) > MOST_INVITED:
        return []
    by_email: dict[str, Any] = {}
    by_name: dict[str, list[Any]] = {}
    for person in (active_people(context) if people is None else people).values():
        by_name.setdefault(person.name.casefold(), []).append(person)
        for address in person.emails:
            by_email.setdefault(address.strip().casefold(), person)
    found: list[dict[str, str]] = []
    for who in invited:
        name = " ".join(str(who.get("name") or "").split())
        address = str(who.get("address", "")).partition(":")[2].strip()
        if not address and str(who.get("address", "")).count("@"):
            address = str(who["address"]).strip()
        if not name or "@" in name:
            continue
        # The same person the choice itself would find: by address, else by exact name when
        # that person has no address of their own to disagree.
        known = by_email.get(address.casefold()) if address else None
        if known is None:
            same = [p for p in by_name.get(name.casefold(), []) if not (address and p.emails)]
            known = same[0] if len(same) == 1 else None
        item = {"name": name, "email": address, "person_id": known.id if known else ""}
        if item not in found:
            found.append(item)
    return found


# -- naming ----------------------------------------------------------------------------------


def same_name(people: dict[str, Any], name: str, address: str) -> list[Any]:
    """The people an invitee could already be by name alone: exactly that name, and no address of
    their own that could disagree with the invitation's."""
    return [
        p
        for p in people.values()
        if p.name.casefold() == name.casefold() and not (address and p.emails)
    ]


def clean(name: Any) -> str:
    text = " ".join(str(name or "").split())
    if not text or len(text) > 200:
        raise Refusal("Type a name of up to 200 characters.")
    return text


def exact(people: dict[str, Any], name: str) -> list[Any]:
    """The people called this, by name or alias, and nothing looser."""
    key = name.casefold()
    return [
        p for p in people.values() if key in {p.name.casefold(), *(a.casefold() for a in p.aliases)}
    ]


async def create(context: Any, name: str, emails: list[str] | None = None) -> Any:
    from coppermind.store_protocol import ValidationFailed

    try:
        made = await context.store.entities.create(
            EntityInput(kind="person", name=name, emails=emails or [])
        )
    except (ValidationFailed, ValueError) as exc:
        detail = getattr(exc, "details", None) or getattr(exc, "errors", None)
        why = "; ".join(str(d) for d in detail) if detail else str(exc)
        raise Refusal(f"{name} cannot be added to People: {why.strip().rstrip('.')}.") from exc
    return context.store.entities.load().entities[made["id"]]


async def give_email(context: Any, person: Any, address: str) -> None:
    """A person found by name now has the address the invitation gave them, so the next
    invitation finds them by it. Best effort: naming the voice does not depend on it."""
    from coppermind.entities import EntityChange
    from coppermind.store_protocol import StoreError
    from coppermind_store.entities import email

    wanted = email(address)
    if not wanted or wanted in {email(a) for a in person.emails}:
        return
    try:
        await context.store.entities.change(
            person.id,
            EntityChange(version=person.version, action="aliases", emails=[*person.emails, wanted]),
        )
    except (StoreError, ValueError):
        return


async def person_for(context: Any, note: Any, meeting_key: str, choice: dict[str, Any]) -> Any:
    """The person a choice means, made only when nobody in People already is that person."""
    from coppermind_store import speaker_names

    people = active_people(context)
    if choice.get("you"):
        owner = people.get(owner_person_id(context))
        if owner is None:
            raise Refusal("Say which voice is you first.")
        return owner
    if choice.get("person"):
        person = people.get(str(choice["person"]))
        if person is None:
            raise Refusal("That person is no longer listed.")
        return person
    if isinstance(choice.get("invitee"), dict):
        wanted = choice["invitee"]
        address = str(wanted.get("email") or "").strip().casefold()
        name = clean(wanted.get("name"))
        # Only someone on this note's invitation can be chosen this way.
        listed = await invitees(context, note, meeting_key)
        if not any(i["name"] == name and i["email"].casefold() == address for i in listed):
            raise Refusal("That person is not on the invitation for this meeting.")
        # An address that is nobody's is found by name only when one person has that name.
        by_address = any(address in {a.casefold() for a in p.emails} for p in people.values())
        if not by_address and len(same_name(people, name, address)) > 1:
            raise Refusal(f"More than one person is called {name}. Choose one from the list.")
        try:
            match = speaker_names.contact_match(context, {"name": name, "email": address})
        except ValueError as exc:
            raise Refusal(str(exc)) from exc
        if match is not None:
            await give_email(context, match, address)
            return match
        return await create(context, name, [address] if address else [])
    name = clean(choice.get("new_name"))
    found = exact(people, name)
    if len(found) > 1:
        raise Refusal(f"More than one person is called {name}. Choose one from the list.")
    return found[0] if found else await create(context, name)


def find(context: Any, note_id: str, voice_id: str) -> tuple[Any, str]:
    job_id, _, label = voice_id.partition("|")
    job = next((j for j in recordings(context, note_id) if j.id == job_id), None)
    if job is None or not label:
        raise Refusal("This voice is no longer on the recording.")
    return job, label


async def attach(context: Any, note: Any, job: Any, label: str, person: Any) -> list[str]:
    """Make this voice that person here, in other notes that used it and in recordings to come.
    Returns the other notes that heard the same voice."""
    from coppermind_store.voice_relabel import combine, relabel_recording, separate
    from coppermind_store.voice_store import VoiceStore

    async with context.lifecycle.lock, context.lock:
        found = appearance(VoiceStore(context).snapshot(), note.id, job, label)
        if found is not None:
            current = found["voice_id"]
            if current == "person:" + person.id:
                return []
            others: list[str] = []
            if current.startswith("person:"):
                # Another person was named for it: it stops being theirs, then becomes this one's.
                current = await separate(context, current, note.id, label, found.get("source_id"))
            else:
                others = sorted(
                    {
                        a["note_id"]
                        for a in VoiceStore(context).assignments(current)
                        if a["note_id"] != note.id and not a["note_id"].startswith("recording:")
                    }
                )
            await combine(context, current, "person:" + person.id, note.id)
            return others
        transcript = context.recordings.saved_transcript(job)
        positions = [i for i, s in enumerate(transcript["segments"]) if s.get("speaker") == label]
        if not positions:
            raise Refusal("This voice is no longer on the recording.")
        await relabel_recording(
            context,
            {"note_id": note.id, "source_id": job.source_id, "label": label, "segments": positions},
            None,
            person.name,
        )
        kept = ledger(context, note.id)
        kept[job.id + "|" + label] = person.id
        context.write(LEDGER + note.id, kept)
        return []


def _known(person: Any) -> frozenset[str]:
    """Every way a note's attendees can write this person, in lower case."""
    known = {person.name.strip().casefold(), *(a.strip().casefold() for a in person.emails)}
    if person.path:
        known.add(("[[" + person.path.removesuffix(".md") + "]]").casefold())
    return frozenset(known)


async def change_attendees(
    context: Any,
    note_id: str,
    field: str,
    add: Any,
    drop: frozenset[str] = frozenset(),
) -> None:
    """Put a person among the note's attendees, written as a link to their page like every other
    attendee, and take one off who was only there because of a voice that is someone else now.
    Anyone else listed stays. Read again once when the note was edited in between."""
    schema = context.store.control.schema()
    for attempt in range(3):
        note = await context.store.get_note(note_id)
        listed = note.frontmatter.get(field)
        current = [] if blank(listed) else listed
        if not isinstance(current, list):
            return
        wanted = list(current)
        wanted = [v for v in wanted if not (isinstance(v, str) and v.strip().casefold() in drop)]
        if add is not None:
            link = filled_in._link(add)
            known = _known(add)
            at = next(
                (
                    i
                    for i, v in enumerate(wanted)
                    if isinstance(v, str) and v.strip().casefold() in known
                ),
                None,
            )
            if at is None:
                wanted.append(link)
            elif wanted[at].strip().casefold() == add.name.strip().casefold() and link != add.name:
                # The invitation wrote this person by a bare name; now there is a page to link.
                wanted[at] = link
        if wanted == current:
            return
        if schema.validate_frontmatter({**note.frontmatter, field: wanted}):
            return
        try:
            after = await context.store.patch_frontmatter(
                note_id, PatchFrontmatter(set={field: wanted}), if_match=note.content_hash
            )
        except VersionConflict:
            if attempt == 2:
                raise Refusal("The note was being changed. Try again.") from None
            continue
        await filled_in.keep_job_current(context, note, after)
        return


async def name_voice(
    context: Any, facts: Facts, payload: dict[str, Any], meeting_key_of: Any
) -> dict[str, Any]:
    """The owner chose who a voice is on a waiting note's card."""
    from coppermind.ids import is_valid_id
    from coppermind_store.context_service import now
    from coppermind_store.voice_store import VoiceStore

    note_id = str(payload.get("id", ""))
    if not is_valid_id(note_id):
        raise Refusal("That note is no longer listed.")
    try:
        note = await context.store.get_note(note_id)
    except (NotFound, StoreError) as exc:
        raise Refusal("That note is no longer listed.") from exc
    meeting_key = meeting_key_of(note.frontmatter)
    job, label = find(context, note_id, str(payload.get("voice", "")))
    choice = payload.get("choice")
    if not isinstance(choice, dict):
        raise Refusal("Choose who this voice is.")
    async with context.naming_lock:
        person = await person_for(context, note, meeting_key, choice)
    was = identify(
        context, VoiceStore(context).snapshot(), note_id, job, label, active_people(context)
    )
    sharing = await attach(context, note, job, label, person)
    async with context.lock:
        if choice.get("me") and not owner_person_id(context):
            context.write("owner", {"person_id": person.id, "at": now()})
    field = context.config().assignments["enrichment"].attendees_field or "attendees"
    if field in {key.name for key in context.store.control.schema().keys}:
        # Someone named for this voice before is taken off the attendees only when no other voice
        # on the note is them and the invitation does not list them.
        gone: frozenset[str] = frozenset()
        if was is not None and was.id != person.id:
            elsewhere = any(
                r["person"] and r["person"]["id"] == was.id for r in voices(context, facts, note_id)
            )
            if not elsewhere and not await _invited(context, note, meeting_key, was):
                gone = _known(was)
        await change_attendees(context, note_id, field, person, gone)
        # Another note still waiting for review that heard the same voice has the same person
        # there now; combining only reaches the attendees of notes already reviewed.
        reviewed = context.store.control.schema().role("reviewed_key")
        for other in sharing:
            try:
                waiting = await context.store.get_note(other)
            except (NotFound, StoreError):
                continue
            if waiting.frontmatter.get(reviewed) is not True:
                await change_attendees(context, other, field, person)
    return {"voices": voices(context, facts, note_id), "you": bool(owner_person_id(context))}


async def _invited(context: Any, note: Any, meeting_key: str, person: Any) -> bool:
    return any(i["person_id"] == person.id for i in await invitees(context, note, meeting_key))
