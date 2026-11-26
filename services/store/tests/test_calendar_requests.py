"""A page's calendar part is served from what was kept and never holds the page up.

On the lab install the Admin overview and a note's review stopped answering for 30 seconds at a
time. The calendar part of each request read the whole calendar again: Admin parsed a calendar
file for every disagreement that had no kept copy, and the review ran the full recurrence
expansion in a separate process three to five times per page. These tests count that work
instead of timing it, so they fail on the slow behaviour and not on a slow machine.
"""

import asyncio
import shutil
import threading
from types import SimpleNamespace

import pytest
from coppermind_store import admin_calendar, calendar_lookup
from coppermind_store.admin_calendar import AdminCalendar
from coppermind_store.calendar_archive import CalendarArchive
from coppermind_store.context_service import ContextService

from coppermind.context_config import FeatureAssignment
from coppermind.settings import default_settings
from services.store.tests.test_calendar_conflicts import event, ics

CONFLICTS = 6


def import_file(archive, name, *events):
    receipt = archive.submit("calendar", name, ics(*events))
    assert archive.process(receipt.id).state == "succeeded"
    return receipt


@pytest.fixture
def archive(tmp_path):
    """Two calendar files that disagree about CONFLICTS meetings, like overlapping exports."""
    archive = CalendarArchive(tmp_path / "state")
    uids = [f"meeting-{i}" for i in range(CONFLICTS + 20)]
    import_file(archive, "first.ics", *(event(uid) for uid in uids))
    import_file(
        archive,
        "second.ics",
        *(
            event(uid, title="Changed" if i < CONFLICTS else "Planning")
            for i, uid in enumerate(uids)
        ),
    )
    assert len(archive.catalog().conflicts) == CONFLICTS
    return archive


@pytest.fixture
def parsed(monkeypatch):
    """Every calendar file parse, counted."""
    calls = []
    real = admin_calendar.parse_bounded

    def counting(*args, **kwargs):
        calls.append(args[1])
        return real(*args, **kwargs)

    monkeypatch.setattr(admin_calendar, "parse_bounded", counting)
    return calls


def admin_for(archive):
    store = SimpleNamespace(
        context=SimpleNamespace(archive=archive),
        control=SimpleNamespace(settings=default_settings),
    )
    return AdminCalendar(store)


async def filled(archive):
    task = admin_calendar._FILLING.get(str(archive.root))
    if task is not None:
        await task


async def test_import_keeps_the_other_version_so_a_page_reads_no_calendar_file(archive, parsed):
    calendar = admin_for(archive)

    shown = await calendar.attention()

    assert not shown.get("unavailable")
    assert len(shown["conflicts"]) == CONFLICTS
    assert all(c["incoming"] and c["incoming"]["title"] == "Changed" for c in shown["conflicts"])
    assert all(c["incoming"]["file"] == "second.ics" for c in shown["conflicts"])
    assert parsed == []


async def test_disagreements_found_before_copies_were_kept_cost_one_file_read_not_one_each(
    archive, parsed
):
    shutil.rmtree(archive.root / "conflict-incoming", ignore_errors=True)
    calendar = admin_for(archive)

    # The request shows what it has and reads no file.
    first = await calendar.attention()
    assert len(first["conflicts"]) == CONFLICTS
    assert all(c["incoming"] is None for c in first["conflicts"])
    assert parsed == []

    # Asked again while the first read is still going, it does not start another.
    await calendar.attention()
    await calendar.view({})
    await filled(archive)

    # One read of the one calendar file that carries them all, however many disagreements.
    assert len(parsed) == 1
    later = await calendar.attention()
    assert all(c["incoming"] and c["incoming"]["title"] == "Changed" for c in later["conflicts"])
    assert len(parsed) == 1


async def test_a_disagreement_that_cannot_be_read_back_is_not_read_again_on_every_page(
    archive, parsed
):
    shutil.rmtree(archive.root / "conflict-incoming", ignore_errors=True)
    for original in (archive.root / "imports").glob("*/original"):
        original.write_bytes(b"not a calendar")
    calendar = admin_for(archive)

    await calendar.attention()
    await filled(archive)
    reads = len(parsed)
    for _ in range(3):
        await calendar.attention()
        await filled(archive)

    assert reads <= 2  # at most the files themselves, once
    assert len(parsed) == reads


async def test_choosing_reads_only_the_chosen_disagreement(archive, parsed):
    shutil.rmtree(archive.root / "conflict-incoming", ignore_errors=True)
    calendar = admin_for(archive)
    first = (await calendar.attention())["conflicts"][0]
    await filled(archive)
    parsed.clear()
    shutil.rmtree(archive.root / "conflict-incoming", ignore_errors=True)

    chosen = await calendar.choose(
        {"id": first["id"], "version": first["version"], "pick": "incoming"}
    )

    assert chosen["file"] == "second.ics"
    assert len(parsed) == 1
    assert len(archive.catalog().conflicts) == CONFLICTS - 1


