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
ALLOWED_TYPES: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "application/pdf": ".pdf",
}
_CHUNK = 1024 * 1024


def safe_filename(name: str | None, content_type: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]", "_", Path(name or "").name).strip("._") or "file"
    if "." not in base:
        base += ALLOWED_TYPES.get(content_type, "")
    return base[:120]


async def store_uploads(files: list[UploadFile], folder: Path, data_dir: Path) -> list[dict]:
    """Validate and write each upload; return the metadata the message row stores."""
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
    try:
        for f in files:
            ctype = (f.content_type or "").split(";", 1)[0].strip().lower()
            filename = safe_filename(f.filename, ctype)
            path = folder / f"{uuid.uuid4().hex[:8]}_{filename}"
            size = 0
            with path.open("wb") as out:
                written.append(path)
                while chunk := await f.read(_CHUNK):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise HTTPException(
                            status.HTTP_413_CONTENT_TOO_LARGE,
                            f"{filename} is over {MAX_BYTES // (1024 * 1024)} MB",
                        )
                    out.write(chunk)
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
