"""Calendar queries and recovery remain bounded, and budgets fail before promotion."""

import json
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
    data = ics(event() + "\r\nDESCRIPTION:" + "x" * (9 * 1024**2))
    with pytest.raises(CalendarImportError, match="calendar_import_memory_mib=128"):
        check_import_budget("large.ics", data, len(data), 50000, 128)


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
