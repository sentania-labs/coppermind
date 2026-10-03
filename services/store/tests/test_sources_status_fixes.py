"""Regression checks for durable refusals and timezone-aware source cursors."""

from __future__ import annotations

import base64
import json
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from coppermind_store.control import ControlState
from coppermind_store.notes import LocalStore
from coppermind_store.rejections import RejectionRecord
from coppermind_store.sources import _decode_source_cursor
from sqlalchemy.exc import SQLAlchemyError

from coppermind.settings import Wiring
from coppermind.store_protocol import IngestRequest, PayloadTooLarge, SourceQuery, ValidationFailed


def _cursor(at: str) -> str:
    return base64.urlsafe_b64encode(
        json.dumps({"v": 1, "id": "01K4Q8Z2A0P1Q2R3S4T5U6V7W8", "at": at}).encode()
    ).decode()


@pytest.mark.parametrize("at", ["2026-09-08", "2026-09-08T12:00:00"])
async def test_naive_cursor_is_rejected_before_database_access(at: str):
    store = cast(Any, object())
    from coppermind_store.sources import list_sources

    with pytest.raises(ValidationFailed):
        await list_sources(store, SourceQuery(cursor=_cursor(at)))


@pytest.mark.parametrize("offset", ["+00:00", "+05:30", "-04:00"])
def test_cursor_accepts_explicit_utc_offsets(offset: str):
    at, _ = _decode_source_cursor(_cursor(f"2026-09-08T12:00:00{offset}"))
    assert at.utcoffset() is not None
    if offset == "+05:30":
        assert at.utcoffset() == timedelta(hours=5, minutes=30)


async def test_database_outage_keeps_each_refusal_on_disk(tmp_path: Path):
    wiring = Wiring(data_dir=tmp_path)
    control = ControlState(wiring.state_dir)
    control.ensure_defaults()
    current = control.store.read("settings")
    body = dict(current.body)
    body["limits"]["ingest_max_bytes"] = 512
    control.store.write("settings", body, if_revision=current.revision)

    def unavailable() -> Any:
        raise SQLAlchemyError("database unavailable")

    store = LocalStore(wiring.notes_dir, control, cast(Any, unavailable), wiring.sources_dir)
    request = IngestRequest.model_validate(
        {
            "source": {
                "provider": "plaud",
                "external_source_id": "refused",
                "source_type": "transcript",
                "artifacts": [
                    {"name": "text.txt", "mime_type": "text/plain", "content": "x" * 1024}
                ],
            },
            "note": {"title": "Refused"},
        }
    )
    for _ in range(2):
        with pytest.raises(PayloadTooLarge):
            await store.ingest(request)

    records = [
        RejectionRecord.model_validate_json(path.read_bytes())
        for path in (wiring.state_dir / "rejections").glob("*.json")
    ]
    assert len({record.record_id for record in records}) == 2
    assert all(record.reference == "plaud:refused" for record in records)
    assert all(record.reason == "payload_too_large" for record in records)
    assert not list(wiring.notes_dir.rglob("*.md"))
