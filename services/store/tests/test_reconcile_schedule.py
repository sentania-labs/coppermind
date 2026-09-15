"""When the daily full rehash falls due, in the operator's own timezone."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from coppermind_store.reconciler import ReconcilerStatus, _next_full_rehash

from coppermind.settings import ReconcileSettings

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
