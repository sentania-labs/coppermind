"""The staged receipt lifecycle: reserve, stream, finalize, expire.

These run on a real `LocalStore` with a real `/data` tree, because what is under
test is where bytes land: the staging file and the receipt beside the other
control state. The artifact, its mirror row and its search entry are proved in
`tests/integration/test_staged_upload.py`, which runs against PostgreSQL.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest
from coppermind_store import staged
from coppermind_store.control import ControlState
from coppermind_store.notes import LocalStore

from coppermind.settings import Wiring
from coppermind.store_protocol import (
    ReceiptMissing,
    StagedFileUnavailable,
    StagedReservation,
    ValidationFailed,
)

DIGEST = hashlib.sha256(b"quokka").hexdigest()


class _FakeResult:
    def __init__(self, row: Any) -> None:
        self._row = row

    def first(self) -> Any:
        return self._row


class _FakeSession:
    """Minimal fake session that satisfies the transaction contract."""

    def __init__(self) -> None:
        self._rows: list[tuple[Any, ...]] = []

    async def execute(self, *a: Any) -> _FakeResult:  # noqa: ARG002
        return _FakeResult(self._rows[0] if self._rows else None)

    async def scalar(self, *a: Any) -> Any:  # noqa: ARG002
        return None

    def add(self, *a: Any) -> None:  # noqa: ARG002
        pass

    async def flush(self) -> None:  # noqa: ARG002
        pass

    async def get(self, *a: Any) -> Any:  # noqa: ARG002
        return None


class _FakeTransaction:
    def __init__(self, *a: Any) -> None:  # noqa: ARG002
        self._session = _FakeSession()

    async def __aenter__(self) -> _FakeSession:
        return self._session

    async def __aexit__(self, *a: Any) -> None:  # noqa: ARG002
        pass


def _fake_transaction(*a: Any) -> _FakeTransaction:
    return _FakeTransaction()


def local_store(tmp_path: Path) -> LocalStore:
    wiring = Wiring(data_dir=tmp_path / "data")
    control = ControlState(wiring.state_dir)
    control.ensure_defaults()
    wiring.notes_dir.mkdir(parents=True, exist_ok=True)
    return LocalStore(wiring.notes_dir, control, cast(Any, object()), wiring.sources_dir)


def declaration(**overrides: Any) -> StagedReservation:
    payload: dict[str, Any] = {
        "filename": "deck.pptx",
        "mime_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "size_bytes": 6,
        "sha256": DIGEST,
    }
    payload.update(overrides)
    return StagedReservation.model_validate(payload)


@pytest.fixture
def store(tmp_path: Path):
    from coppermind_store import artifacts as artifacts_module
    from coppermind_store import notes as notes_module

    import coppermind.db.session as session_module

    with (
        patch.object(session_module, "transaction", _fake_transaction),
        patch.object(notes_module, "transaction", _fake_transaction),
        patch.object(artifacts_module, "transaction", _fake_transaction),
    ):
        yield local_store(tmp_path)


async def test_a_reservation_is_durable_and_names_the_accepted_limits(store):
    receipt = await staged.reserve(store, declaration())
    assert receipt.state == "uploading"
    assert receipt.upload_bytes == 0
    assert receipt.limits.staged_max_bytes == 50 * 1024 * 1024
    path = store.control.store.state_dir / "staged" / f"{receipt.id}.json"
    assert path.is_file()
    assert json.loads(path.read_text())["sha256"] == DIGEST


async def test_the_same_declaration_replays_the_same_receipt(store):
    first = await staged.reserve(store, declaration())
    again = await staged.reserve(store, declaration())
    assert again.id == first.id
    changed = await staged.reserve(store, declaration(fields={"title": "Other"}))
    assert changed.id != first.id


async def test_a_declaration_over_the_staged_maximum_is_refused_at_once(store):
    with pytest.raises(ValidationFailed) as info:
        await staged.reserve(store, declaration(size_bytes=50 * 1024 * 1024 + 1))
    assert "staged maximum" in str(info.value)
    assert list((store.control.store.state_dir / "staged").glob("*.json")) == []


async def test_an_identical_put_reports_the_verified_count_and_replays(store):
    receipt = await staged.reserve(store, declaration())
    first = await staged.put_bytes(store, receipt.id, b"quokka", declared_bytes=6)
    assert first.state == "uploading"
    assert first.upload_bytes == 6
    again = await staged.put_bytes(store, receipt.id, b"quokka", declared_bytes=6)
    assert again.upload_bytes == 6
    assert again.failure is None
    staged_path = store.control.store.state_dir / "staged" / f"{receipt.id}.bin"
    assert staged_path.read_bytes() == b"quokka"


async def test_bytes_that_disagree_with_the_declared_hash_fail_the_receipt(store):
    receipt = await staged.reserve(store, declaration())
    failed = await staged.put_bytes(store, receipt.id, b"QUOKKA", declared_bytes=6)
    assert failed.state == "failed"
    assert "hashed" in str(failed.failure)
    assert failed.artifact is None
    # The rejected bytes are not kept, and a later good upload can still land.
    staged_path = store.control.store.state_dir / "staged" / f"{receipt.id}.bin"
    assert not staged_path.exists()
    recovered = await staged.put_bytes(store, receipt.id, b"quokka", declared_bytes=6)
    assert recovered.state == "uploading" and recovered.upload_bytes == 6


async def test_a_stream_longer_or_shorter_than_declared_fails_the_receipt(store):
    receipt = await staged.reserve(store, declaration())
    longer = await staged.put_bytes(store, receipt.id, b"quokkaX", declared_bytes=7)
    assert longer.state == "failed"
    assert "7 bytes" in str(longer.failure)
    shorter = await staged.put_bytes(store, receipt.id, b"quok", declared_bytes=4)
    assert shorter.state == "failed"
    assert "4 bytes" in str(shorter.failure)


async def test_a_content_length_that_disagrees_with_the_declaration_fails_first(store):
    receipt = await staged.reserve(store, declaration())
    result = await staged.put_bytes(store, receipt.id, b"quokka", declared_bytes=9)
    assert result.state == "failed"
    assert "Content-Length" in str(result.failure)


async def test_finalize_writes_the_receipt_and_drops_the_staged_copy(store):
    receipt = await staged.reserve(store, declaration())
    await staged.put_bytes(store, receipt.id, b"quokka", declared_bytes=6)
    done = await staged.finalize(store, receipt.id)
    assert done.state == "stored"
    assert done.artifact is not None
    assert done.artifact.path and done.artifact.path.endswith("deck.pptx")
    # The staged copy is gone; the receipt is the durable answer.
    assert not (store.control.store.state_dir / "staged" / f"{receipt.id}.bin").exists()
    assert await staged.get_receipt(store, receipt.id) == done


async def test_finalizing_twice_answers_the_same_stored_artifact(store):
    receipt = await staged.reserve(store, declaration())
    await staged.put_bytes(store, receipt.id, b"quokka", declared_bytes=6)
    first = await staged.finalize(store, receipt.id)
    second = await staged.finalize(store, receipt.id)
    assert second.state == "stored"
    assert second.artifact == first.artifact
    assert second.artifact is not None and first.artifact is not None
    assert second.artifact.revision == first.artifact.revision == 1


async def test_finalize_without_staged_bytes_or_a_failed_upload_is_refused(store):
    bare = await staged.reserve(store, declaration())
    with pytest.raises(ValidationFailed) as info:
        await staged.finalize(store, bare.id)
    assert "no bytes are staged" in str(info.value)
    failed = await staged.put_bytes(store, bare.id, b"nope!", declared_bytes=5)
    assert failed.state == "failed"
    with pytest.raises(ValidationFailed):
        await staged.finalize(store, bare.id)


async def test_a_second_put_of_conflicting_bytes_fails_the_receipt(store):
    receipt = await staged.reserve(store, declaration())
    await staged.put_bytes(store, receipt.id, b"quokka", declared_bytes=6)
    await staged.finalize(store, receipt.id)
    conflict = await staged.put_bytes(store, receipt.id, b"other!", declared_bytes=6)
    assert conflict.state == "failed"
    assert "hashed" in str(conflict.failure)
    assert (await staged.get_receipt(store, receipt.id)).state == "failed"


async def test_an_unknown_or_unreadable_receipt_is_reported(store):
    with pytest.raises(ReceiptMissing):
        await staged.get_receipt(store, "nope")
    receipt = await staged.reserve(store, declaration())
    path = store.control.store.state_dir / "staged" / f"{receipt.id}.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(StagedFileUnavailable):
        await staged.get_receipt(store, receipt.id)


async def test_an_abandoned_reservation_is_swept_once_the_window_passes(store):
    receipt = await staged.reserve(store, declaration())
    await staged.put_bytes(store, receipt.id, b"quokka", declared_bytes=6)
    folder = store.control.store.state_dir / "staged"
    path = folder / f"{receipt.id}.json"
    document = json.loads(path.read_text())
    document["reserved_at"] = "2020-01-01T00:00:00+00:00"
    path.write_text(json.dumps(document), encoding="utf-8")

    await staged.reserve(store, declaration(filename="second.pptx"))
    assert not path.exists()
    assert not (folder / f"{receipt.id}.bin").exists()
    # A receipt written moments ago survives the same sweep.
    survivors = [json.loads(p.read_text())["filename"] for p in folder.glob("*.json")]
    assert "second.pptx" in survivors
