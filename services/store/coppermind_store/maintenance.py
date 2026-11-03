"""Let ordinary writers overlap, with an exclusive barrier for retention purge."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from functools import wraps


class MaintenanceGate:
    def __init__(self):
        self.condition = asyncio.Condition()
        self.holders = {}
        self.owner = None
        self.waiting = 0

    @asynccontextmanager
    async def shared(self):
        task = asyncio.current_task()
        async with self.condition:
            await self.condition.wait_for(
                lambda: (
                    task == self.owner
                    or task in self.holders
                    or (self.owner is None and not self.waiting)
                )
            )
            self.holders[task] = self.holders.get(task, 0) + 1
        try:
            yield
        finally:
            async with self.condition:
                self.holders[task] -= 1
                if not self.holders[task]:
                    del self.holders[task]
                self.condition.notify_all()

    @asynccontextmanager
    async def exclusive(self):
        async with self.condition:
            self.waiting += 1
            try:
                await self.condition.wait_for(lambda: not self.holders and self.owner is None)
                self.owner = asyncio.current_task()
            finally:
                self.waiting -= 1
                self.condition.notify_all()
        try:
            yield
        finally:
            async with self.condition:
                self.owner = None
                self.condition.notify_all()


def writer(function):
    @wraps(function)
    async def guarded(store, *args, **kwargs):
        async with store.maintenance.shared():
            return await function(store, *args, **kwargs)

    return guarded
