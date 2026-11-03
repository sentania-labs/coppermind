"""PostgreSQL calendar rebuilds and refused imports are visible through Problems."""

from datetime import UTC, datetime
from pathlib import Path

from coppermind_store.calendar_archive import CalendarArchive

from coppermind.db.calendar import calendar_events, calendar_evidence, calendar_state
from coppermind.db.models import SearchUnreadable
from coppermind.settings import LimitSettings
from services.store.tests.test_calendar_import import event, ics


async def test_postgres_stream_rebuild_and_budget_problem(store, wiring, monkeypatch):
    archive = CalendarArchive(
        wiring.state_dir,
        wiring.database_url_for("psycopg"),
        limits=lambda: LimitSettings(calendar_max_events=2),
    )
    store.context.archive = archive
    try:
        first = archive.submit("work", "initial.ics", ics(event()))
        assert archive.process(first.id).state == "succeeded"
        before = archive.catalog().events.fingerprint()
        with archive.engine.begin() as connection:
            for table in (calendar_events, calendar_evidence, calendar_state):
                connection.execute(table.delete())
        read_bytes = Path.read_bytes

        def bounded_read(path):
            if path.name == "catalog.json":
                raise AssertionError("whole-catalog read")
            return read_bytes(path)

        monkeypatch.setattr(Path, "read_bytes", bounded_read)
        assert archive.catalog().events.fingerprint() == before
        assert len(list(archive.catalog().events.page(100))) == 1
        second = archive.submit("work", "extra.ics", ics(event("two"), event("three")))
        refused = archive.process(second.id)
        assert refused.state == "failed"
        problems = await store.get_problems()
        assert any(
            p.kind == "calendar_import"
            and p.reference == "extra.ics"
            and "calendar_max_events=2" in p.reason
            for p in problems
        )
        assert len(archive.catalog().events) == 1
        dropbox = store.context.dropbox
        records = dropbox.records()
        records["items"]["mirror"] = {
            "id": "mirror",
            "state": "failed",
            "error": "Calendar import failed; inspect the calendar import receipt",
            "parts": [{"import_id": second.id}],
        }
        dropbox.save(records)
        async with store.session_factory() as session, session.begin():
            session.add(
                SearchUnreadable(
                    kind="note",
                    ref_id="unreadable-note",
                    version="1",
                    path="unreadable.md",
                    observation="permission-denied",
                    reason="Permission denied",
                    observed_at=datetime.now(UTC),
                )
            )
        counters = (await store.get_status()).counters
        assert counters.calendar_import_failures == 1
        assert counters.known_unreadable == 1
        problems = await store.get_problems()
        assert any(p.kind == "calendar_import" and p.reference == "extra.ics" for p in problems)
        assert any(
            p.kind == "search_unreadable"
            and p.reference == "unreadable.md"
            and p.reason == "Permission denied"
            for p in problems
        )
    finally:
        archive.engine.dispose()


async def test_postgres_contact_pages_batch_full_counts_and_bounded_previews(wiring):
    from services.store.tests.test_calendar_memory import assert_batched_contact_pages

    archive = CalendarArchive(wiring.state_dir, wiring.database_url_for("psycopg"))
    try:
        assert_batched_contact_pages(archive)
    finally:
        archive.engine.dispose()
