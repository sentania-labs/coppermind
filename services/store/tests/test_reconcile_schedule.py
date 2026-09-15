"""When the daily full rehash falls due, and how a stalled reconciler surfaces."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from coppermind_store import reconciler
from coppermind_store.reconciler import ReconcilerStatus

from coppermind.settings import ReconcileSettings
from coppermind.store_protocol import MetadataUnavailable

CHICAGO = ZoneInfo("America/Chicago")


def test_settings_refuse_a_rehash_time_that_is_not_a_wall_clock():
    for value in ("half past three", "25:00", "03:60", "3:30pm"):
        with pytest.raises(ValueError, match="full_rehash_daily_at"):
            ReconcileSettings(full_rehash_daily_at=value)
    assert ReconcileSettings(full_rehash_daily_at="23:59").full_rehash_daily_at == "23:59"


def test_a_reconciler_is_only_reported_wedged_after_several_failed_scans():
    status = ReconcilerStatus()
    assert status.problem() == ""

    status.deferred("MetadataUnavailable")
    status.deferred("MetadataUnavailable")
    assert status.problem() == ""

    status.deferred("MetadataUnavailable")
    assert "MetadataUnavailable" in status.problem()

    status.completed()
    assert status.problem() == ""
    assert status.last_completed_at is not None


def test_a_scan_that_stalls_without_raising_is_reported_stale():
    """A wedged walk never raises, so the failure counter alone stays green."""
    status = ReconcilerStatus()
    status.scan_interval_s = 60
    status.completed()
    assert status.consecutive_failures == 0
    assert status.problem() == ""

    status.last_completed_at = datetime.now(UTC) - timedelta(seconds=179)
    assert status.problem() == ""

    status.last_completed_at = datetime.now(UTC) - timedelta(seconds=600)
    stalled = status.problem()
    assert "600s ago" in stalled
    assert status.consecutive_failures == 0

    status.completed()
    assert status.problem() == ""


def test_a_long_first_scan_stays_ready_while_its_grace_holds():
    """Nothing has measured a scan here yet, so the deadline cannot judge one."""
    status = ReconcilerStatus()
    status.scan_interval_s = 60
    status.started_at = datetime.now(UTC) - timedelta(seconds=660)
    status.scanning()
    status.scan_started_at = datetime.now(UTC) - timedelta(seconds=600)

    assert status.last_completed_at is None
    assert status.longest_scan_s == 0.0
    assert status.problem() == ""

    status.completed()
    assert status.longest_scan_s >= 600
    assert status.problem() == ""


def test_a_first_scan_that_never_completes_is_reported_once_the_grace_runs_out():
    status = ReconcilerStatus()
    status.scan_interval_s = 60
    grace = reconciler._FIRST_SCAN_GRACE.total_seconds()
    status.started_at = datetime.now(UTC) - timedelta(seconds=grace + 600)
    status.scanning()
    status.scan_started_at = datetime.now(UTC) - timedelta(seconds=grace + 540)

    assert status.last_completed_at is None
    assert "no scan has completed" in status.problem()


def test_a_reconciler_that_never_starts_a_first_scan_is_reported_stale():
    status = ReconcilerStatus()
    status.scan_interval_s = 60
    grace = reconciler._FIRST_SCAN_GRACE.total_seconds()
    status.started_at = datetime.now(UTC) - timedelta(seconds=grace + 600)

    assert status.last_completed_at is None
    assert status.scan_started_at is None
    assert "no scan has completed" in status.problem()


def test_the_staleness_deadline_follows_the_configured_interval():
    slow = ReconcilerStatus()
    slow.scan_interval_s = 600
    slow.completed()
    slow.last_completed_at = datetime.now(UTC) - timedelta(seconds=600)

    assert slow.problem() == ""

    brisk = ReconcilerStatus()
    brisk.scan_interval_s = 5
    brisk.completed()
    brisk.last_completed_at = datetime.now(UTC) - timedelta(seconds=600)

    assert brisk.problem() != ""


async def _drive(monkeypatch, scan, calls, rehash_at="00:00"):
    """Run the loop with no sleep until `scan` has been called three times."""
    finished = asyncio.Event()

    monkeypatch.setattr(reconciler, "_cadence", lambda _store: (0, 0, rehash_at, ZoneInfo("UTC")))

    async def counted(_store, *, full, quiet_period_s, unidentified=None):
        calls.append(full)
        if len(calls) >= 3:
            finished.set()
        return await scan(full)

    monkeypatch.setattr(reconciler, "reconcile_once", counted)
    task = asyncio.create_task(reconciler.run_reconciler(object()))
    try:
        await asyncio.wait_for(finished.wait(), timeout=5)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def test_a_deferred_daily_rehash_is_retried_rather_than_forfeited(monkeypatch):
    """The full pass is the only one that sees a change that moved no mtime."""
    calls: list[bool] = []
    completed: list[bool] = []
    failures: list[str] = []

    async def scan(full):
        if full and not failures:
            failures.append("deferred")
            raise MetadataUnavailable("postgres restarting")
        completed.append(full)
        return {"changed": 0, "moved": 0, "missing": 0, "unparsed": 0, "deferred": 0}

    await _drive(monkeypatch, scan, calls)

    assert calls[:3] == [True, True, False]
    assert True in completed


async def test_the_rehash_runs_once_a_day_and_not_on_every_pass(monkeypatch):
    calls: list[bool] = []

    async def scan(_full):
        return {"changed": 0, "moved": 0, "missing": 0, "unparsed": 0, "deferred": 0}

    await _drive(monkeypatch, scan, calls)

    assert calls[:3] == [True, False, False]


def test_the_rehash_owes_a_run_once_the_local_hour_has_passed():
    morning = datetime(2026, 9, 15, 3, 29, tzinfo=CHICAGO)
    on_the_hour = datetime(2026, 9, 15, 3, 30, tzinfo=CHICAGO)
    evening = datetime(2026, 9, 15, 21, 0, tzinfo=CHICAGO)

    assert reconciler._rehash_due(morning, "03:30", None) is False
    assert reconciler._rehash_due(on_the_hour, "03:30", None) is True
    assert reconciler._rehash_due(evening, "03:30", None) is True


def test_one_rehash_a_day_and_the_next_day_owes_another():
    evening = datetime(2026, 9, 15, 21, 0, tzinfo=CHICAGO)
    tomorrow = datetime(2026, 9, 16, 4, 0, tzinfo=CHICAGO)

    assert reconciler._rehash_due(evening, "03:30", evening.date()) is False
    assert reconciler._rehash_due(tomorrow, "03:30", evening.date()) is True


def test_a_scan_in_flight_is_not_counted_as_silence():
    """The loop sleeps the interval and then scans, so a scan's own runtime is extra."""
    status = ReconcilerStatus()
    status.scan_interval_s = 60
    status.completed()
    status.last_completed_at = datetime.now(UTC) - timedelta(seconds=600)
    assert status.problem() != ""

    status.scanning()

    assert status.problem() == ""


