"""Structured logging. JSON to file (machine-parseable for observability),
human-readable to console. Every agent logs through this so a production run
is auditable end to end.
"""

from __future__ import annotations

import logging
from pathlib import Path

import structlog


def setup_logging(level: str = "INFO", file: str = "logs/crawl.log"):
    Path(file).parent.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        handlers=[logging.FileHandler(file, encoding="utf-8"), logging.StreamHandler()],
        format="%(message)s",
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    return structlog.get_logger()


def get_logger(name: str = "crawler"):
    return structlog.get_logger(name)
