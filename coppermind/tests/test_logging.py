"""Logging configuration, which every service runs before it can serve.

`COPPERMIND_LOG_LEVEL` is an operator knob that compose forwards to bootstrap,
migrate, store and api, so what it does with a value it does not recognise
decides whether the stack comes up at all.
"""

from __future__ import annotations

import json

import pytest
import structlog

from coppermind.logging import configure_logging, get_logger


@pytest.fixture(autouse=True)
def restore_logging():
    yield
    structlog.reset_defaults()
    configure_logging("tests")


def lines(captured: str) -> list[dict]:
    return [json.loads(line) for line in captured.splitlines() if line.startswith("{")]


def test_a_level_the_operator_typed_wrong_falls_back_instead_of_raising(capsys):
    """A typo in the compose environment must not stop a service from starting."""
    configure_logging("coppermind-store", "VERBOSE")
    get_logger("unknown-level-probe").debug("should be filtered out")
    get_logger("unknown-level-probe").info("should survive at INFO")

    events = [record["event"] for record in lines(capsys.readouterr().out)]
    assert "should be filtered out" not in events
    assert "should survive at INFO" in events
    assert any("log level not understood" in event for event in events)


def test_a_level_the_operator_typed_right_takes_effect(capsys):
    configure_logging("coppermind-store", "debug")
    get_logger("debug-level-probe").debug("visible at debug")

    events = [record["event"] for record in lines(capsys.readouterr().out)]
    assert "visible at debug" in events
    assert not any("log level not understood" in event for event in events)
