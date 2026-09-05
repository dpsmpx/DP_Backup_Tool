"""Scanning a directory tree into a structure file."""

from __future__ import annotations

import logging
import os
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .hashing import HASH_FULL, HASH_MODES, HASH_NONE, hash_file
from .structure import (
    KIND_DIRECTORY,
    KIND_FILE,
    KIND_SYMLINK,
    Entry,
    Structure,
    save_structure,
)

logger = logging.getLogger("dp_backup.export")

ProgressCallback = Callable[[int, str], None]


class ExportError(Exception):
    """The export could not start or its result could not be written."""


@dataclass
class ExportResult:
    structure: Structure
    errors: list[str] = field(default_factory=list)
    skipped_special: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)
    output_path: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors and not self.skipped_special and not self.unreadable

    @property
    def counts(self) -> dict[str, int]:
        return {
            "directories": len(self.structure.directories),
            "files": len(self.structure.files),
            "symlinks": len(self.structure.symlinks),
            "total_bytes": self.structure.total_bytes,
            "errors": len(self.errors),
            "skipped_special": len(self.skipped_special),
            "unreadable": len(self.unreadable),
        }


def export_structure(
    root_dir: str,
    output_path: str,
    *,
    hash_mode: str = HASH_FULL,
    follow_symlinks: bool = False,
    progress: ProgressCallback | None = None,
) -> ExportResult:
    """Scan *root_dir* and write its structure to *output_path*.

    Raises :class:`ExportError` only for conditions that make the whole run
    pointless (bad arguments, unusable root, unwritable output). Per-file
    trouble is collected in the result and never aborts the scan.
    """
    if hash_mode not in HASH_MODES:
        raise ExportError(
            f"unknown hash mode {hash_mode!r}; expected one of {', '.join(HASH_MODES)}"
        )
    if not root_dir:
        raise ExportError("no source directory given")
    if not output_path:
        raise ExportError("no output file given")

    root_dir = os.path.abspath(root_dir)
    output_path = os.path.abspath(output_path)

    if not os.path.exists(root_dir):
        raise ExportError(f"source directory does not exist: {root_dir!r}")
    if not os.path.isdir(root_dir):
        raise ExportError(f"source path is not a directory: {root_dir!r}")
    if not os.access(root_dir, os.R_OK | os.X_OK):
        raise ExportError(f"no permission to read the source directory: {root_dir!r}")
    if os.path.isdir(output_path):
        raise ExportError(f"output path is a directory: {output_path!r}")

    structure = Structure(
        hash_mode=hash_mode,
        source_root=root_dir,
        created_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    )
    result = ExportResult(structure=structure, output_path=output_path)

    def on_walk_error(exc: OSError) -> None:
        # os.walk swallows directory errors unless this is supplied.
        message = f"could not list directory {getattr(exc, 'filename', '?')!r}: {exc}"
        logger.warning("%s", message)
        result.errors.append(message)

    scanned = 0
    for dirpath, dirnames, filenames in os.walk(
        root_dir, onerror=on_walk_error, followlinks=False
    ):
        # Sorted in place so the walk order, and therefore the output file, is
        # reproducible across runs and platforms.
        dirnames.sort()
        filenames.sort()

        for dirname in list(dirnames):
            full_path = os.path.join(dirpath, dirname)
            rel_path = _relative(full_path, root_dir)
            if os.path.islink(full_path):
                # Recorded as a symlink, not descended into: this is what keeps
                # a symlink loop from turning the scan into an infinite walk.
                dirnames.remove(dirname)
                entry = _symlink_entry(full_path, rel_path, result)
                if entry is not None:
                    structure.entries.append(entry)
                continue
            entry = Entry(
                path=rel_path, kind=KIND_DIRECTORY, mtime=_mtime(full_path, result)
            )
            structure.entries.append(entry)

        for filename in filenames:
            full_path = os.path.join(dirpath, filename)
            if os.path.normcase(full_path) == os.path.normcase(output_path):
                # Never record the structure file we are about to write.
                continue
            rel_path = _relative(full_path, root_dir)
            entry = _file_entry(
                full_path, rel_path, hash_mode, follow_symlinks, result
            )
            if entry is not None:
                structure.entries.append(entry)
            scanned += 1
            if progress is not None and scanned % 200 == 0:
                progress(scanned, rel_path)

    try:
        save_structure(structure, output_path)
    except OSError as exc:
        raise ExportError(f"could not write structure file {output_path!r}: {exc}") from None

    logger.info(
        "Exported %d directories, %d files, %d symlinks from %r",
        len(structure.directories), len(structure.files),
        len(structure.symlinks), root_dir,
    )
    return result


def _relative(full_path: str, root_dir: str) -> str:
    return os.path.relpath(full_path, root_dir).replace(os.sep, "/")


def _mtime(path: str, result: ExportResult) -> float | None:
    try:
        return os.lstat(path).st_mtime
    except OSError as exc:
        result.errors.append(f"could not stat {path!r}: {exc}")
        return None


def _symlink_entry(full_path: str, rel_path: str, result: ExportResult) -> Entry | None:
    try:
        target = os.readlink(full_path)
        mtime = os.lstat(full_path).st_mtime
    except OSError as exc:
        message = f"could not read symlink {full_path!r}: {exc}"
        logger.warning("%s", message)
        result.errors.append(message)
        return None
    return Entry(path=rel_path, kind=KIND_SYMLINK, target=target, mtime=mtime)


def _file_entry(
    full_path: str,
    rel_path: str,
    hash_mode: str,
    follow_symlinks: bool,
    result: ExportResult,
) -> Entry | None:
    is_link = os.path.islink(full_path)
    if is_link and not follow_symlinks:
        return _symlink_entry(full_path, rel_path, result)

    try:
        info = os.stat(full_path) if follow_symlinks else os.lstat(full_path)
    except OSError as exc:
        # Covers a dangling symlink under --follow-symlinks, a file deleted
        # mid-scan, and permission problems on the containing directory.
        message = f"could not stat {full_path!r}: {exc}"
        logger.warning("%s", message)
        result.errors.append(message)
        return None

    if not stat.S_ISREG(info.st_mode):
        # FIFOs, sockets and device nodes carry no content worth restoring.
        message = f"skipped non-regular file {full_path!r}"
        logger.warning("%s", message)
        result.skipped_special.append(full_path)
        return None

    entry = Entry(
        path=rel_path, kind=KIND_FILE, size=info.st_size, mtime=info.st_mtime
    )

    if hash_mode != HASH_NONE:
        try:
            entry.digest = hash_file(full_path, hash_mode, size=info.st_size)
        except OSError as exc:
            # Keep the entry: size and name still allow a weaker match later.
            message = f"could not hash {full_path!r} ({exc}); recorded without a digest"
            logger.warning("%s", message)
            result.unreadable.append(full_path)

    return entry
