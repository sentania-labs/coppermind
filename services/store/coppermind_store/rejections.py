"""Durable ingest refusals under /data/state, mirrored for the problems page."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coppermind.atomicio import atomic_write_text, sync_directory
from coppermind.db.models import RecordedRejection
from coppermind.db.session import transaction
from coppermind.ids import new_id
from coppermind.store_protocol import MetadataUnavailable


class RejectionRecord(BaseModel):
    schema_version: Literal[1] = 1
    record_id: str
    kind: Literal["ingest"] = "ingest"
    reference: str
    reason: str
    created_at: AwareDatetime


def write_rejection(state_dir: Path, reference: str, reason: str) -> RejectionRecord:
    """Keep each attempt, including repeated refusals of the same claim."""
    record = RejectionRecord(
        record_id=new_id(),
        reference=reference,
        reason=reason,
        created_at=datetime.now(tz=UTC),
    )
    directory = state_dir / "rejections"
    directory.mkdir(parents=True, exist_ok=True)
    sync_directory(state_dir)
    atomic_write_text(
        directory / f"{record.record_id}.json", record.model_dump_json(indent=2) + "\n"
    )
    return record


async def mirror_rejection(session: AsyncSession, record: RejectionRecord) -> None:
    """An immutable record has the same identity on every reconciliation."""
    await session.execute(
        insert(RecordedRejection)
        .values(**record.model_dump(exclude={"schema_version"}))
        .on_conflict_do_nothing(index_elements=["record_id"])
    )


async def reconcile_rejections(
    state_dir: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Restore lost rows without duplicating refusals already in the mirror."""
    try:
        async with transaction(session_factory) as session:
            for path in sorted((state_dir / "rejections").glob("*.json")):
                record = RejectionRecord.model_validate_json(path.read_bytes())
                await mirror_rejection(session, record)
    except (SQLAlchemyError, OSError) as exc:
        raise MetadataUnavailable(str(exc)) from exc
