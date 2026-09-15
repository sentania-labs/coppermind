"""When the daily full rehash falls due, in the operator's own timezone."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from coppermind_store import reconciler
from coppermind_store.reconciler import ReconcilerStatus, _next_full_rehash

from coppermind.settings import ReconcileSettings
from coppermind.store_protocol import MetadataUnavailable

CHICAGO = ZoneInfo("America/Chicago")


def test_the_rehash_is_due_at_the_next_local_wall_clock_time():
    before = datetime(2026, 9, 15, 6, 0, tzinfo=UTC)

    due = _next_full_rehash(before, "03:30", CHICAGO)

    assert due == datetime(2026, 9, 15, 8, 30, tzinfo=UTC)
    assert due.astimezone(CHICAGO).hour == 3


def test_a_time_already_past_today_rolls_to_tomorrow():
    after = datetime(2026, 9, 15, 4, 0, tzinfo=CHICAGO)

    due = _next_full_rehash(after, "03:30", CHICAGO)

    assert due == datetime(2026, 9, 16, 3, 30, tzinfo=CHICAGO)


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


def test_a_reconciler_that_never_completes_a_first_scan_is_reported_stale():
    status = ReconcilerStatus()
    status.scan_interval_s = 60
    status.started_at = datetime.now(UTC) - timedelta(seconds=600)

    assert status.last_completed_at is None
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


async def test_a_deferred_daily_rehash_is_retried_rather_than_forfeited(monkeypatch):
    """The full pass is the only one that sees a change that moved no mtime."""
    calls: list[bool] = []
    completed: list[bool] = []
    failures: list[str] = []
    finished = asyncio.Event()
    past = datetime(2000, 1, 1, tzinfo=UTC)
    ahead = datetime(2400, 1, 1, tzinfo=UTC)

    monkeypatch.setattr(reconciler, "_cadence", lambda _store: (0, 0, "03:30", ZoneInfo("UTC")))
    monkeypatch.setattr(
        reconciler, "_next_full_rehash", lambda *_args: ahead if completed else past
    )

    async def scan(_store, *, full, quiet_period_s):
        calls.append(full)
        if len(calls) >= 3:
            finished.set()
        if full and not failures:
            failures.append("deferred")
            raise MetadataUnavailable("postgres restarting")
        completed.append(full)
        return {"changed": 0, "moved": 0, "missing": 0, "unparsed": 0, "deferred": 0}

    monkeypatch.setattr(reconciler, "reconcile_once", scan)
    task = asyncio.create_task(reconciler.run_reconciler(object()))
    try:
        await asyncio.wait_for(finished.wait(), timeout=5)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    assert calls[:3] == [True, True, False]
    assert True in completed


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
    monkeypatch.setattr(reconciler, "_next_full_rehash", lambda *_args: None)

    async def scan(_store, *, full, quiet_period_s):
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
