from __future__ import annotations

import json
import logging

from app.config import Settings
from app.logging_setup import JsonFormatter, TextFormatter, configure_logging


def _record(**extra: object) -> logging.LogRecord:
    record = logging.LogRecord("app.jobs", logging.INFO, "jobs.py", 1, "job %s", ("retried",), None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter_is_one_object_per_line_with_extras() -> None:
    line = JsonFormatter().format(_record(case_id="c-1", attempts=2))
    assert "\n" not in line
    data = json.loads(line)
    assert data["level"] == "INFO" and data["logger"] == "app.jobs"
    assert data["message"] == "job retried"
    assert data["case_id"] == "c-1" and data["attempts"] == 2
    assert data["ts"].endswith("+00:00")


def test_json_formatter_includes_exceptions() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = _record()
        record.exc_info = __import__("sys").exc_info()
    data = json.loads(JsonFormatter().format(record))
    assert "ValueError: boom" in data["exception"]


def test_text_formatter_appends_extras() -> None:
    line = TextFormatter().format(_record(case_id="c-1"))
    assert line.endswith("app.jobs: job retried case_id=c-1")


def test_configure_logging_installs_one_handler_and_picks_format_by_env() -> None:
    root = logging.getLogger()
    before = [h for h in root.handlers if not getattr(h, "_caseloop", False)]
    configure_logging(Settings(env="prod"))
    configure_logging(Settings(env="prod"))
    ours = [h for h in root.handlers if getattr(h, "_caseloop", False)]
    assert len(ours) == 1 and isinstance(ours[0].formatter, JsonFormatter)
    assert [h for h in root.handlers if h not in ours] == before
    assert logging.getLogger("uvicorn.access").propagate

    configure_logging(Settings(env="dev"))
    assert isinstance(ours[0].formatter, TextFormatter)
    configure_logging(Settings(env="test"))