async def test_the_page_is_shown_without_the_calendar_part_when_it_is_slow(archive, monkeypatch):
    monkeypatch.setattr(admin_calendar, "CALENDAR_BUDGET_SECONDS", 0.05)
    release = threading.Event()
    calendar = admin_for(archive)
    monkeypatch.setattr(calendar, "_conflicts", lambda: release.wait(10) or [])
    monkeypatch.setattr(calendar, "_files", lambda: release.wait(10) or [])

    try:
        started = asyncio.get_running_loop().time()
        attention = await calendar.attention()
        files = await calendar.files()
        waited = asyncio.get_running_loop().time() - started
    finally:
        release.set()

    assert attention == {"conflicts": [], "bad_files": [], "unavailable": True}
    assert files is None
    assert waited < 2


async def test_the_page_is_shown_without_the_calendar_part_when_it_fails(archive, monkeypatch):
    calendar = admin_for(archive)

    def broken():
        raise RuntimeError("the calendar database went away")

    monkeypatch.setattr(calendar, "_conflicts", broken)
    monkeypatch.setattr(calendar, "_files", broken)

    assert (await calendar.attention())["unavailable"] is True
    assert await calendar.files() is None


# -- which meetings fall around a day -------------------------------------------------------


@pytest.fixture
def lookups(monkeypatch):
    """Every full-calendar lookup, counted."""
    calls = []
    real = calendar_lookup.occurrences

    def counting(events, day, timezone):
        calls.append(day)
        return real(events, day, timezone)

    monkeypatch.setattr(calendar_lookup, "occurrences", counting)
    return calls


def review_context(archive):
    return SimpleNamespace(
        archive=archive,
        recordings=SimpleNamespace(jobs=lambda: []),
        config=lambda: SimpleNamespace(
            assignments={"enrichment": FeatureAssignment()}, lifecycle_enabled=True
        ),
        read=lambda name, default=None: default,
        store=SimpleNamespace(
            control=SimpleNamespace(
                settings=default_settings,
                schema=lambda: SimpleNamespace(role=lambda name: "date"),
            )
        ),
    )


NOTE = SimpleNamespace(id="note", frontmatter={"date": "2026-10-14"}, title="Planning")


async def test_one_review_page_runs_the_calendar_lookup_once_not_once_per_question(
    archive, lookups
):
    context = review_context(archive)

    # What review detail asks: the suggestions' evidence, the recording's meeting, its hash.
    for _ in range(5):
        await ContextService.calendar_candidates(context, NOTE)
    await asyncio.gather(*[ContextService.calendar_candidates(context, NOTE) for _ in range(5)])

    assert len(lookups) == 1


async def test_the_lookup_is_run_again_only_when_the_calendar_changes(archive, lookups):
    context = review_context(archive)
    before = await ContextService.calendar_candidates(context, NOTE)
    assert len(before) == 20 + CONFLICTS and len(lookups) == 1

    import_file(archive, "third.ics", event("meeting-new", title="Added later"))
    after = await ContextService.calendar_candidates(context, NOTE)

    assert len(lookups) == 2
    assert len(after) == len(before) + 1
    # And what was returned cannot be changed by a caller into what the next one sees.
    after[0]["title"] = "mutated"
    assert (await ContextService.calendar_candidates(context, NOTE))[0]["title"] != "mutated"
    assert len(lookups) == 2


async def test_a_slow_calendar_lookup_degrades_the_page_and_is_still_shared(archive, monkeypatch):
    context = review_context(archive)
    release = threading.Event()
    started = []
    real = calendar_lookup.occurrences

    def slow(events, day, timezone):
        started.append(day)
        release.wait(10)
        return real(events, day, timezone)

    monkeypatch.setattr(calendar_lookup, "occurrences", slow)

    try:
        with calendar_lookup.degrading(0.05) as calendar:
            await ContextService.calendar_candidates(context, NOTE)
        assert calendar.unavailable
        # A second page asking meanwhile joins the lookup already running.
        with calendar_lookup.degrading(0.05) as again:
            await ContextService.calendar_candidates(context, NOTE)
        assert again.unavailable
        assert len(started) == 1
    finally:
        release.set()

    found = await ContextService.calendar_candidates(context, NOTE)
    assert found
    assert len(started) == 1


async def test_a_calendar_lookup_that_fails_degrades_the_page(archive, monkeypatch):
    from coppermind_store.calendar_import import CalendarImportError

    def failing(events, day, timezone):
        raise CalendarImportError("matching window exceeds 200 occurrences")

    monkeypatch.setattr(calendar_lookup, "occurrences", failing)
    with calendar_lookup.degrading(5) as calendar:
        await ContextService.calendar_candidates(review_context(archive), NOTE)
    assert calendar.unavailable
