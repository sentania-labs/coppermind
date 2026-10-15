"""Explicit, version-checked removal of mistaken approved note associations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from coppermind import frontmatter as fm
from coppermind.ids import new_id
from coppermind.store_protocol import PatchFrontmatter
from coppermind_store.context_service import fingerprint, now
from coppermind_store.notes import _with_kinds, content_hash

if TYPE_CHECKING:
    from coppermind_store.context_service import ContextService


async def correct(context: ContextService, payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("confirmation") != "Remove mistaken association":
        raise ValueError("explicit correction approval is required")
    note = await context.store.get_note(payload["note_id"])
    if note.content_hash != payload.get("note_hash"):
        raise ValueError("note changed; reload the correction preview")
    records = context.read("associations", {})
    current = records.get(note.id, {})
    if fingerprint(current) != payload.get("associations_hash"):
        raise ValueError("associations changed; reload the correction preview")
    kind, target = payload["kind"], payload["target"]
    if kind not in {"events", "contacts", "attendees", "invitees"} or target not in current.get(
        kind, []
    ):
        raise ValueError("association does not exist")
    updated = {key: list(values) for key, values in current.items()}
    remove_from = {kind}
    if kind == "contacts":
        remove_from.update({"attendees", "invitees"})
    for key in remove_from:
        updated[key] = [item for item in updated.get(key, []) if item != target]
    assignment = context.config().assignments["enrichment"]
    mapping = {
        "events": assignment.event_field,
        "contacts": assignment.contacts_field,
        "attendees": assignment.attendees_field,
        "invitees": assignment.invitees_field,
    }
    contacts = {contact.id: contact for contact in context.archive.catalog().contacts()}
    addresses = (
        {evidence.address.partition(":")[2] for evidence in contacts[target].evidence}
        if target in contacts
        else set()
    )
    changes: dict[str, Any] = {}
    unset: list[str] = []
    for key in remove_from:
        field = mapping[key]
        if not field or field not in note.frontmatter:
            continue
        value = note.frontmatter[field]
        if key == "events":
            if value == target:
                unset.append(field)
        elif isinstance(value, list):
            removed = addresses if key in {"attendees", "invitees"} else {target}
            changes[field] = [item for item in value if item not in removed]
    _, path = await context.store._locate(note.id)
    data = path.read_bytes()
    if content_hash(data) != note.content_hash:
        raise ValueError("note changed before correction")
    schema = context.store.control.schema()
    predicted = (
        fm.patch(data.decode(), _with_kinds(changes, schema), unset=unset).encode()
        if changes or unset
        else data
    )
    frontmatter, body = fm.parse(predicted.decode())
    if body != note.body or schema.validate_frontmatter(frontmatter):
        raise ValueError("correction would violate the note schema or body preservation")
    record = {
        "id": new_id(),
        "note_id": note.id,
        "path": note.path,
        "state": "pending",
        "at": now(),
        "before_hash": note.content_hash,
        "after_hash": content_hash(predicted),
        "body_hash": fingerprint(note.body),
        "before_associations": current,
        "after_associations": updated,
        "changes": changes,
        "unset": unset,
        "confirmation": payload["confirmation"],
    }
    context.write("association-corrections/" + record["id"], record)
    return await resume(context, record)


async def resume(context: ContextService, record: dict[str, Any]) -> dict[str, Any]:
    if not record:
        raise ValueError("correction record missing")
    if record["state"] == "succeeded":
        return {"corrected": record["id"]}
    note = await context.store.get_note(record["note_id"])
    if note.path != record["path"] or fingerprint(note.body) != record["body_hash"]:
        raise ValueError("note changed; inspect the pending correction")
    associations = context.read("associations", {})
    current = associations.get(note.id, {})
    if current not in (record["before_associations"], record["after_associations"]):
        raise ValueError("associations changed; inspect the pending correction")
    if note.content_hash != record["after_hash"]:
        if note.content_hash != record["before_hash"]:
            raise ValueError("note changed; inspect the pending correction")
        note = await context.store.patch_frontmatter(
            note.id,
            PatchFrontmatter(set=record["changes"], unset=record["unset"]),
            note.content_hash,
        )
    if note.content_hash != record["after_hash"]:
        raise ValueError("correction did not match its recorded result")
    associations[note.id] = record["after_associations"]
    context.write("associations", associations)
    record["state"] = "succeeded"
    context.write("association-corrections/" + record["id"], record)
    return {"corrected": record["id"]}
