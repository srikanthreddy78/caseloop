from __future__ import annotations

import json
import logging

from app.config import Settings
from app.logging_setup import (
    AccessLogRedactor,
    JsonFormatter,
    TextFormatter,
    configure_logging,
    redact_tokens,
)


def _access_record(method: str, path: str, status: int = 200) -> logging.LogRecord:
    """A record shaped like uvicorn's: the request line arrives through ``args``."""
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "h11_impl.py",
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("10.0.0.7:5050", method, path, "1.1", status),
        None,
    )


def test_redact_tokens_hides_chat_session_tokens_and_token_query_values() -> None:
    assert (
        redact_tokens("/v1/chat/sessions/abcDEF123-_/messages?after=2026-01-01")
        == "/v1/chat/sessions/…/messages?after=2026-01-01"
    )
    assert redact_tokens("/v1/chat/sessions/abc") == "/v1/chat/sessions/…"
    assert redact_tokens("/v1/chat/sessions/abc?after=x") == "/v1/chat/sessions/…?after=x"
    assert redact_tokens("/dashboard/login?token=s3cret") == "/dashboard/login?token=…"
    assert redact_tokens("/x?a=1&token=s3cret&b=2") == "/x?a=1&token=…&b=2"
    assert redact_tokens("token=s3cret") == "token=…"
    # Untouched when nothing sensitive is there.
    assert redact_tokens("/v1/chat/sessions") == "/v1/chat/sessions"
    assert redact_tokens("/v1/cases?state=open") == "/v1/cases?state=open"


def test_access_log_filter_scrubs_the_request_line_and_keeps_the_record() -> None:
    redactor = AccessLogRedactor()
    record = _access_record("GET", "/v1/chat/sessions/tok123/stream?after=2026-01-01T00:00:00")
    assert redactor.filter(record) is True
    assert record.getMessage() == (
        '10.0.0.7:5050 - "GET /v1/chat/sessions/…/stream?after=2026-01-01T00:00:00 HTTP/1.1" 200'
    )
    login = _access_record("GET", "/dashboard/login?token=abc.def")
    redactor.filter(login)
    assert "abc.def" not in login.getMessage()
    assert "/dashboard/login?token=…" in login.getMessage()
    # Both formatters see the scrubbed record.
    assert "tok123" not in JsonFormatter().format(record)
    assert "tok123" not in TextFormatter().format(record)
    plain = _access_record("GET", "/healthz")
    redactor.filter(plain)
    assert plain.getMessage() == '10.0.0.7:5050 - "GET /healthz HTTP/1.1" 200'


def test_configure_logging_installs_the_access_redactor_once() -> None:
    configure_logging(Settings(env="prod"))
    configure_logging(Settings(env="test"))
    access = logging.getLogger("uvicorn.access")
    assert len([f for f in access.filters if isinstance(f, AccessLogRedactor)]) == 1
    captured: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    handler = Capture()
    logging.getLogger().addHandler(handler)
    try:
        access.info(
            '%s - "%s %s HTTP/%s" %d',
            "1.2.3.4:1",
            "POST",
            "/v1/chat/sessions/tok456/attachments",
            "1.1",
            201,
        )
    finally:
        logging.getLogger().removeHandler(handler)
    assert captured == ['1.2.3.4:1 - "POST /v1/chat/sessions/…/attachments HTTP/1.1" 201']


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
