"""Rebuilding a directory tree from a structure file and a pile of loose files."""

from __future__ import annotations

import errno
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import Callable

from .hashing import HASH_NONE, hash_file
from .matching import (
    AMBIGUOUS,
    MATCH_CONTENT,
    Decision,
    SourceIndex,
)
from .paths import UnsafePathError, describe_overlap, is_within, safe_join
from .structure import Entry, Structure

logger = logging.getLogger("dp_backup.restore")

ProgressCallback = Callable[[int, int, str], None]

# Outcome statuses.
RESTORED = "restored"
PLANNED = "planned"            # dry run only
SKIPPED_EXISTS = "skipped-exists"
MISSING = "missing"
UNRESOLVED = "unresolved"      # ambiguous, left for the operator
FAILED = "failed"
REJECTED = "rejected"          # unsafe path


class RestoreError(Exception):
    """The restore could not start, or had to stop part-way."""


@dataclass
class Outcome:
    """What happened to one entry from the structure file."""

    path: str
    kind: str
    status: str
    source: str = ""
    confidence: str = ""
    detail: str = ""


@dataclass
class RestoreResult:
    outcomes: list[Outcome] = field(default_factory=list)
    directories_created: int = 0
    symlinks_created: int = 0
    bytes_copied: int = 0
    source_files_seen: int = 0
    source_files_unused: int = 0
    scan_errors: list[str] = field(default_factory=list)
    structure_problems: list[str] = field(default_factory=list)
    aborted: str = ""
    dry_run: bool = False

    def by_status(self, status: str) -> list[Outcome]:
        return [o for o in self.outcomes if o.status == status]

    @property
    def counts(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for outcome in self.outcomes:
            tally[outcome.status] = tally.get(outcome.status, 0) + 1
        return tally

    @property
    def needs_attention(self) -> bool:
        return bool(
            self.aborted
            or self.scan_errors
            or self.structure_problems
            or self.by_status(MISSING)
            or self.by_status(UNRESOLVED)
            or self.by_status(FAILED)
            or self.by_status(REJECTED)
        )


def restore_structure(
    structure: Structure,
    source_dir: str,
    destination_dir: str,
    *,
    dry_run: bool = False,
    allow_ambiguous: bool = False,
    overwrite: bool = False,
    restore_symlinks: bool = True,
    restore_mtime: bool = True,
    verify: bool = False,
    progress: ProgressCallback | None = None,
) -> RestoreResult:
    """Recreate *structure* under *destination_dir* using files from *source_dir*."""
    source_dir = os.path.abspath(source_dir)
    destination_dir = os.path.abspath(destination_dir)

    if not os.path.isdir(source_dir):
        raise RestoreError(f"source directory does not exist: {source_dir!r}")
    if not os.access(source_dir, os.R_OK | os.X_OK):
        raise RestoreError(f"no permission to read the source directory: {source_dir!r}")

    if os.path.exists(destination_dir) and not os.path.isdir(destination_dir):
        raise RestoreError(f"destination path is not a directory: {destination_dir!r}")
    if not dry_run:
        try:
            os.makedirs(destination_dir, exist_ok=True)
        except OSError as exc:
            raise RestoreError(
                f"could not create destination directory {destination_dir!r}: {exc}"
            ) from None
        if not os.access(destination_dir, os.W_OK):
            raise RestoreError(
                f"no permission to write to the destination directory: {destination_dir!r}"
            )
    elif not os.path.isdir(destination_dir):
        raise RestoreError(f"destination directory does not exist: {destination_dir!r}")

    overlap = describe_overlap(source_dir, destination_dir)
    if overlap:
        raise RestoreError(
            f"refusing to run: {overlap}. Restore into a separate empty directory "
            "so the source pile is never modified."
        )

    result = RestoreResult(dry_run=dry_run)

    logger.info("Indexing source directory %r", source_dir)
    index = SourceIndex.build(source_dir, structure.hash_mode)
    result.source_files_seen = index.file_count
    result.scan_errors.extend(index.scan_errors)
    logger.info("Indexed %d source file(s)", index.file_count)

    _create_directories(structure, destination_dir, result, dry_run)
    _restore_files(
        structure, index, destination_dir, result,
        dry_run=dry_run, allow_ambiguous=allow_ambiguous, overwrite=overwrite,
        restore_mtime=restore_mtime, verify=verify, progress=progress,
    )
    if restore_symlinks:
        _restore_symlinks(structure, destination_dir, result, dry_run, overwrite)
    if restore_mtime and not dry_run:
        _apply_directory_times(structure, destination_dir)

    result.scan_errors.extend(index.hash_errors)
    result.source_files_unused = sum(1 for _ in index.unused_files())
    return result


# -- directories --------------------------------------------------------


def _create_directories(
    structure: Structure, destination_dir: str, result: RestoreResult, dry_run: bool
) -> None:
    # Sorted so a parent is always created before its children.
    for entry in sorted(structure.directories, key=lambda e: e.path):
        try:
            target = safe_join(destination_dir, entry.path)
        except UnsafePathError as exc:
            _reject(result, entry, exc)
            continue
        if dry_run:
            if not os.path.isdir(target):
                result.directories_created += 1
            continue
        try:
            if os.path.isdir(target):
                continue
            os.makedirs(target, exist_ok=True)
            result.directories_created += 1
        except OSError as exc:
            message = f"could not create directory {target!r}: {exc}"
            logger.error("%s", message)
            result.outcomes.append(
                Outcome(entry.path, entry.kind, FAILED, detail=message)
            )


def _apply_directory_times(structure: Structure, destination_dir: str) -> None:
    """Set directory mtimes last, so writing children does not overwrite them."""
    for entry in sorted(structure.directories, key=lambda e: e.path, reverse=True):
        if entry.mtime is None:
            continue
        try:
            target = safe_join(destination_dir, entry.path)
        except UnsafePathError:
            continue
        try:
            if os.path.isdir(target):
                os.utime(target, (entry.mtime, entry.mtime))
        except OSError as exc:
            logger.warning("could not set mtime on %r: %s", target, exc)


# -- files --------------------------------------------------------------


def _restore_files(
    structure: Structure,
    index: SourceIndex,
    destination_dir: str,
    result: RestoreResult,
    *,
    dry_run: bool,
    allow_ambiguous: bool,
    overwrite: bool,
    restore_mtime: bool,
    verify: bool,
    progress: ProgressCallback | None,
) -> None:
    files = structure.files
    total = len(files)

    for position, entry in enumerate(files, start=1):
        if progress is not None:
            progress(position, total, entry.path)

        try:
            target = safe_join(destination_dir, entry.path)
        except UnsafePathError as exc:
            _reject(result, entry, exc)
            continue

        if os.path.lexists(target) and not overwrite:
            result.outcomes.append(
                Outcome(entry.path, entry.kind, SKIPPED_EXISTS,
                        detail="a file already exists at this path")
            )
            continue

        decision = index.match(
            size=entry.size or 0,
            name=entry.name,
            digest=entry.digest,
            allow_ambiguous=allow_ambiguous,
        )

        if not decision.matched:
            status = UNRESOLVED if decision.reason == AMBIGUOUS else MISSING
            result.outcomes.append(
                Outcome(entry.path, entry.kind, status, detail=decision.detail)
            )
            continue

        assert decision.source is not None
        if dry_run:
            result.outcomes.append(
                Outcome(entry.path, entry.kind, PLANNED,
                        source=decision.source.path,
                        confidence=decision.confidence, detail=decision.detail)
            )
            index.consume(decision.source)
            continue

        error = _copy_file(
            decision, entry, target, destination_dir, index,
            restore_mtime=restore_mtime, verify=verify,
        )
        if error is None:
            index.consume(decision.source)
            result.bytes_copied += entry.size or 0
            result.outcomes.append(
                Outcome(entry.path, entry.kind, RESTORED,
                        source=decision.source.path,
                        confidence=decision.confidence, detail=decision.detail)
            )
        else:
            result.outcomes.append(
                Outcome(entry.path, entry.kind, FAILED,
                        source=decision.source.path, detail=error)
            )
            if _is_out_of_space(error):
                result.aborted = (
                    "the destination filesystem ran out of space; "
                    "the remaining entries were not attempted"
                )
                logger.error("%s", result.aborted)
                return


def _copy_file(
    decision: Decision,
    entry: Entry,
    target: str,
    destination_dir: str,
    index: SourceIndex,
    *,
    restore_mtime: bool,
    verify: bool,
) -> str | None:
    """Copy one file into place atomically. Returns an error string, or None."""
    source_path = decision.source.path if decision.source else ""
    parent = os.path.dirname(target)

    try:
        os.makedirs(parent, exist_ok=True)
    except OSError as exc:
        return f"could not create parent directory {parent!r}: {exc}"

    # Re-check containment after the directories exist: safe_join validated the
    # path before, but the parents were only just created and a symlink could
    # have been substituted in between.
    if not is_within(destination_dir, target):
        return f"{target!r} no longer resolves inside the destination directory"

    handle = None
    temp_path = ""
    try:
        handle = tempfile.NamedTemporaryFile(
            dir=parent, prefix=".dp_restore-", suffix=".part", delete=False
        )
        temp_path = handle.name
        handle.close()
        # copyfile writes content only; metadata is applied deliberately below.
        shutil.copyfile(source_path, temp_path)

        written = os.path.getsize(temp_path)
        if entry.size is not None and written != entry.size:
            raise OSError(
                errno.EIO,
                f"copied {written} bytes but the structure says {entry.size}",
            )

        if verify and entry.digest and index.hash_mode != HASH_NONE:
            actual = hash_file(temp_path, index.hash_mode, size=written)
            if actual != entry.digest:
                raise OSError(errno.EIO, "content check failed after copying")

        # Take permissions and times from the source file, then let the
        # structure's own mtime win where it recorded one.
        shutil.copystat(source_path, temp_path)
        if restore_mtime and entry.mtime is not None:
            os.utime(temp_path, (entry.mtime, entry.mtime))

        os.replace(temp_path, target)
        temp_path = ""
        return None
    except (OSError, shutil.Error) as exc:
        return f"could not copy {source_path!r} to {target!r}: {exc}"
    except BaseException:
        # KeyboardInterrupt and the like unwind after the finally block below
        # has removed the partial file, so no ".part" scratch is left behind.
        raise
    finally:
        if handle is not None and not handle.closed:
            handle.close()
        _discard(temp_path)


def _discard(temp_path: str) -> None:
    if not temp_path:
        return
    try:
        os.unlink(temp_path)
    except OSError:
        pass


def _is_out_of_space(message: str) -> bool:
    return "No space left on device" in message or "errno 28" in message.lower()


# -- symlinks -----------------------------------------------------------


def _restore_symlinks(
    structure: Structure,
    destination_dir: str,
    result: RestoreResult,
    dry_run: bool,
    overwrite: bool,
) -> None:
    for entry in structure.symlinks:
        try:
            target = safe_join(destination_dir, entry.path)
        except UnsafePathError as exc:
            _reject(result, entry, exc)
            continue

        if os.path.lexists(target):
            if not overwrite:
                result.outcomes.append(
                    Outcome(entry.path, entry.kind, SKIPPED_EXISTS,
                            detail="something already exists at this path")
                )
                continue
            if not dry_run:
                try:
                    os.unlink(target)
                except OSError as exc:
                    result.outcomes.append(
                        Outcome(entry.path, entry.kind, FAILED,
                                detail=f"could not replace existing entry: {exc}")
                    )
                    continue

        if dry_run:
            result.symlinks_created += 1
            result.outcomes.append(
                Outcome(entry.path, entry.kind, PLANNED, detail=f"-> {entry.target}")
            )
            continue

        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            os.symlink(entry.target or "", target)
            result.symlinks_created += 1
            result.outcomes.append(
                Outcome(entry.path, entry.kind, RESTORED, detail=f"-> {entry.target}")
            )
        except (OSError, NotImplementedError, ValueError) as exc:
            # Windows without the privilege, or a filesystem without symlinks.
            message = f"could not create symlink {target!r} -> {entry.target!r}: {exc}"
            logger.warning("%s", message)
            result.outcomes.append(
                Outcome(entry.path, entry.kind, FAILED, detail=message)
            )


def _reject(result: RestoreResult, entry: Entry, exc: Exception) -> None:
    message = str(exc)
    logger.error("Refused unsafe path %r: %s", entry.path, message)
    result.outcomes.append(Outcome(entry.path, entry.kind, REJECTED, detail=message))
