"""Email over Resend: threaded replies out, Receiving webhook in."""

from __future__ import annotations

import asyncio
import html as html_lib
import logging
import re
from email.utils import parseaddr
from pathlib import Path
from typing import Any

import httpx
import resend

from app.channels.base import ChannelUnavailable, SendResult
from app.models import Business, Case, Customer

log = logging.getLogger(__name__)

_QUOTE_MARKERS = [
    re.compile(r"^On .{3,120} wrote:\s*$", re.MULTILINE),
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^From: .+\nSent: .+", re.MULTILINE),
]


def strip_quoted_reply(text: str) -> str:
    """Drop quoted history so the agent sees only what the customer just wrote."""
    cut = len(text)
    for marker in _QUOTE_MARKERS:
        m = marker.search(text)
        if m and m.start() < cut:
            cut = m.start()
    head = text[:cut]
    lines = [ln for ln in head.splitlines() if not ln.lstrip().startswith(">")]
    return "\n".join(lines).strip() or text.strip()


def html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", html)
    text = re.sub(r"<[^>]+>", "", html)
    text = html_lib.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_sender(value: str) -> tuple[str | None, str]:
    name, addr = parseaddr(value or "")
    return (name or None), addr.lower()


class ResendEmailAdapter:
    channel = "email"

    def __init__(self, api_key: str) -> None:
        resend.api_key = api_key

    async def send(
        self, *, business: Business, case: Case, customer: Customer, text: str
    ) -> SendResult:
        if not customer.email:
            raise ChannelUnavailable("customer has no email address")
        subject = case.subject or "Your request"
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        headers: dict[str, str] = {}
        thread = [m for m in (case.email_thread_message_ids or []) if m.startswith("<")]
        if thread:
            headers["In-Reply-To"] = thread[-1]
            headers["References"] = " ".join(thread)
        params: dict[str, Any] = {
            "from": f"{business.name} <{business.from_email}>",
            "to": [customer.email],
            "subject": subject,
            "text": text,
            "headers": headers,
        }
        response = await asyncio.to_thread(resend.Emails.send, params)  # type: ignore[arg-type]
        return SendResult(provider_message_id=_id_of(response))

    async def send_email(
        self, *, to: str, subject: str, text: str, from_email: str | None = None
    ) -> str | None:
        params: dict[str, Any] = {
            "from": from_email or "caseloop@localhost",
            "to": [to],
            "subject": subject,
            "text": text,
        }
        response = await asyncio.to_thread(resend.Emails.send, params)  # type: ignore[arg-type]
        return _id_of(response)


def _id_of(response: Any) -> str | None:
    if isinstance(response, dict):
        return response.get("id")
    return getattr(response, "id", None)


async def fetch_received_email(email_id: str) -> dict[str, Any]:
    received = await resend.Emails.Receiving.get_async(email_id)
    return dict(received)


async def download_attachments(
    email_id: str, attachments: list[dict[str, Any]], data_dir: Path
) -> list[dict[str, Any]]:
    """Pull each attachment to disk; return the metadata the message row stores."""
    stored: list[dict[str, Any]] = []
    folder = data_dir / "attachments" / email_id
    async with httpx.AsyncClient(timeout=30) as http:
        for att in attachments:
            att_id = str(att.get("id") or "")
            filename = re.sub(r"[^A-Za-z0-9._-]", "_", str(att.get("filename") or att_id or "file"))
            entry: dict[str, Any] = {
                "resend_id": att_id,
                "filename": filename,
                "content_type": att.get("content_type"),
                "size": att.get("size"),
            }
            try:
                details = await resend.Emails.Receiving.Attachments.get_async(email_id, att_id)
                url = str(dict(details).get("download_url") or "")
                if url:
                    folder.mkdir(parents=True, exist_ok=True)
                    path = folder / f"{att_id[:8]}_{filename}"
                    resp = await http.get(url)
                    resp.raise_for_status()
                    path.write_bytes(resp.content)
                    entry["storage_key"] = str(path.relative_to(data_dir))
                    entry["size"] = len(resp.content)
            except Exception:
                log.exception("attachment %s on %s could not be downloaded", att_id, email_id)
            stored.append(entry)
    return stored
