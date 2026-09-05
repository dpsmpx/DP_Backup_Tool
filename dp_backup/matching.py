"""Deciding which source file belongs at which place in the restored tree.

Matching runs in tiers, strongest evidence first:

1. ``content``   - the content digest recorded at export equals the digest of a
                   source file. Unambiguous, and the only tier that lets one
                   source file be copied to several destinations (the original
                   tree genuinely held duplicates).
2. ``name``      - same size *and* same file name. Used when no digest is
                   available (a 1.x structure file, or ``--hash none``).
3. ``extension`` - same size and the same, uniquely matching, extension.
4. ``unique-size``- same size, and that size is unique in the whole source.

Anything weaker is reported as ambiguous and left for the operator instead of
being guessed at, which is what DP Backup Tool 1.x used to do silently.
"""

from __future__ import annotations

import logging
import os
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Iterable

from .hashing import HASH_NONE, hash_file

logger = logging.getLogger("dp_backup.matching")

# Match confidences, strongest first.
MATCH_CONTENT = "content"
MATCH_NAME = "name"
MATCH_NAME_AMBIGUOUS = "name-ambiguous"
MATCH_EXTENSION = "extension"
MATCH_UNIQUE_SIZE = "unique-size"
MATCH_SIZE_GUESS = "size-guess"

# Reasons a target could not be matched.
NO_SIZE_MATCH = "no-size-match"
NO_CONTENT_MATCH = "no-content-match"
AMBIGUOUS = "ambiguous"


@dataclass
class SourceFile:
    """One candidate file found in the source directory."""

    path: str
    size: int
    name: str
    extension: str
    used: bool = False
    digest: str | None = None       # cached; "" means "could not be hashed"

    @property
    def basename_lower(self) -> str:
        return self.name.lower()


@dataclass
class Decision:
    """The outcome of matching one structure entry against the source."""

    source: SourceFile | None
    confidence: str = ""
    reason: str = ""
    detail: str = ""

    @property
    def matched(self) -> bool:
        return self.source is not None


class _Bucket:
    """All source files sharing one size, with indexes for each match tier.

    Consumed files are tombstoned rather than removed, and the queues are
    pruned lazily from the head, which keeps the whole restore linear in the
    number of source files instead of the quadratic scan version 1.x did.
    """

    __slots__ = (
        "files", "by_name", "by_ext", "queue",
        "live_by_name", "live_by_ext", "live_total", "by_digest",
    )

    def __init__(self, files: list[SourceFile]) -> None:
        self.files = files
        self.by_name: dict[str, deque[SourceFile]] = {}
        self.by_ext: dict[str, deque[SourceFile]] = {}
        self.queue: deque[SourceFile] = deque(files)
        self.live_by_name: Counter[str] = Counter()
        self.live_by_ext: Counter[str] = Counter()
        self.live_total = len(files)
        self.by_digest: dict[str, list[SourceFile]] | None = None
        for item in files:
            self.by_name.setdefault(item.basename_lower, deque()).append(item)
            self.by_ext.setdefault(item.extension, deque()).append(item)
            self.live_by_name[item.basename_lower] += 1
            self.live_by_ext[item.extension] += 1

    def consume(self, item: SourceFile) -> None:
        if item.used:
            return
        item.used = True
        self.live_by_name[item.basename_lower] -= 1
        self.live_by_ext[item.extension] -= 1
        self.live_total -= 1

    @staticmethod
    def _head(queue: deque[SourceFile] | None) -> SourceFile | None:
        if queue is None:
            return None
        while queue and queue[0].used:
            queue.popleft()
        return queue[0] if queue else None

    def take_by_name(self, name: str) -> SourceFile | None:
        return self._head(self.by_name.get(name.lower()))

    def take_by_extension(self, extension: str) -> SourceFile | None:
        return self._head(self.by_ext.get(extension))

    def take_any(self) -> SourceFile | None:
        return self._head(self.queue)

    def live_named(self, name: str) -> int:
        return self.live_by_name.get(name.lower(), 0)

    def live_with_extension(self, extension: str) -> int:
        return self.live_by_ext.get(extension, 0)