def test_the_deadline_never_falls_below_a_scans_own_runtime():
    """A brisk interval is a legitimate setting, not a permanent not-ready."""
    status = ReconcilerStatus()
    status.scan_interval_s = 5
    status.scanning()
    status.scan_started_at = datetime.now(UTC) - timedelta(seconds=120)
    status.completed()
    assert status.longest_scan_s >= 120

    status.last_completed_at = datetime.now(UTC) - timedelta(seconds=200)
    assert status.problem() == ""

    status.last_completed_at = datetime.now(UTC) - timedelta(seconds=1000)
    assert status.problem() != ""


async def test_the_loop_marks_a_scan_in_flight_before_running_it(monkeypatch):
    status = ReconcilerStatus()
    in_flight: list[datetime | None] = []
    finished = asyncio.Event()

    monkeypatch.setattr(reconciler, "_cadence", lambda _store: (0, 0, "03:30", ZoneInfo("UTC")))

    async def scan(_store, *, full, quiet_period_s, unidentified=None):
        in_flight.append(status.scan_started_at)
        finished.set()
        return {"changed": 0, "moved": 0, "missing": 0, "unparsed": 0, "deferred": 0}

    monkeypatch.setattr(reconciler, "reconcile_once", scan)
    task = asyncio.create_task(reconciler.run_reconciler(object(), status))
    try:
        await asyncio.wait_for(finished.wait(), timeout=5)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    assert in_flight[0] is not None
    assert status.scan_started_at is None
