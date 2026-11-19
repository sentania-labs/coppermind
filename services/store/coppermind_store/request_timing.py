"""Correlated request and slow-operation timings, without logging request contents."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from time import monotonic
from typing import Any, TypeVar
from uuid import uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from coppermind.logging import get_logger

log = get_logger("coppermind-store")
request_id: ContextVar[str] = ContextVar("store_request_id", default="")
SLOW_SECONDS = 2.0
T = TypeVar("T")


class RequestTiming:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/internal/"):
            await self.app(scope, receive, send)
            return
        supplied = dict(scope.get("headers", [])).get(b"x-request-id", b"").decode("latin1")
        identity = supplied if re.fullmatch("[0-9a-f]{32}", supplied) else uuid4().hex
        token = request_id.set(identity)
        fields = {"request_id": identity, "method": scope["method"], "path": scope["path"]}
        start = monotonic()
        status = None
        outcome = "error"

        async def tracked_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        log.info("store request started", **fields)
        try:
            await self.app(scope, receive, tracked_send)
            outcome = "completed"
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            log.info(
                "store request finished",
                **fields,
                status=status,
                outcome=outcome,
                duration_ms=round((monotonic() - start) * 1000, 3),
            )
            request_id.reset(token)


class SlowOperation:
    """Report completed steps and the active step at two seconds and on slow completion."""

    def __init__(self, operation: str) -> None:
        self.operation = operation
        self.steps: dict[str, float] = {}
        self.executor_wait: dict[str, float] = {}
        self.active_step = ""
        self.step_started = self.started = monotonic()
        self.identity = request_id.get()

    def __enter__(self) -> SlowOperation:
        self.watchdog = asyncio.get_running_loop().call_later(SLOW_SECONDS, self.report, "waiting")
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.watchdog.cancel()
        if monotonic() - self.started >= SLOW_SECONDS:
            self.report("completed" if exc_type is None else "error")

    def report(self, outcome: str) -> None:
        current = monotonic()
        log.warning(
            "slow store operation",
            operation=self.operation,
            request_id=self.identity,
            duration_ms=round((current - self.started) * 1000, 3),
            steps_ms=dict(self.steps),
            executor_wait_ms=dict(self.executor_wait),
            active_step=self.active_step,
            active_step_ms=round((current - self.step_started) * 1000, 3)
            if self.active_step
            else 0,
            outcome=outcome,
        )

    @contextmanager
    def step(self, name: str) -> Iterator[None]:
        self.active_step = name
        self.step_started = monotonic()
        try:
            yield
        finally:
            self.steps[name] = self.steps.get(name, 0) + round(
                (monotonic() - self.step_started) * 1000, 3
            )
            self.active_step = ""

    async def to_thread(self, name: str, call: Callable[[], T]) -> T:
        queued = monotonic()

        def run() -> T:
            self.executor_wait[name] = round((monotonic() - queued) * 1000, 3)
            return call()

        return await asyncio.to_thread(run)

    async def read(self, name: str, call: Awaitable[T]) -> T:
        with self.step(name):
            return await call

    def call(self, name: str, call: Callable[[], T]) -> T:
        with self.step(name):
            return call()
