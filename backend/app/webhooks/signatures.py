"""Inbound webhook signature verification. Pure functions, raise on failure."""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Mapping

from svix.webhooks import Webhook, WebhookVerificationError

DIAL_TOLERANCE_SECONDS = 5 * 60


class InvalidSignature(ValueError):
    pass


def verify_resend(secret: str, raw_body: bytes, headers: Mapping[str, str]) -> None:
    """Resend signs with Svix: svix-id, svix-timestamp, svix-signature."""
    try:
        Webhook(secret).verify(raw_body, dict(headers))
    except WebhookVerificationError as exc:
        raise InvalidSignature(str(exc)) from exc


def dial_signature(secret: str, timestamp: str, raw_body: bytes) -> str:
    message = f"{timestamp}.".encode() + raw_body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify_dial(
    secret: str, header: str | None, raw_body: bytes, *, now: float | None = None
) -> None:
    """Dial header: `X-Dial-Signature: t=<unix-seconds>,v1=<hex-hmac-sha256 of "{t}.{body}">`."""
    if not header:
        raise InvalidSignature("missing X-Dial-Signature header")
    parts: dict[str, str] = {}
    for item in header.split(","):
        key, _, value = item.strip().partition("=")
        if key and value:
            parts[key] = value
    timestamp, provided = parts.get("t"), parts.get("v1")
    if not timestamp or not provided:
        raise InvalidSignature("malformed X-Dial-Signature header")
    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise InvalidSignature("non-numeric timestamp") from exc
    current = now if now is not None else time.time()
    if abs(current - ts) > DIAL_TOLERANCE_SECONDS:
        raise InvalidSignature("signature timestamp outside tolerance")
    expected = dial_signature(secret, timestamp, raw_body)
    if not hmac.compare_digest(expected, provided):
        raise InvalidSignature("signature mismatch")
