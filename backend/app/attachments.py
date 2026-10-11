"""Files customers send with a message: validation, storage under data_dir, and safe lookup."""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

from fastapi import HTTPException, UploadFile, status

from app.models import Message

MAX_FILES = 5
MAX_BYTES = 8 * 1024 * 1024
# Per case, across every message: a chat is a conversation, not a backup drive.
MAX_CASE_FILES = 25
MAX_CASE_BYTES = 50 * 1024 * 1024
ALLOWED_TYPES: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "application/pdf": ".pdf",
}
# Leading bytes each declared type must start with. HEIC/HEIF carry a brand inside an ISO
# container that many encoders vary, so they are checked by extension and declared type only.
_MAGIC: dict[str, tuple[bytes, ...]] = {
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "image/webp": (b"RIFF",),
    "application/pdf": (b"%PDF-",),
}
_HEIF_EXTENSIONS = (".heic", ".heif")
_CHUNK = 1024 * 1024
# Downloads are served as-is but never run: no MIME sniffing, no scripts, no top-level navigation.
ATTACHMENT_HEADERS = {
    "Cache-Control": "private, max-age=3600",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox",
}


def matches_declared_type(content_type: str, head: bytes, filename: str) -> bool:
    """True when the first bytes (or, for HEIF, the extension) agree with the declared type."""
    if content_type in ("image/heic", "image/heif"):
        return filename.lower().endswith(_HEIF_EXTENSIONS)
    signatures = _MAGIC.get(content_type)
    if signatures is None:
        return False
    if content_type == "image/webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    return any(head.startswith(sig) for sig in signatures)


def safe_filename(name: str | None, content_type: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]", "_", Path(name or "").name).strip("._") or "file"
    if "." not in base:
        base += ALLOWED_TYPES.get(content_type, "")
    return base[:120]


async def store_uploads(
    files: list[UploadFile], folder: Path, data_dir: Path, *, byte_budget: int | None = None
) -> list[dict]:
    """Validate and write each upload; return the metadata the message row stores.

    ``byte_budget`` is how much the case may still take in total; crossing it is a 413.
    """
    if not files:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "no files were sent")
    if len(files) > MAX_FILES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"at most {MAX_FILES} files per message"
        )
    for f in files:
        ctype = (f.content_type or "").split(";", 1)[0].strip().lower()
        if ctype not in ALLOWED_TYPES:
            raise HTTPException(
                status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                f"{f.filename or 'file'}: only images and PDFs can be sent",
            )
        if f.size is not None and f.size > MAX_BYTES:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"{f.filename or 'file'} is over {MAX_BYTES // (1024 * 1024)} MB",
            )
    folder.mkdir(parents=True, exist_ok=True)
    stored: list[dict] = []
    written: list[Path] = []
    total = 0
    try:
        for f in files:
            ctype = (f.content_type or "").split(";", 1)[0].strip().lower()
            filename = safe_filename(f.filename, ctype)
            path = folder / f"{uuid.uuid4().hex[:8]}_{filename}"
            size = 0
            first = True
            with path.open("wb") as out:
                written.append(path)
                while chunk := await f.read(_CHUNK):
                    if first:
                        first = False
                        if not matches_declared_type(ctype, chunk[:16], f.filename or ""):
                            raise HTTPException(
                                status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                                f"{filename} does not look like a {ctype} file",
                            )
                    size += len(chunk)
                    total += len(chunk)
                    if size > MAX_BYTES:
                        raise HTTPException(
                            status.HTTP_413_CONTENT_TOO_LARGE,
                            f"{filename} is over {MAX_BYTES // (1024 * 1024)} MB",
                        )
                    if byte_budget is not None and total > byte_budget:
                        raise HTTPException(
                            status.HTTP_413_CONTENT_TOO_LARGE,
                            f"this chat has reached its {MAX_CASE_BYTES // (1024 * 1024)} MB "
                            "attachment limit",
                        )
                    out.write(chunk)
            if first:  # nothing was read: an empty file cannot be the type it claims
                raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, f"{filename} is empty")
            stored.append(
                {
                    "filename": filename,
                    "content_type": ctype,
                    "size": size,
                    "storage_key": str(path.relative_to(data_dir)),
                }
            )
    except Exception:
        for path in written:
            path.unlink(missing_ok=True)
        raise
    return stored


def resolve_stored(data_dir: Path, attachment: dict[str, Any]) -> Path | None:
    """Path of a stored attachment, or None if it was never downloaded or points outside data_dir."""
    key = attachment.get("storage_key")
    if not key:
        return None
    root = data_dir.resolve()
    path = (root / str(key)).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return None
    return path


def public_view(attachment: dict[str, Any], index: int) -> dict[str, Any]:
    """What clients see: no storage paths or provider ids."""
    return {
        "index": index,
        "filename": attachment.get("filename") or "file",
        "content_type": attachment.get("content_type"),
        "size": attachment.get("size"),
    }


def case_attachments(messages: list[Message]) -> list[tuple[Message, int, dict[str, Any]]]:
    """Every attachment on a case as (message, case-wide index, metadata), in message order."""
    out: list[tuple[Message, int, dict[str, Any]]] = []
    for m in sorted(messages, key=lambda m: (m.created_at, m.id)):
        for att in m.attachments or []:
            out.append((m, len(out), att))
    return out
