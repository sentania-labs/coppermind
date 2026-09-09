from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy.exc import SQLAlchemyError

import coppermind_store.notes as notes_module
from coppermind.settings import Wiring
from coppermind.store_protocol import CreateNote, MetadataUnavailable
from coppermind_store.control import ControlState
from coppermind_store.notes import LocalStore


class SessionWithLostCommitResult:
    async def execute(self, statement: Any) -> None:
        return None

    def add(self, row: Any) -> None:
        return None


@asynccontextmanager
async def transaction_with_lost_commit_result(session_factory: Any):
    yield SessionWithLostCommitResult()
    raise SQLAlchemyError("commit result is unknown")


async def test_an_ambiguous_commit_failure_keeps_the_authoritative_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    wiring = Wiring(data_dir=tmp_path / "data")
    control = ControlState(wiring.state_dir)
    control.ensure_defaults()
    wiring.notes_dir.mkdir(parents=True)
    store = LocalStore(wiring.notes_dir, control, cast(Any, object()))
    monkeypatch.setattr(notes_module, "transaction", transaction_with_lost_commit_result)

    with pytest.raises(MetadataUnavailable):
        await store.create_note(CreateNote(title="Commit result unknown"))

    files = list(wiring.notes_dir.rglob("*.md"))
    assert len(files) == 1
    assert "# Commit result unknown" in files[0].read_text(encoding="utf-8")
