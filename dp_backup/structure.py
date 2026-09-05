"""The structure file: its in-memory model, reader and writer.

A structure file is untrusted input. :func:`load_structure` never raises on bad
*content*: it returns the entries it could understand plus a list of
:class:`Problem` records describing the rest, so the caller can decide whether
to continue. It only raises :class:`StructureError` when the file as a whole
cannot be read (missing, unreadable, not JSON, wrong top-level shape).
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

from . import __version__
from .hashing import HASH_MODES, HASH_NONE
from .paths import UnsafePathError, normalize_relpath

FORMAT_NAME = "dp_backup_structure"
FORMAT_VERSION = 2

KIND_DIRECTORY = "directory"
KIND_FILE = "file"
KIND_SYMLINK = "symlink"
KINDS = (KIND_DIRECTORY, KIND_FILE, KIND_SYMLINK)

#: Cap on how many problems are collected before the loader stops recording
#: detail. A badly corrupted file must not produce a million-line report.
MAX_RECORDED_PROBLEMS = 200


class StructureError(Exception):
    """The structure file could not be read at all."""


@dataclass(frozen=True)
class Problem:
    """One rejected entry, identified by its position in the file."""

    index: int
    message: str
    raw_path: str = ""

    def __str__(self) -> str:
        where = f"entry #{self.index}"
        if self.raw_path:
            where += f" ({self.raw_path!r})"
        return f"{where}: {self.message}"


@dataclass
class Entry:
    """One directory, file or symlink recorded in the structure."""

    path: str                      # POSIX-style, relative, already validated
    kind: str
    size: int | None = None        # files only
    mtime: float | None = None
    digest: str = ""               # "" when hashing was disabled
    target: str | None = None      # symlinks only

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def extension(self) -> str:
        base = self.name
        dot = base.rfind(".")
        return base[dot:].lower() if dot > 0 else ""

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"path": self.path, "kind": self.kind}
        if self.kind == KIND_FILE:
            data["size"] = self.size
            if self.digest:
                data["digest"] = self.digest
        if self.kind == KIND_SYMLINK:
            data["target"] = self.target
        if self.mtime is not None:
            data["mtime"] = round(self.mtime, 6)
        return data


@dataclass
class Structure:
    """A loaded structure file."""

    entries: list[Entry] = field(default_factory=list)
    hash_mode: str = HASH_NONE
    source_root: str = ""
    created_at: str = ""
    version: int = FORMAT_VERSION

    def of_kind(self, kind: str) -> Iterator[Entry]:
        return (entry for entry in self.entries if entry.kind == kind)

    @property
    def directories(self) -> list[Entry]:
        return list(self.of_kind(KIND_DIRECTORY))

    @property
    def files(self) -> list[Entry]:
        return list(self.of_kind(KIND_FILE))

    @property
    def symlinks(self) -> list[Entry]:
        return list(self.of_kind(KIND_SYMLINK))

    @property
    def total_bytes(self) -> int:
        return sum(e.size or 0 for e in self.files)


def save_structure(structure: Structure, output_path: str) -> None:
    """Write *structure* to *output_path* atomically.

    The JSON is written to a temporary file in the same directory and then
    renamed, so an interrupted or failing export cannot leave a half-written
    structure file where a complete one used to be.
    """
    payload = {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "generator": f"DP Backup Tool {__version__}",
        "created_at": structure.created_at
        or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source_root": structure.source_root,
        "hash_mode": structure.hash_mode,
        "counts": {
            "directories": len(structure.directories),
            "files": len(structure.files),
            "symlinks": len(structure.symlinks),
            "total_bytes": structure.total_bytes,
        },
        "items": [entry.to_json() for entry in structure.entries],
    }

    directory = os.path.dirname(os.path.abspath(output_path)) or "."
    os.makedirs(directory, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=directory,
        prefix=".dp_backup-", suffix=".tmp", delete=False,
    )
    temp_path = handle.name
    try:
        with handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, output_path)
    except BaseException:
        # Includes KeyboardInterrupt: never leave the scratch file behind.
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def load_structure(path: str) -> tuple[Structure, list[Problem]]:
    """Read a structure file, returning the usable entries and the rejects.

    Accepts both the version 2 object format and the version 1 bare-list format
    written by DP Backup Tool 1.x.
    """
    raw = _read_json(path)

    if isinstance(raw, list):
        return _load_v1(raw)
    if isinstance(raw, dict):
        return _load_v2(raw, path)
    raise StructureError(
        f"unexpected JSON in {path!r}: expected an object or a list, "
        f"got {type(raw).__name__}"
    )


def _read_json(path: str) -> Any:
    if os.path.isdir(path):
        raise StructureError(f"{path!r} is a directory, not a structure file")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise StructureError(f"structure file not found: {path!r}") from None
    except IsADirectoryError:
        raise StructureError(f"{path!r} is a directory, not a structure file") from None
    except PermissionError:
        raise StructureError(f"no permission to read the structure file: {path!r}") from None
    except UnicodeDecodeError as exc:
        raise StructureError(
            f"structure file {path!r} is not valid UTF-8 text: {exc}"
        ) from None
    except json.JSONDecodeError as exc:
        raise StructureError(
            f"structure file {path!r} is not valid JSON (line {exc.lineno}, "
            f"column {exc.colno}): {exc.msg}"
        ) from None
    except OSError as exc:
        raise StructureError(f"could not read structure file {path!r}: {exc}") from None


def _load_v2(raw: dict[str, Any], path: str) -> tuple[Structure, list[Problem]]:
    fmt = raw.get("format")
    if fmt is not None and fmt != FORMAT_NAME:
        raise StructureError(
            f"{path!r} is not a DP Backup structure file (format={fmt!r})"
        )

    version = raw.get("version", FORMAT_VERSION)
    if not isinstance(version, int):
        raise StructureError(f"{path!r} has a non-numeric version: {version!r}")
    if version > FORMAT_VERSION:
        raise StructureError(
            f"{path!r} was written by a newer version of the tool "
            f"(format version {version}, this build understands {FORMAT_VERSION}). "
            "Upgrade DP Backup Tool to read it."
        )

    items = raw.get("items")
    if not isinstance(items, list):
        raise StructureError(f"{path!r} has no usable 'items' list")

    hash_mode = raw.get("hash_mode", HASH_NONE)
    if hash_mode not in HASH_MODES:
        hash_mode = HASH_NONE

    structure = Structure(
        hash_mode=hash_mode,
        source_root=str(raw.get("source_root", "")),
        created_at=str(raw.get("created_at", "")),
        version=version,
    )
    problems = _parse_items(items, structure, legacy=False)
    return structure, problems


def _load_v1(items: list[Any]) -> tuple[Structure, list[Problem]]:
    structure = Structure(hash_mode=HASH_NONE, version=1)
    problems = _parse_items(items, structure, legacy=True)
    return structure, problems


def _parse_items(
    items: list[Any], structure: Structure, *, legacy: bool
) -> list[Problem]:
    problems: list[Problem] = []
    seen: set[str] = set()
    suppressed = 0

    def record(index: int, message: str, raw_path: str = "") -> None:
        nonlocal suppressed
        if len(problems) < MAX_RECORDED_PROBLEMS:
            problems.append(Problem(index, message, raw_path))
        else:
            suppressed += 1

    for index, item in enumerate(items):
        if not isinstance(item, dict):
            record(index, f"expected an object, got {type(item).__name__}")
            continue

        raw_path = item.get("path") if not legacy else item.get("original_relative_path")
        if raw_path is None:
            raw_path = item.get("path", item.get("original_relative_path"))
        raw_label = raw_path if isinstance(raw_path, str) else repr(raw_path)

        try:
            rel_path = normalize_relpath(raw_path)
        except UnsafePathError as exc:
            record(index, str(exc), raw_label or "")
            continue

        if rel_path in seen:
            record(index, "duplicate path in structure file", rel_path)
            continue

        kind = item.get("kind") if not legacy else item.get("type")
        if kind is None:
            kind = item.get("kind", item.get("type"))
        if kind not in KINDS:
            record(index, f"unknown entry kind {kind!r}", rel_path)
            continue

        entry = Entry(path=rel_path, kind=kind)

        if kind == KIND_FILE:
            size = item.get("size") if not legacy else item.get("size_bytes")
            if size is None:
                size = item.get("size", item.get("size_bytes"))
            if isinstance(size, bool) or not isinstance(size, int):
                record(index, f"file size must be an integer, got {size!r}", rel_path)
                continue
            if size < 0:
                record(index, f"file size must not be negative, got {size}", rel_path)
                continue
            entry.size = size

            digest = item.get("digest", "")
            if digest and not isinstance(digest, str):
                record(index, f"digest must be a string, got {type(digest).__name__}", rel_path)
                continue
            entry.digest = digest or ""

        elif kind == KIND_SYMLINK:
            target = item.get("target")
            if not isinstance(target, str) or not target:
                record(index, "symlink entry has no usable 'target'", rel_path)
                continue
            entry.target = target

        mtime = item.get("mtime")
        if mtime is not None:
            if isinstance(mtime, bool) or not isinstance(mtime, (int, float)):
                record(index, f"mtime must be a number, got {mtime!r}", rel_path)
                continue
            entry.mtime = float(mtime)

        seen.add(rel_path)
        structure.entries.append(entry)

    if suppressed:
        problems.append(
            Problem(-1, f"{suppressed} further problem(s) not listed", "")
        )
    return problems
