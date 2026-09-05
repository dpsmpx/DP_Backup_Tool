"""Path normalisation and containment checks.

Relative paths inside a structure file are *untrusted input*: the file may have
been produced elsewhere, hand-edited, or corrupted. Every path taken from a
structure file must pass through :func:`normalize_relpath` and
:func:`safe_join` before it is used to touch the filesystem.
"""

from __future__ import annotations

import os
import re

# A leading drive letter such as ``C:``. Rejected on every platform, not just
# Windows, so that a structure file behaves identically wherever it is restored.
_DRIVE_RE = re.compile(r"^[A-Za-z]:")

# Windows device names that can never be created as files. Only enforced when
# actually running on Windows: ``aux.txt`` and ``con`` are perfectly legal POSIX
# names, and rejecting them would make valid Linux trees unrestorable.
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class UnsafePathError(ValueError):
    """Raised when a path from a structure file would escape the destination."""


def normalize_relpath(raw: object) -> str:
    """Validate an untrusted relative path and return it in POSIX form.

    Rejects absolute paths, drive letters, UNC prefixes, ``..`` traversal, NUL
    bytes and empty results. Both ``/`` and ``\\`` are treated as separators on
    every platform so a Windows-made structure file cannot escape on Linux and
    vice versa.

    Names that are merely *unusable on Windows* (reserved device names, trailing
    dots or spaces) are only rejected when running on Windows, so that valid
    POSIX trees stay restorable on POSIX.
    """
    if not isinstance(raw, str):
        raise UnsafePathError(f"path must be a string, got {type(raw).__name__}")
    if not raw:
        raise UnsafePathError("path is empty")
    if "\x00" in raw:
        raise UnsafePathError("path contains a NUL byte")

    text = raw.replace("\\", "/")

    if text.startswith("/"):
        raise UnsafePathError(f"absolute path is not allowed: {raw!r}")
    if _DRIVE_RE.match(text):
        raise UnsafePathError(f"drive-letter path is not allowed: {raw!r}")

    parts: list[str] = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise UnsafePathError(f"parent-directory traversal is not allowed: {raw!r}")
        if os.name == "nt":
            if part.rstrip(" .") == "":
                raise UnsafePathError(
                    f"path component {part!r} is not a usable name on Windows: {raw!r}"
                )
            if part.split(".")[0].upper() in _WINDOWS_RESERVED:
                raise UnsafePathError(
                    f"path component {part!r} is a reserved device name on Windows: {raw!r}"
                )
        parts.append(part)

    if not parts:
        raise UnsafePathError(f"path resolves to the destination root itself: {raw!r}")

    return "/".join(parts)


def to_native(relpath: str) -> str:
    """Convert a POSIX-form relative path to the running platform's separator."""
    return os.path.join(*relpath.split("/")) if relpath else relpath


def is_within(root: str, path: str) -> bool:
    """Return True when *path* resolves to *root* itself or something inside it.

    Symlinks are resolved first, so a symlink planted inside the destination
    cannot be used to redirect a write outside of it.
    """
    try:
        real_root = os.path.normcase(os.path.realpath(root))
        real_path = os.path.normcase(os.path.realpath(path))
    except (OSError, ValueError):
        return False
    if real_path == real_root:
        return True
    try:
        return os.path.commonpath([real_root, real_path]) == real_root
    except ValueError:
        # Different drives, or a mix of absolute and relative paths.
        return False


def safe_join(root: str, relpath: str) -> str:
    """Join an untrusted relative path onto *root*, refusing to escape it.

    Raises :class:`UnsafePathError` if the result would land outside *root*.
    """
    normalized = normalize_relpath(relpath)
    target = os.path.join(root, to_native(normalized))
    if not is_within(root, target):
        raise UnsafePathError(
            f"path {relpath!r} would resolve outside the destination directory"
        )
    return target


def describe_overlap(source: str, destination: str) -> str | None:
    """Return a warning string when source and destination overlap, else None."""
    try:
        real_src = os.path.normcase(os.path.realpath(source))
        real_dst = os.path.normcase(os.path.realpath(destination))
    except (OSError, ValueError):
        return None
    if real_src == real_dst:
        return "source and destination are the same directory"
    if is_within(real_dst, real_src):
        return "source directory is inside the destination directory"
    if is_within(real_src, real_dst):
        return "destination directory is inside the source directory"
    return None
