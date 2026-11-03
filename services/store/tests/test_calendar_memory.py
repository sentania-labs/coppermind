"""Calendar queries and recovery remain bounded, and budgets fail before promotion."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from coppermind_store.calendar_archive import CalendarArchive
from coppermind_store.calendar_import import CalendarImportError, check_import_budget
from coppermind_store.calendar_worker import parse_bounded

from coppermind.settings import LimitSettings
from services.store.tests.test_calendar_import import event, ics


def test_catalog_rebuild_and_queries_never_read_whole_catalog(tmp_path, monkeypatch):
    archive = CalendarArchive(tmp_path)
    receipt = archive.submit("work", "one.ics", ics(*(event(str(i)) for i in range(125))))
    assert archive.process(receipt.id).state == "succeeded"
    expected = archive.catalog().events.fingerprint()
    archive.engine.dispose()
    (archive.root / "mirror.sqlite3").unlink()
    read_bytes = Path.read_bytes

    def bounded_read(path):
        if path.name == "catalog.json":
            raise AssertionError("catalog-sized read")
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", bounded_read)
    restarted = CalendarArchive(tmp_path)
    catalog = restarted.catalog()
    assert len(catalog.events) == 125
    assert len(list(catalog.events.page(10, 120))) == 5
    assert catalog.events.fingerprint() == expected
    contact = catalog.contacts()[0]
    assert contact.evidence_count == 125
    assert len(contact.evidence) == 100
    # The preview cap does not remove durable evidence during a later import.
    added = restarted.submit("work", "next.ics", ics(event("next")))
    assert restarted.process(added.id).state == "succeeded"
    with (archive.root / "catalog.json").open() as handle:
        saved = json.load(handle)
    assert len(next(iter(saved["contact_records"].values()))["evidence"]) == 126


def test_cumulative_budget_rolls_back_every_event_and_preserves_original(tmp_path):
    archive = CalendarArchive(tmp_path, limits=lambda: LimitSettings(calendar_max_events=2))
    initial = archive.submit("work", "initial.ics", ics(event()))
    archive.process(initial.id)
    before = (archive.root / "catalog.json").read_bytes()
    incoming = archive.submit("work", "extra.ics", ics(event("second"), event("third")))
    failed = archive.process(incoming.id)
    assert failed.state == "failed"
    assert "limits.calendar_max_events=2" in failed.error
    assert len(archive.catalog().events) == 1
    assert (archive.root / "catalog.json").read_bytes() == before
    assert (archive.root / "imports" / incoming.id / "original").exists()
    assert (
        next(r for r in CalendarArchive(tmp_path).receipts() if r.id == incoming.id).state
        == "failed"
    )


def test_preflight_rejects_event_budget_without_starting_parser(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("parser must not run")

    monkeypatch.setattr("coppermind_store.calendar_worker.isolated_calendar", unexpected)
    with pytest.raises(CalendarImportError, match="calendar_max_events=1"):
        parse_bounded("work", "two.ics", ics(event(), event("second")), max_events=1)


def test_preflight_rejects_estimated_memory_budget():
    data = ics(event() + "\r\nDESCRIPTION:" + "x" * (5 * 1024**2))
    with pytest.raises(CalendarImportError, match="calendar_import_memory_mib=128") as raised:
        check_import_budget("large.ics", data, len(data), 50000, 128)
    assert "needs about 151 MiB" in str(raised.value)
    assert "Raise the setting to at least 151" in str(raised.value)


def test_preflight_and_hard_cap_agree_on_a_realistic_export():
    body = "\r\nDESCRIPTION:" + "meeting notes " * 150
    data = ics(*(event(str(i)) + body for i in range(2500)))
    script = (
        "import resource, sys\n"
        "from coppermind_store.calendar_import import parse_export\n"
        "data = sys.stdin.buffer.read()\n"
        "before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss\n"
        "events = parse_export('work', 'big.ics', data)\n"
        "print(len(events), resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], input=data, capture_output=True, check=True
    )
    parsed, grown_kib = map(int, result.stdout.split())
    assert parsed == 2500
    estimate_mib = -(-(len(data) * 30 + 2500 * 4096) // 1024**2)
    assert grown_kib * 1024 <= estimate_mib * 1024**2
    check_import_budget("big.ics", data, len(data), 50000, estimate_mib)
    with pytest.raises(CalendarImportError, match="calendar_import_memory_mib"):
        check_import_budget("big.ics", data, len(data), 50000, estimate_mib - 1)


def test_default_budget_admits_the_owner_scale_export():
    assert LimitSettings().calendar_import_memory_mib == 1024
    data = ics(*(event(str(i)) for i in range(4499))) + b"X" * (14 * 1024**2)
    check_import_budget("owner.ics", data, len(data), 50000, 1024)


def test_crash_between_catalog_promotion_and_mirror_commit_rebuilds(tmp_path, monkeypatch):
    from coppermind_store import calendar_mirror
    from sqlalchemy.exc import SQLAlchemyError

    archive = CalendarArchive(tmp_path)
    receipt = archive.submit("work", "one.ics", ics(event()))
    promote = calendar_mirror.write_catalog

    def crash(connection, path, imports):
        promote(connection, path, imports)
        raise SQLAlchemyError("simulated database commit failure")

    monkeypatch.setattr(calendar_mirror, "write_catalog", crash)
    with pytest.raises(SQLAlchemyError):
        archive.process(receipt.id)
    restarted = CalendarArchive(tmp_path)
    assert len(restarted.catalog().events) == 1
    assert restarted.process(receipt.id).state == "succeeded"
    assert restarted.catalog().contacts()[0].evidence_count == 1


def test_queries_serve_accepted_calendar_while_next_import_is_parsing(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from coppermind_store import calendar_archive

    archive = CalendarArchive(tmp_path)
    first = archive.submit("work", "first.ics", ics(event()))
    archive.process(first.id)
    second = archive.submit("work", "second.ics", ics(event("second")))
    parsing, release = Event(), Event()
    parse = calendar_archive.parse_bounded

    def wait_for_release(*args, **kwargs):
        parsing.set()
        assert release.wait(5)
        return parse(*args, **kwargs)

    monkeypatch.setattr(calendar_archive, "parse_bounded", wait_for_release)
    with ThreadPoolExecutor(2) as pool:
        importing = pool.submit(archive.process, second.id)
        try:
            assert parsing.wait(5)
            readable = pool.submit(lambda: len(archive.catalog().events))
            assert readable.result(timeout=2) == 1
        finally:
            release.set()
        assert importing.result(timeout=5).state == "succeeded"
    assert len(archive.catalog().events) == 2


def test_streamed_context_fingerprint_preserves_legacy_job_evidence(tmp_path):
    from coppermind_store.context_service import fingerprint

    archive = CalendarArchive(tmp_path)
    receipt = archive.submit("work", "one.ics", ics(event("b"), event("a")))
    archive.process(receipt.id)
    catalog = archive.catalog()
    corrections = {"contact": {"name": "A corrected name", "same_as": ""}}
    expected = fingerprint(
        {
            "calendar": sorted((key, value.semantic_hash) for key, value in catalog.events.items()),
            "contacts": corrections,
        }
    )
    assert catalog.events.context_fingerprint(corrections) == expected


def assert_batched_contact_pages(archive):
    import sqlalchemy as sa
    from coppermind_store.calendar_archive import ContactEvidence
    from coppermind_store.calendar_mirror import ContactRows

    from coppermind.db.calendar import calendar_evidence

    records = []
    for contact in range(105):
        for item in range(125 if contact == 0 else 3):
            records.append(
                {
                    "id": f"{contact:03}-{item:04}",
                    "contact_id": f"{contact:03}",
                    "record": ContactEvidence(
                        event_id=f"meeting-{item}",
                        address=f"mailto:p{contact}@example.test",
                        name="Person",
                        role="invitee",
                        participation="ACCEPTED",
                    ).model_dump_json(),
                }
            )
    with archive.engine.begin() as connection:
        connection.execute(calendar_evidence.delete())
        connection.execute(calendar_evidence.insert(), records)
    queries = []

    def count_query(*args):
        queries.append(1)

    sa.event.listen(archive.engine, "before_cursor_execute", count_query)
    try:
        contacts = ContactRows(archive.engine)
        page = list(contacts.page(100))
        assert len(page) == 100
        assert page[0].id == "000"
        assert page[0].evidence_count == 125
        assert len(page[0].evidence) == 100
        assert page[-1].evidence_count == len(page[-1].evidence) == 3
        assert len(queries) <= 2  # A bounded number of round trips, independent of contacts.
        queries.clear()
        assert [c.id for c in contacts.page(100, 100)] == [f"{n:03}" for n in range(100, 105)]
        assert len(queries) <= 2
    finally:
        sa.event.remove(archive.engine, "before_cursor_execute", count_query)


def test_contact_page_batches_counts_and_bounded_evidence(tmp_path):
    assert_batched_contact_pages(CalendarArchive(tmp_path))


async def test_snapshot_keeps_health_responsive_while_lazy_database_read_stalls(
    tmp_path, monkeypatch
):
    import asyncio
    from threading import Event, get_ident

    import httpx
    import sqlalchemy as sa
    from coppermind_store.auth import InternalAuth
    from coppermind_store.main import create_app

    from coppermind.settings import Wiring
    from services.store.tests.test_dropbox import local_store

    store = local_store(tmp_path)
    archive = store.context.archive
    receipt = archive.submit("work", "one.ics", ics(event()))
    archive.process(receipt.id)
    ready, stalled, release = Event(), Event(), Event()
    event_loop_thread = get_ident()
    catalog = archive.catalog

    def lazy_catalog():
        view = catalog()
        ready.set()  # Only stall a lazy page/count read, after catalog() has returned.
        return view

    def delayed_read(*args):
        if ready.is_set():
            assert get_ident() != event_loop_thread, "snapshot SQL ran on the event-loop thread"
        if ready.is_set() and not stalled.is_set():
            stalled.set()
            if not release.wait(10):
                raise AssertionError("snapshot database read blocked the event loop")

    monkeypatch.setattr(archive, "catalog", lazy_catalog)
    sa.event.listen(archive.engine, "before_cursor_execute", delayed_read)
    app = create_app(Wiring(data_dir=tmp_path / "data"))
    app.state.store = store
    app.state.auth = InternalAuth("test-internal-token")
    snapshot = None
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://store"
        ) as client:
            snapshot = asyncio.create_task(
                client.post(
                    "/internal/v1/context/snapshot",
                    json={},
                    headers={"Authorization": "Bearer test-internal-token"},
                )
            )
            assert await asyncio.to_thread(stalled.wait, 5)
            assert not snapshot.done()
            health = await client.get("/healthz")
            assert health.status_code == 200
            assert not snapshot.done()  # The database is still stalled while health serves.
            release.set()
            response = await snapshot
            assert response.status_code == 200
            assert response.json()["totals"]["events"] == 1
    finally:
        release.set()
        if snapshot is not None:
            await asyncio.gather(snapshot, return_exceptions=True)
        sa.event.remove(archive.engine, "before_cursor_execute", delayed_read)
