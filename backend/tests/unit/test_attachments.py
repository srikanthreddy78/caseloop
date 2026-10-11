from __future__ import annotations

from app.attachments import matches_declared_type


def test_magic_bytes_per_declared_type() -> None:
    assert matches_declared_type("image/jpeg", b"\xff\xd8\xff\xe1Exif", "a.jpg")
    assert matches_declared_type("image/png", b"\x89PNG\r\n\x1a\n\0\0", "a.png")
    assert matches_declared_type("image/gif", b"GIF87a\0", "a.gif")
    assert matches_declared_type("image/gif", b"GIF89a\0", "a.gif")
    assert matches_declared_type("image/webp", b"RIFF\x10\0\0\0WEBPVP8 ", "a.webp")
    assert matches_declared_type("application/pdf", b"%PDF-1.7\n", "a.pdf")
    # The filename never rescues a wrong signature.
    assert not matches_declared_type("image/png", b"GIF89a\0\0\0\0", "a.png")
    assert not matches_declared_type("image/webp", b"RIFF\x10\0\0\0WAVEfmt ", "a.webp")
    assert not matches_declared_type("application/pdf", b"\x89PNG\r\n\x1a\n", "a.pdf")
    assert not matches_declared_type("image/jpeg", b"", "a.jpg")
    assert not matches_declared_type("text/plain", b"hello", "a.txt")


def test_heif_is_accepted_by_extension_only() -> None:
    assert matches_declared_type("image/heic", b"\0\0\0\x18ftypheic", "IMG.HEIC")
    assert matches_declared_type("image/heif", b"anything", "a.heif")
    assert not matches_declared_type("image/heic", b"\0\0\0\x18ftypheic", "a.jpg")
    assert not matches_declared_type("image/heif", b"\0\0\0\x18ftypheic", "noext")