class SourceIndex:
    """Every regular file under the source directory, indexed by size."""

    def __init__(self, hash_mode: str = HASH_NONE) -> None:
        self.hash_mode = hash_mode
        self._buckets: dict[int, _Bucket] = {}
        self.scan_errors: list[str] = []
        self.hash_errors: list[str] = []
        self.file_count = 0
        self.total_bytes = 0

    # -- building -------------------------------------------------------

    @classmethod
    def build(
        cls, source_dir: str, hash_mode: str = HASH_NONE, *, follow_symlinks: bool = False
    ) -> "SourceIndex":
        index = cls(hash_mode)
        by_size: dict[int, list[SourceFile]] = {}

        def on_error(exc: OSError) -> None:
            message = f"could not list source directory {getattr(exc, 'filename', '?')!r}: {exc}"
            logger.warning("%s", message)
            index.scan_errors.append(message)

        for dirpath, dirnames, filenames in os.walk(
            source_dir, onerror=on_error, followlinks=False
        ):
            dirnames.sort()
            filenames.sort()
            for filename in filenames:
                full_path = os.path.join(dirpath, filename)
                if os.path.islink(full_path) and not follow_symlinks:
                    continue
                try:
                    info = os.stat(full_path)
                except OSError as exc:
                    index.scan_errors.append(f"could not stat {full_path!r}: {exc}")
                    continue
                if not os.path.isfile(full_path):
                    continue
                by_size.setdefault(info.st_size, []).append(
                    SourceFile(
                        path=full_path,
                        size=info.st_size,
                        name=filename,
                        extension=_extension_of(filename),
                    )
                )
                index.file_count += 1
                index.total_bytes += info.st_size

        index._buckets = {size: _Bucket(files) for size, files in by_size.items()}
        return index

    # -- hashing --------------------------------------------------------

    def digest_of(self, item: SourceFile) -> str:
        """Return the cached digest of *item*, computing it on first use."""
        if item.digest is None:
            try:
                item.digest = hash_file(item.path, self.hash_mode, size=item.size)
            except OSError as exc:
                message = f"could not hash source file {item.path!r}: {exc}"
                logger.warning("%s", message)
                self.hash_errors.append(message)
                item.digest = ""
        return item.digest

    def _digest_index(self, bucket: _Bucket) -> dict[str, list[SourceFile]]:
        """Hash every file in *bucket* once, then index it by digest."""
        if bucket.by_digest is None:
            mapping: dict[str, list[SourceFile]] = {}
            for item in bucket.files:
                digest = self.digest_of(item)
                if digest:
                    mapping.setdefault(digest, []).append(item)
            bucket.by_digest = mapping
        return bucket.by_digest

    # -- matching -------------------------------------------------------

    def match(
        self,
        *,
        size: int,
        name: str,
        digest: str = "",
        allow_ambiguous: bool = False,
    ) -> Decision:
        """Find the best source file for one structure entry."""
        bucket = self._buckets.get(size)
        if bucket is None:
            return Decision(None, reason=NO_SIZE_MATCH,
                            detail=f"no source file is {size} bytes")

        if digest and self.hash_mode != HASH_NONE:
            return self._match_by_digest(bucket, digest, name, size)

        return self._match_without_digest(bucket, name, size, allow_ambiguous)

    def _match_by_digest(
        self, bucket: _Bucket, digest: str, name: str, size: int
    ) -> Decision:
        candidates = self._digest_index(bucket).get(digest)
        if not candidates:
            return Decision(
                None, reason=NO_CONTENT_MATCH,
                detail=(
                    f"{len(bucket.files)} source file(s) are {size} bytes but none "
                    "has the recorded content"
                ),
            )
        # Prefer an unused copy with the right name, then any unused copy, then
        # reuse an already-copied one: identical content means the original tree
        # held duplicates, and every one of them deserves restoring.
        wanted = name.lower()
        for item in candidates:
            if not item.used and item.basename_lower == wanted:
                return Decision(item, confidence=MATCH_CONTENT)
        for item in candidates:
            if not item.used:
                return Decision(item, confidence=MATCH_CONTENT)
        return Decision(
            candidates[0], confidence=MATCH_CONTENT,
            detail="source file reused for a duplicate of the same content",
        )

    def _match_without_digest(
        self, bucket: _Bucket, name: str, size: int, allow_ambiguous: bool
    ) -> Decision:
        named = bucket.live_named(name)
        if named == 1:
            item = bucket.take_by_name(name)
            if item is not None:
                return Decision(item, confidence=MATCH_NAME)
        elif named > 1:
            item = bucket.take_by_name(name)
            if item is not None:
                return Decision(
                    item, confidence=MATCH_NAME_AMBIGUOUS,
                    detail=f"{named} unused source files share this name and size",
                )

        extension = _extension_of(name)
        if extension and bucket.live_with_extension(extension) == 1:
            item = bucket.take_by_extension(extension)
            if item is not None:
                return Decision(item, confidence=MATCH_EXTENSION)

        if len(bucket.files) == 1:
            item = bucket.take_any()
            if item is not None:
                return Decision(item, confidence=MATCH_UNIQUE_SIZE)
            return Decision(
                None, reason=AMBIGUOUS,
                detail="the only source file of this size was already restored elsewhere",
            )

        if bucket.live_total == 0:
            return Decision(
                None, reason=AMBIGUOUS,
                detail=(
                    f"all {len(bucket.files)} source file(s) of {size} bytes have "
                    "already been used"
                ),
            )

        if allow_ambiguous:
            item = bucket.take_any()
            if item is not None:
                return Decision(
                    item, confidence=MATCH_SIZE_GUESS,
                    detail=(
                        f"guessed among {bucket.live_total} unused source file(s) of "
                        f"{size} bytes; content is not verified"
                    ),
                )

        return Decision(
            None, reason=AMBIGUOUS,
            detail=(
                f"{bucket.live_total} source file(s) are {size} bytes and none "
                "matches by name or extension; needs a manual decision"
            ),
        )

    def consume(self, item: SourceFile) -> None:
        bucket = self._buckets.get(item.size)
        if bucket is not None:
            bucket.consume(item)

    def unused_files(self) -> Iterable[SourceFile]:
        for bucket in self._buckets.values():
            for item in bucket.files:
                if not item.used:
                    yield item


def _extension_of(filename: str) -> str:
    dot = filename.rfind(".")
    return filename[dot:].lower() if dot > 0 else ""
