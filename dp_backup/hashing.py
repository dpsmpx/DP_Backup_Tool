"""Content fingerprinting used to match source files to structure entries.

Three modes are offered:

``full``
    SHA-256 over the whole file. Unambiguous, but reads every byte.
``partial``
    SHA-256 over the size plus the leading and trailing 64 KiB. Much cheaper on
    large media files while still distinguishing essentially any real pair of
    same-sized files.
``none``
    No hashing. Matching then falls back to name and size only, which is what
    the 1.x tool did.

A partial digest is domain-separated from a full one, so the two can never
compare equal by accident even for a file small enough to be read whole.
"""

from __future__ import annotations

import hashlib
import os

HASH_NONE = "none"
HASH_PARTIAL = "partial"
HASH_FULL = "full"
HASH_MODES = (HASH_FULL, HASH_PARTIAL, HASH_NONE)

#: Bytes read per chunk when streaming a whole file.
CHUNK_SIZE = 1024 * 1024

#: Bytes taken from each end of the file in ``partial`` mode.
PARTIAL_EDGE = 64 * 1024

_PARTIAL_PREFIX = b"dp_backup-partial-v1"


def hash_file(path: str, mode: str, *, size: int | None = None) -> str:
    """Return the hex digest of *path* under *mode*.

    Raises :class:`ValueError` for an unknown mode and :class:`OSError` if the
    file cannot be read; callers are expected to handle the latter and carry on.
    """
    if mode == HASH_NONE:
        return ""
    if mode == HASH_FULL:
        return _hash_full(path)
    if mode == HASH_PARTIAL:
        return _hash_partial(path, size)
    raise ValueError(f"unknown hash mode: {mode!r}")


def _hash_full(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_partial(path: str, size: int | None) -> str:
    if size is None:
        size = os.path.getsize(path)
    digest = hashlib.sha256()
    digest.update(_PARTIAL_PREFIX)
    digest.update(str(size).encode("ascii"))
    with open(path, "rb") as handle:
        if size <= 2 * PARTIAL_EDGE:
            # Small enough to read once; no seek needed and no overlap concerns.
            digest.update(handle.read())
        else:
            digest.update(handle.read(PARTIAL_EDGE))
            handle.seek(-PARTIAL_EDGE, os.SEEK_END)
            digest.update(handle.read(PARTIAL_EDGE))
    return digest.hexdigest()
