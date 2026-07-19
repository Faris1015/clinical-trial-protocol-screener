"""Upload-edge guards (#15): content-type allowlist, size cap, filename sanitize."""

import pytest

from app.exceptions import PayloadTooLargeError, UnsupportedMediaTypeError
from app.services.uploads import (
    read_upload_capped,
    sanitize_filename,
    validate_content_type,
)

ALLOWED = frozenset({"application/pdf", "text/markdown", "text/plain"})


# --- content-type allowlist -------------------------------------------------


@pytest.mark.parametrize(
    ("content_type", "filename"),
    [
        ("application/pdf", "protocol.pdf"),
        ("text/markdown", "protocol.md"),
        ("text/plain", "protocol.txt"),
        ("application/pdf; charset=binary", "protocol.pdf"),  # params ignored
        ("APPLICATION/PDF", "protocol.pdf"),  # case-insensitive
    ],
)
def test_allowlisted_types_pass(content_type, filename):
    validate_content_type(content_type, filename, ALLOWED)  # no raise


@pytest.mark.parametrize(
    ("content_type", "filename"),
    [
        ("application/octet-stream", "protocol.md"),
        ("", "protocol.txt"),
        (None, "protocol.pdf"),
        ("binary/octet-stream", "notes.markdown"),
    ],
)
def test_generic_type_falls_back_to_extension(content_type, filename):
    validate_content_type(content_type, filename, ALLOWED)  # no raise


@pytest.mark.parametrize(
    ("content_type", "filename"),
    [
        ("image/png", "x.png"),
        ("application/zip", "x.zip"),
        ("application/octet-stream", "malware.exe"),  # generic + non-allowed ext
        ("application/octet-stream", None),  # generic + no filename
    ],
)
def test_disallowed_types_raise_415(content_type, filename):
    with pytest.raises(UnsupportedMediaTypeError):
        validate_content_type(content_type, filename, ALLOWED)


# --- size cap ---------------------------------------------------------------


class FakeUpload:
    """Minimal async .read(n) shim over a bytes buffer, like UploadFile."""

    def __init__(self, data: bytes):
        self._buf = data
        self._pos = 0

    async def read(self, n: int = -1) -> bytes:
        chunk = self._buf[self._pos : self._pos + n] if n > 0 else self._buf[self._pos :]
        self._pos += len(chunk)
        return chunk


async def test_read_under_cap_returns_all_bytes():
    data = b"x" * 1000
    out = await read_upload_capped(FakeUpload(data), max_bytes=2000)
    assert out == data


async def test_read_over_cap_raises_413():
    data = b"x" * 5000
    with pytest.raises(PayloadTooLargeError):
        await read_upload_capped(FakeUpload(data), max_bytes=1000)


async def test_read_at_exactly_cap_is_allowed():
    data = b"x" * 1000
    out = await read_upload_capped(FakeUpload(data), max_bytes=1000)
    assert len(out) == 1000


# --- filename sanitization --------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("protocol.pdf", "protocol.pdf"),
        ("../../etc/passwd", "passwd"),
        ("..\\..\\windows\\system32\\evil.txt", "evil.txt"),
        ("/absolute/path/report.md", "report.md"),
        ("name with spaces.pdf", "name_with_spaces.pdf"),
        ("weird$name;rm -rf.pdf", "weird_name_rm_-rf.pdf"),
        (None, "upload"),
        ("", "upload"),
        ("...", "upload"),
        ("///", "upload"),
    ],
)
def test_sanitize_filename(raw, expected):
    assert sanitize_filename(raw) == expected


def test_sanitize_filename_bounds_length():
    assert len(sanitize_filename("a" * 500 + ".pdf")) <= 128


def test_sanitize_filename_strips_newlines_for_log_safety():
    # A CRLF in a filename must never reach a log line intact.
    out = sanitize_filename("evil\r\nINJECTED.pdf")
    assert "\n" not in out and "\r" not in out
