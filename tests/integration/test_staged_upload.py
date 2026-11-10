"""The staged upload path over the whole stack, against PostgreSQL and real files.

These are the acceptance checks for a file over the inline limit: one end to end,
plus the mismatches, the retry, the abandoned reservation and the oversize staged
file. Every one reads the notes filesystem, the archive, the state record and the
mirror back, because the claim is that the staged path and the inline path leave
exactly the same thing behind.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
import sqlalchemy as sa
from coppermind_store import staged
from coppermind_store.artifacts import record_path

from coppermind.db.models import Artifact, SearchDocument
from coppermind.store_protocol import (
    CreateArtifact,
    ReceiptMissing,
    StagedFileUnavailable,
    StagedReservation,
    ValidationFailed,
)

DIGEST = hashlib.sha256(b"quokka").hexdigest()
STAGED_MAX = 50 * 1024 * 1024


def declaration(**overrides) -> StagedReservation:
    payload = {
        "filename": "deck.pptx",
        "mime_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "size_bytes": 6,
        "sha256": DIGEST,
    }
    payload.update(overrides)
    return StagedReservation.model_validate(payload)


def _staged(store, receipt_id: str) -> Path:
    return store.control.store.state_dir / "staged" / f"{receipt_id}.bin"


async def test_a_file_over_the_inline_limit_ends_up_as_an_ordinary_artifact(store):
    receipt = await staged.reserve(store, declaration(fields={"title": "Deck", "tags": ["ref"]}))
    uploaded = await staged.put_bytes(store, receipt.id, b"quokka", declared_bytes=6)
    assert uploaded.upload_bytes == 6
    done = await staged.finalize(store, receipt.id)
    assert done.state == "stored"
    artifact = done.artifact
    assert artifact is not None and artifact.created and artifact.revision == 1

    # The browsable copy, the archive and the record are what an inline upload
    # would have made: same root, same folder keyed on the received month.
    assert artifact.path and artifact.path.startswith("Artifacts/")
    browsable = store.notes_root / artifact.path
    assert browsable.read_bytes() == b"quokka"
    archived = store.artifacts_root / artifact.id / "r0001" / "deck.pptx"
    assert archived.read_bytes() == b"quokka"
    record = json.loads(record_path(store, artifact.id).read_text(encoding="utf-8"))
    assert record["sha256"] == DIGEST and record["size_bytes"] == 6

    # The mirror row and the search entry, too.
    async with store.session_factory() as session:
        row = await session.get(Artifact, artifact.id)
        assert row is not None
        assert (row.sha256, row.size_bytes, row.filename) == (DIGEST, 6, "deck.pptx")
        hit = await session.scalar(
            sa.select(SearchDocument).where(
                SearchDocument.kind == "artifact", SearchDocument.ref_id == artifact.id
            )
        )
        assert hit is not None
        assert hit.path == artifact.path

    # Staging is left empty; the receipt is the durable answer.
    assert not _staged(store, receipt.id).exists()
    assert await staged.get_receipt(store, receipt.id) == done


async def test_a_staged_upload_replays_the_same_artifact_as_the_inline_one(store):
    receipt = await staged.reserve(store, declaration())
    await staged.put_bytes(store, receipt.id, b"quokka", declared_bytes=6)
    staged_result = await staged.finalize(store, receipt.id)
    inline = await store.create_artifact(
        CreateArtifact(
            kind="artifact",
            filename="deck.pptx",
            mime_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            content_base64=base64.b64encode(b"quokka").decode(),
        )
    )
    assert staged_result.artifact is not None
    assert staged_result.artifact.id == inline.id
    assert inline.created is False


async def test_a_hash_mismatch_fails_the_receipt_and_a_retry_of_it_lands(store):
    receipt = await staged.reserve(store, declaration())
    failed = await staged.put_bytes(store, receipt.id, b"QUOKKA", declared_bytes=6)
    assert failed.state == "failed"
    assert "hashed" in str(failed.failure)
    assert failed.artifact is None
    assert not _staged(store, receipt.id).exists()
    recovered = await staged.put_bytes(store, receipt.id, b"quokka", declared_bytes=6)
    assert recovered.state == "uploading" and recovered.upload_bytes == 6
    done = await staged.finalize(store, receipt.id)
    assert done.state == "stored" and done.artifact is not None


async def test_a_size_mismatch_fails_the_receipt_with_the_two_counts(store):
    receipt = await staged.reserve(store, declaration())
    longer = await staged.put_bytes(store, receipt.id, b"quokkaX", declared_bytes=7)
    assert longer.state == "failed" and "7 bytes" in str(longer.failure)
    shorter = await staged.put_bytes(store, receipt.id, b"quok", declared_bytes=4)
    assert shorter.state == "failed" and "4 bytes" in str(shorter.failure)
    with pytest.raises(ValidationFailed):
        await staged.finalize(store, receipt.id)


async def test_an_identical_retry_succeeds_and_conflicting_bytes_do_not(store):
    receipt = await staged.reserve(store, declaration())
    await staged.put_bytes(store, receipt.id, b"quokka", declared_bytes=6)
    first = await staged.finalize(store, receipt.id)
    replay = await staged.finalize(store, receipt.id)
    assert replay.state == "stored" and replay.artifact == first.artifact
    # Same declaration, same receipt identifier: an identical retry, not a new one.
    assert (await staged.reserve(store, declaration())).id == receipt.id
    # Different bytes under that receipt are a conflict on the receipt.
    conflict = await staged.put_bytes(store, receipt.id, b"other!", declared_bytes=6)
    assert conflict.state == "failed" and "hashed" in str(conflict.failure)
    async with store.session_factory() as session:
        rows = (
            await session.execute(sa.select(Artifact).where(Artifact.filename == "deck.pptx"))
        ).all()
    assert len(rows) == 1
    assert rows[0][0].current_revision == 1


async def test_an_abandoned_reservation_is_swept_once_the_window_passes(store):
    receipt = await staged.reserve(store, declaration())
    await staged.put_bytes(store, receipt.id, b"quokka", declared_bytes=6)
    path = store.control.store.state_dir / "staged" / f"{receipt.id}.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["reserved_at"] = "2020-01-01T00:00:00+00:00"
    path.write_text(json.dumps(document), encoding="utf-8")

    await staged.reserve(store, declaration(filename="second.pptx"))
    assert not path.exists()
    assert not _staged(store, receipt.id).exists()
    survivors = [
        json.loads(p.read_text(encoding="utf-8"))["filename"]
        for p in (store.control.store.state_dir / "staged").glob("*.json")
    ]
    assert "second.pptx" in survivors


async def test_a_staged_file_over_the_staged_maximum_is_refused_at_once(store):
    with pytest.raises(ValidationFailed) as info:
        await staged.reserve(store, declaration(size_bytes=STAGED_MAX + 1))
    assert "staged maximum" in str(info.value)
    assert list((store.control.store.state_dir / "staged").glob("*.json")) == []


async def test_an_unknown_or_unreadable_receipt_is_reported(store):
    with pytest.raises(ReceiptMissing):
        await staged.get_receipt(store, "nope")
    receipt = await staged.reserve(store, declaration())
    path = store.control.store.state_dir / "staged" / f"{receipt.id}.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(StagedFileUnavailable):
        await staged.get_receipt(store, receipt.id)


@pytest.mark.parametrize(
    ("size", "said"),
    [(6, 6), (10 * 1024 * 1024, 10 * 1024 * 1024)],
)
async def test_the_staged_maximum_is_configurable_and_stops_a_long_stream(store, size, said):
    """A stream longer than the declaration stops being hashed, and the receipt says so."""
    assert said == size
    receipt = await staged.reserve(store, declaration())
    result = await staged.put_bytes(store, receipt.id, b"x" * (6 + 1), declared_bytes=6)
    assert result.state == "failed"
    assert "7 bytes" in str(result.failure)
