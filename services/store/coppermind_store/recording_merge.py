"""Safe construction of additions to an owner's meeting note."""

from __future__ import annotations

from coppermind.meeting_summary import MeetingSummary


def append_summary(body: str, summary: MeetingSummary, projection_path: str) -> str:
    """Append a separate section, preserving the existing body byte for byte."""
    if not projection_path or any(
        part in projection_path for part in ("\n", "\r", "\0", "]]", "|", "#")
    ):
        raise ValueError("the transcript link is unavailable")
    sections = []
    for heading, text in (
        ("Summary", summary.summary),
        ("Decisions", summary.decisions),
        ("Action items", summary.action_items),
    ):
        if text.strip():
            sections.append(f"### {heading}\n\n{text.strip()}")
    if not sections:
        raise ValueError("the recording summary is empty")
    path = projection_path.removesuffix(".md")
    addition = "## Recording summary\n\n" + "\n\n".join(sections) + f"\n\n[[{path}|Transcript]]\n"
    return body + ("\n\n" if body else "") + addition


async def apply(context, receipt):
    """Replay a confirmed merge, keeping a durable checkpoint between each write."""
    import json
    from datetime import UTC, datetime

    import sqlalchemy as sa

    from coppermind import frontmatter as fm
    from coppermind.atomicio import atomic_write_text, sync_directory
    from coppermind.db.models import Note, NoteSource
    from coppermind.db.session import transaction
    from coppermind.ids import is_valid_id
    from coppermind.store_protocol import VersionConflict
    from coppermind_store import indexer
    from coppermind_store.fs import content_hash, resolve
    from coppermind_store.sources import _external_id_claim_path
    from coppermind_store.voice_store import VoiceStore

    source_id, generated_id, target_id = (
        receipt["source_id"],
        receipt["generated_id"],
        receipt["target_id"],
    )
    if not all(is_valid_id(value) for value in (source_id, generated_id, target_id)):
        raise ValueError("invalid merge identity")
    if generated_id == target_id:
        raise ValueError("The recording and destination must be different notes.")
    store = context.store
    name = "recording-merges/" + source_id
    manifest_path = store.sources_root / source_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    claim = _external_id_claim_path(
        store.sources_root, manifest["provider"], manifest["external_source_id"]
    )
    async with store.maintenance.shared(), store._source_lock_for(claim.name):
        if receipt.get("state") == "complete":
            return receipt
        manifest = json.loads(manifest_path.read_text())
        if (
            manifest.get("source_id") != source_id
            or manifest.get("current_revision") != receipt["source_revision"]
            or manifest.get("note_id") not in {generated_id, target_id}
        ):
            raise ValueError("The recording has changed. Refresh the review before merging.")
        target = await store.get_note(target_id)
        source_key = store.control.schema().role("sources_key")
        citations = target.frontmatter.get(source_key, [])
        if not isinstance(citations, list):
            raise ValueError("The destination note has an invalid source list.")
        changes = {}
        if source_id not in citations:
            changes[source_key] = [*citations, source_id]
        artifacts_key = store.control.schema().optional_role("artifacts_key")
        if artifacts_key:
            cited = target.frontmatter.get(artifacts_key, [])
            if not isinstance(cited, list):
                raise ValueError("The destination note has an invalid recording list.")
            if receipt["artifact_id"] not in cited:
                changes[artifacts_key] = [*cited, receipt["artifact_id"]]
        if changes:
            target = await store.merge_note_update(target_id, changes, "", target.content_hash)
        if manifest.get("note_id") != target_id:
            manifest["note_id"] = target_id
            atomic_write_text(manifest_path, json.dumps(manifest, ensure_ascii=False), mode=0o600)
        async with transaction(store.session_factory) as session:
            old = await session.get(NoteSource, (generated_id, source_id))
            existing = await session.get(NoteSource, (target_id, source_id))
            if existing is None:
                session.add(
                    NoteSource(
                        note_id=target_id,
                        source_id=source_id,
                        approved_revision=old.approved_revision if old else None,
                        created_at=old.created_at if old else datetime.now(UTC),
                    )
                )
            if old is not None:
                await session.delete(old)
        if receipt.get("step") not in {"appending", "appended"}:
            receipt["step"] = "attached"
            context.write(name, receipt)

        marker = f"<!-- coppermind-recording:{source_id} -->"
        for _ in range(3):
            target = await store.get_note(target_id)
            if marker in target.body:
                break
            if receipt.get("step") == "appended":
                raise ValueError("The added summary was changed. Refresh before continuing.")
            receipt["step"] = "appending"
            receipt["append_base_hash"] = target.content_hash
            context.write(name, receipt)
            body = (
                append_summary(
                    target.body,
                    MeetingSummary.model_validate(receipt["summary"]),
                    receipt["projection_path"],
                )
                + marker
                + "\n"
            )
            try:
                target = await store.merge_note_update(
                    target_id, {}, body[len(target.body) :], target.content_hash
                )
                break
            except VersionConflict:
                receipt["step"] = "attached"
                context.write(name, receipt)
                continue
        else:
            raise ValueError("Your note keeps changing. Refresh and try the merge again.")
        receipt["step"] = "appended"
        context.write(name, receipt)

        # Do not remove a recording note with human edits, even after confirmation.
        target_path = (await store._locate(target_id))[1]
        generated_path = resolve(store.notes_root, receipt["generated_path"])
        async with context.lock, store._lock_for(target_id), store._lock_for(generated_id):
            target_bytes = target_path.read_bytes()
            target_frontmatter, target_body = fm.parse(target_bytes.decode())
            if (
                target_frontmatter.get(store.control.schema().role("id_key")) != target_id
                or source_id not in target_frontmatter.get(source_key, [])
                or marker not in target_body
            ):
                raise ValueError("The destination note changed. Refresh and try again.")
            if generated_path.exists():
                data = generated_path.read_bytes()
                if content_hash(data) != receipt["generated_hash"]:
                    raise ValueError(
                        "The recording note changed. It was kept; refresh before merging."
                    )
                context.write(
                    "retired-recordings/" + generated_id,
                    {
                        "target_id": target_id,
                        "source_id": source_id,
                        "snapshot": receipt["generated_snapshot"],
                    },
                )
                if (
                    content_hash(target_path.read_bytes()) != content_hash(target_bytes)
                    or content_hash(generated_path.read_bytes()) != receipt["generated_hash"]
                ):
                    raise ValueError(
                        "A note changed. The recording note was kept; refresh and retry."
                    )
                VoiceStore(context).reparent_recording(generated_id, target_id, source_id)
                generated_path.unlink()
                sync_directory(generated_path.parent)
            elif not context.read("retired-recordings/" + generated_id, {}):
                raise ValueError("The recording note is unavailable. It was not retired.")
            else:
                VoiceStore(context).reparent_recording(generated_id, target_id, source_id)
            async with transaction(store.session_factory) as session:
                await session.execute(
                    sa.update(Note)
                    .where(Note.id == generated_id)
                    .values(
                        state="deleted",
                        state_reason="Merged into another note",
                        updated_at=datetime.now(UTC),
                    )
                )
                await indexer.drop_note(session, generated_id, datetime.now(UTC))
        receipt["state"], receipt["step"], receipt["error"] = "complete", "complete", ""
        context.write(name, receipt)
        return receipt
