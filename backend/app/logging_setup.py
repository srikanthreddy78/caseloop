"""Process-wide logging: one JSON object per line in prod/test, plain text in dev.

Call ``configure_logging(settings)`` once at app construction. Any ``extra={...}`` fields passed
to a log call (``log.info("retrying", extra={"case_id": case.id})``) land as top-level keys in
the JSON line, so logs can be filtered by case, job, or business in whatever ships them.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from app.config import Settings

# Attributes every LogRecord carries; anything else on the record came from ``extra``.
_STANDARD_ATTRS = frozenset(
    {
        *vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys(),
        "message",
        "asctime",
        "taskName",
    }
)

_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key.startswith("_"):
                continue
            line[key] = value if isinstance(value, str | int | float | bool | None) else str(value)
        if record.exc_info:
            line["exception"] = self.formatException(record.exc_info)
        return json.dumps(line, default=str)


class TextFormatter(logging.Formatter):
    """Dev format: the usual one-liner, with extra fields appended as key=value."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = " ".join(
            f"{k}={v}"
            for k, v in record.__dict__.items()
            if k not in _STANDARD_ATTRS and not k.startswith("_")
        )
        return f"{base} {extras}" if extras else base


def configure_logging(settings: Settings) -> None:
    """Install a single stdout handler on the root logger. Safe to call more than once."""
    formatter: logging.Formatter = TextFormatter() if settings.env == "dev" else JsonFormatter()
    root = logging.getLogger()
    handler = next((h for h in root.handlers if getattr(h, "_caseloop", False)), None)
    if handler is None:
        handler = logging.StreamHandler(sys.stdout)
        handler._caseloop = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    handler.setFormatter(formatter)
    root.setLevel(logging.DEBUG if settings.env == "dev" else logging.INFO)
    # Route uvicorn's own loggers through the same handler so every line has one shape.
    for name in _UVICORN_LOGGERS:
        uv_logger = logging.getLogger(name)
        uv_logger.handlers.clear()
        uv_logger.propagate = True
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
