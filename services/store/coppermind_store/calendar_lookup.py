"""Which meetings fall around a day, worked out once per calendar change.

Finding them reads every meeting in the calendar and expands the recurring ones in a
separate process, which takes seconds on a large calendar. A note's review page needs the
answer several times over (suggestions, the recording's meeting, the hash that says
whether the suggestions are stale), and background jobs ask the same question, so the
answer is kept until the calendar changes and one slow lookup is shared by whoever is
waiting for it, never repeated beside it.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
from collections.abc import Iterator
from contextvars import ContextVar
from datetime import date
from pathlib import Path
from time import monotonic
from typing import Any

from coppermind_store import calendar_mirror as mirror
from coppermind_store.calendar_import import CALENDAR_ID, CalendarImportError
from coppermind_store.calendar_worker import occurrences

log = logging.getLogger(__name__)

# Days kept per calendar state; the review page and background jobs ask about a few at a time.
KEEP = 64

_Key = tuple[str, str, str, str]
_KEPT: dict[_Key, list[dict[str, Any]]] = {}
_RUNNING: dict[_Key, asyncio.Task[list[dict[str, Any]]]] = {}
_DEADLINE: ContextVar[float | None] = ContextVar("calendar_deadline", default=None)


class CalendarUnavailable(Exception):
    """The calendar did not answer in the time a page allows. The lookup carries on without it."""


class Outcome:
    """Whether the calendar failed to answer inside a `degrading` block."""

    unavailable = False


@contextlib.contextmanager
def degrading(seconds: float) -> Iterator[Outcome]:
    """Calendar lookups inside share one budget of `seconds`; running out ends the block.

    A lookup that is too slow, or that fails, leaves the block quietly with
    `outcome.unavailable` set, so a page can be shown without what the calendar would have
    added instead of failing as a whole.
    """
    outcome = Outcome()
    token = _DEADLINE.set(monotonic() + seconds)
    try:
        yield outcome
    except (CalendarUnavailable, CalendarImportError):
        outcome.unavailable = True
    finally:
        _DEADLINE.reset(token)


def _lookup(archive: Any, day: date, timezone: str) -> list[dict[str, Any]]:
    catalog = archive.catalog()
    return occurrences(catalog.events.page(None, calendar_id=CALENDAR_ID), day, timezone)


async def around(archive: Any, day: date, timezone: str) -> list[dict[str, Any]]:
    """The occurrences near `day`, with descriptions. Raises CalendarUnavailable past a deadline."""
    root: Path = archive.root
    key = (str(root), mirror.signature(root / "catalog.json"), day.isoformat(), timezone)
    if key in _KEPT:
        return copy.deepcopy(_KEPT[key])
    running = _RUNNING.get(key)
    if running is None:
        running = asyncio.get_running_loop().create_task(
            asyncio.to_thread(_lookup, archive, day, timezone)
        )
        _RUNNING[key] = running
        running.add_done_callback(lambda done: _finished(key, done))
    limit = _DEADLINE.get()
    try:
        if limit is None:
            found = await asyncio.shield(running)
        else:
            found = await asyncio.wait_for(asyncio.shield(running), max(limit - monotonic(), 0))
    except TimeoutError as exc:
        raise CalendarUnavailable("the calendar did not answer in time") from exc
    return copy.deepcopy(found)


def _finished(key: _Key, done: asyncio.Task[list[dict[str, Any]]]) -> None:
    if _RUNNING.get(key) is done:
        del _RUNNING[key]
    if done.cancelled() or done.exception() is not None:
        if not done.cancelled():
            log.warning("calendar_lookup_failed", exc_info=done.exception())
        return
    _KEPT[key] = done.result()
    while len(_KEPT) > KEEP:
        del _KEPT[next(iter(_KEPT))]
