"""Structured logging to stdout.

The hosting contract collects stdout and nothing else, so every service logs
JSON there and never to a file. One configuration function keeps the field
names identical across images, which is what makes a query across services
possible.
"""

from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(service: str, level: str = "INFO") -> None:
    """Configure JSON logging on stdout at `level`.

    An unrecognised level falls back to INFO and says so rather than raising.
    `COPPERMIND_LOG_LEVEL` is an operator knob that compose forwards to every
    service, so a typo in it must not stop the stack from coming up.
    """
    names = logging.getLevelNamesMapping()
    requested = level.upper()
    resolved = names.get(requested, logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=resolved)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(resolved),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    structlog.contextvars.bind_contextvars(service=service)
    if requested not in names:
        get_logger(service).warning(
            "log level not understood, using INFO",
            requested=level,
            known=sorted(names),
        )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
