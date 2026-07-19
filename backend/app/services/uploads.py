"""Upload-edge guards (#15): content-type allowlist, size cap, and filename
sanitization — everything that must happen to an untrusted upload before its
bytes reach the screening pipeline or its name touches a log line or the store.

Kept separate from the route so the rules are unit-testable without an HTTP
request, and separate from `screening` so the business logic never re-derives
what the edge already validated.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Protocol

from app.exceptions import PayloadTooLargeError, UnsupportedMediaTypeError


class Readable(Protocol):
    """The slice of ``fastapi.UploadFile`` the size cap needs — a chunked async
    read. Depending on this (not the concrete UploadFile) keeps the cap unit-
    testable with a plain bytes shim."""

    async def read(self, size: int = -1) -> bytes: ...


# Read the multipart part in bounded chunks so a hostile upload never buffers
# more than one chunk past the cap before we abort.
_CHUNK_BYTES = 64 * 1024

# When the client sends a generic/empty content type we can't trust it, so we
# fall back to the filename extension. These map to the allowlisted text types.
_ALLOWED_EXTENSIONS = frozenset({".pdf", ".md", ".markdown", ".txt"})
_GENERIC_CONTENT_TYPES = frozenset({"", "application/octet-stream", "binary/octet-stream"})

# Anything outside this set is stripped from a stored/echoed filename. Keeps
# path separators, control chars, and shell/log-hostile characters out.
_SAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")
_MAX_FILENAME_LEN = 128


def validate_content_type(
    content_type: str | None, filename: str | None, allowed: frozenset[str]
) -> None:
    """Reject uploads whose type isn't allowlisted, before reading the body.

    A concrete, non-allowlisted type (image/png, application/zip) is a hard 415.
    A generic type (octet-stream / missing) is trusted only if the filename
    carries an allowlisted extension — browsers often send octet-stream for
    .md/.txt files, and rejecting those would break legitimate uploads.
    """
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    if ctype in allowed:
        return
    if ctype in _GENERIC_CONTENT_TYPES:
        ext = PurePosixPath(filename or "").suffix.lower()
        if ext in _ALLOWED_EXTENSIONS:
            return
    raise UnsupportedMediaTypeError(
        f"Unsupported upload type {content_type!r}. "
        f"Allowed: {', '.join(sorted(allowed))} (or a .pdf/.md/.txt file)."
    )


async def read_upload_capped(file: Readable, max_bytes: int) -> bytes:
    """Stream the upload into memory, aborting with 413 as soon as it exceeds
    the cap — never buffering the whole of an oversized body."""
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(_CHUNK_BYTES):
        total += len(chunk)
        if total > max_bytes:
            raise PayloadTooLargeError(
                f"Upload exceeds the {max_bytes} byte limit.",
                headers={"Connection": "close"},
            )
        chunks.append(chunk)
    return b"".join(chunks)


def sanitize_filename(filename: str | None) -> str:
    """Reduce an untrusted filename to a safe basename for storage/logging.

    Strips any directory component (POSIX *and* Windows separators, so
    `..\\..\\etc` can't slip past on a POSIX server), drops everything outside a
    conservative charset, and bounds the length. Returns "upload" when nothing
    usable survives, so callers always get a non-empty, traversal-free name.
    """
    if not filename:
        return "upload"
    # Take the basename under both separator conventions — a Windows client can
    # send backslash-delimited paths that PurePosixPath would treat as one name.
    base = PureWindowsPath(PurePosixPath(filename).name).name
    cleaned = _SAFE_FILENAME_CHARS.sub("_", base).strip("._")
    cleaned = cleaned[:_MAX_FILENAME_LEN]
    return cleaned or "upload"
