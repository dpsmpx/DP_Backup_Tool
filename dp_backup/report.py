"""Human-readable run reports.

The point of a report is that the operator can see, without reading a log,
exactly which files still need a decision. Lists are capped so a run over a
damaged disk produces something a person can actually read.
"""

from __future__ import annotations

import json
import os
from typing import Iterable

from .export import ExportResult
from .matching import (
    MATCH_CONTENT,
    MATCH_EXTENSION,
    MATCH_NAME,
    MATCH_NAME_AMBIGUOUS,
    MATCH_SIZE_GUESS,
    MATCH_UNIQUE_SIZE,
)
from .restore import (
    FAILED,
    MISSING,
    PLANNED,
    REJECTED,
    RESTORED,
    SKIPPED_EXISTS,
    UNRESOLVED,
    Outcome,
    RestoreResult,
)

#: How many individual entries a section lists before summarising the rest.
LIST_LIMIT = 50

#: Confidences worth drawing the operator's attention to.
WEAK_MATCHES = (MATCH_NAME_AMBIGUOUS, MATCH_EXTENSION, MATCH_UNIQUE_SIZE, MATCH_SIZE_GUESS)

_CONFIDENCE_NOTE = {
    MATCH_CONTENT: "content digest matched - certain",
    MATCH_NAME: "name and size matched - strong",
    MATCH_NAME_AMBIGUOUS: "name and size matched, but several candidates shared them",
    MATCH_EXTENSION: "only the extension and size matched - check these",
    MATCH_UNIQUE_SIZE: "only the size matched, but it was unique in the source",
    MATCH_SIZE_GUESS: "guessed by size alone - content is NOT verified",
}


def human_bytes(count: int) -> str:
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024.0 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TiB"


def _section(title: str, lines: Iterable[str], limit: int = LIST_LIMIT) -> list[str]:
    items = list(lines)
    if not items:
        return []
    out = [f"{title} ({len(items)})", "-" * len(f"{title} ({len(items)})")]
    out.extend(f"  {line}" for line in items[:limit])
    if len(items) > limit:
        out.append(f"  ... and {len(items) - limit} more (see the log for the full list)")
    out.append("")
    return out


def _describe(outcome: Outcome) -> str:
    text = outcome.path
    if outcome.detail:
        text += f"  -- {outcome.detail}"
    return text


def format_export_report(result: ExportResult, root_dir: str) -> str:
    counts = result.counts
    lines = [
        "DP Backup Tool - export report",
        "=" * 34,
        f"Scanned      : {root_dir}",
        f"Structure    : {result.output_path}",
        f"Hash mode    : {result.structure.hash_mode}",
        "",
        f"Directories  : {counts['directories']}",
        f"Files        : {counts['files']} ({human_bytes(counts['total_bytes'])})",
        f"Symlinks     : {counts['symlinks']}",
        "",
    ]
    lines += _section("Directories that could not be read", result.errors)
    lines += _section(
        "Files recorded without a digest (unreadable)", result.unreadable
    )
    lines += _section(
        "Skipped, not regular files (sockets, FIFOs, devices)", result.skipped_special
    )
    if result.ok:
        lines.append("No problems encountered.")
    return "\n".join(lines).rstrip() + "\n"


def format_restore_report(
    result: RestoreResult, structure_path: str, source_dir: str, destination_dir: str
) -> str:
    counts = result.counts
    mode = "DRY RUN - nothing was written" if result.dry_run else "restore"
    lines = [
        "DP Backup Tool - restore report",
        "=" * 35,
        f"Mode         : {mode}",
        f"Structure    : {structure_path}",
        f"Source       : {source_dir}",
        f"Destination  : {destination_dir}",
        "",
        f"Restored     : {counts.get(RESTORED, 0)}",
    ]
    if result.dry_run:
        lines.append(f"Would restore: {counts.get(PLANNED, 0)}")
    lines += [
        f"Skipped      : {counts.get(SKIPPED_EXISTS, 0)} (already present)",
        f"Missing      : {counts.get(MISSING, 0)} (no matching file in the source)",
        f"Unresolved   : {counts.get(UNRESOLVED, 0)} (ambiguous - needs a decision)",
        f"Failed       : {counts.get(FAILED, 0)}",
        f"Rejected     : {counts.get(REJECTED, 0)} (unsafe path in the structure file)",
        "",
        f"Directories created : {result.directories_created}",
        f"Symlinks created    : {result.symlinks_created}",
        f"Data copied         : {human_bytes(result.bytes_copied)}",
        f"Source files seen   : {result.source_files_seen}",
        f"Source files unused : {result.source_files_unused}",
        "",
    ]

    if result.aborted:
        lines += ["!! RUN ABORTED: " + result.aborted, ""]

    lines += _section(
        "Ambiguous - decide these by hand",
        (_describe(o) for o in result.by_status(UNRESOLVED)),
    )
    lines += _section(
        "Not found in the source",
        (_describe(o) for o in result.by_status(MISSING)),
    )
    lines += _section(
        "Failed", (_describe(o) for o in result.by_status(FAILED))
    )
    lines += _section(
        "Rejected as unsafe", (_describe(o) for o in result.by_status(REJECTED))
    )
    lines += _section(
        "Already present, left untouched",
        (o.path for o in result.by_status(SKIPPED_EXISTS)),
    )

    weak = [o for o in result.outcomes if o.confidence in WEAK_MATCHES]
    lines += _section(
        "Matched on weaker evidence - worth checking",
        (
            f"{o.path}  <- {os.path.basename(o.source)}  "
            f"[{_CONFIDENCE_NOTE.get(o.confidence, o.confidence)}]"
            for o in weak
        ),
    )

    lines += _section("Problems in the structure file", result.structure_problems)
    lines += _section("Problems reading the source", result.scan_errors)

    if not result.needs_attention:
        lines.append("Everything in the structure file was accounted for.")
    return "\n".join(lines).rstrip() + "\n"


def restore_result_to_json(
    result: RestoreResult, structure_path: str, source_dir: str, destination_dir: str
) -> str:
    payload = {
        "structure": structure_path,
        "source": source_dir,
        "destination": destination_dir,
        "dry_run": result.dry_run,
        "aborted": result.aborted,
        "counts": result.counts,
        "directories_created": result.directories_created,
        "symlinks_created": result.symlinks_created,
        "bytes_copied": result.bytes_copied,
        "source_files_seen": result.source_files_seen,
        "source_files_unused": result.source_files_unused,
        "structure_problems": result.structure_problems,
        "scan_errors": result.scan_errors,
        "entries": [
            {
                "path": o.path,
                "kind": o.kind,
                "status": o.status,
                "source": o.source,
                "confidence": o.confidence,
                "detail": o.detail,
            }
            for o in result.outcomes
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def write_report(text: str, path: str) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
