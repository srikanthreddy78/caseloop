import base64
import hashlib
import hmac
import time

import pytest
from app.webhooks.signatures import InvalidSignature, dial_signature, verify_dial, verify_resend

SECRET = "whsec_testsecret"


def test_dial_valid_signature() -> None:
    body = b'{"type":"message.received"}'
    ts = str(int(time.time()))
    header = f"t={ts},v1={dial_signature(SECRET, ts, body)}"
    verify_dial(SECRET, header, body)


def test_dial_rejects_tampered_body_and_wrong_secret() -> None:
    body = b'{"a":1}'
    ts = str(int(time.time()))
    header = f"t={ts},v1={dial_signature(SECRET, ts, body)}"
    with pytest.raises(InvalidSignature):
        verify_dial(SECRET, header, b'{"a":2}')
    with pytest.raises(InvalidSignature):
        verify_dial("whsec_other", header, body)


def test_dial_rejects_old_timestamp_and_bad_header() -> None:
    body = b"{}"
    old = str(int(time.time()) - 600)
    header = f"t={old},v1={dial_signature(SECRET, old, body)}"
    with pytest.raises(InvalidSignature):
        verify_dial(SECRET, header, body)
    with pytest.raises(InvalidSignature):
        verify_dial(SECRET, None, body)
    with pytest.raises(InvalidSignature):
        verify_dial(SECRET, "garbage", body)


def _svix_headers(secret_b64: str, msg_id: str, ts: int, body: bytes) -> dict[str, str]:
    key = base64.b64decode(secret_b64)
    to_sign = f"{msg_id}.{ts}.".encode() + body
    sig = base64.b64encode(hmac.new(key, to_sign, hashlib.sha256).digest()).decode()
    return {"svix-id": msg_id, "svix-timestamp": str(ts), "svix-signature": f"v1,{sig}"}


def test_resend_svix_roundtrip() -> None:
    raw = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()
    secret = "whsec_" + raw
    body = b'{"type":"email.received","data":{"email_id":"x"}}'
    ts = int(time.time())
    verify_resend(secret, body, _svix_headers(raw, "msg_1", ts, body))
    with pytest.raises(InvalidSignature):
        verify_resend(secret, body + b" ", _svix_headers(raw, "msg_1", ts, body))
