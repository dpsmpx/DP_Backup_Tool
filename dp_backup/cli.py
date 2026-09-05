"""Command-line interface.

Exit codes
    0   finished, nothing needs attention
    1   finished, but something needs a look (missing, ambiguous or failed)
    2   could not run (bad arguments, unusable paths, unreadable structure file)
    130 interrupted with Ctrl-C
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from . import __version__
from .export import ExportError, export_structure
from .hashing import HASH_FULL, HASH_MODES
from .logsetup import configure_logging
from .report import (
    format_export_report,
    format_restore_report,
    restore_result_to_json,
    write_report,
)
from .restore import RestoreError, restore_structure
from .structure import StructureError, load_structure

logger = logging.getLogger("dp_backup.cli")

#: Progress is only drawn on a real terminal, so piped or redirected output
#: stays clean and machine-readable.
def _interactive(quiet: bool) -> bool:
    return not quiet and sys.stdout.isatty()


def _clear_progress(active: bool) -> None:
    if active:
        print("\r\033[K", end="", flush=True)

EXIT_OK = 0
EXIT_ATTENTION = 1
EXIT_ERROR = 2
EXIT_INTERRUPTED = 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dp-backup",
        description=(
            "Record the structure of a directory tree, and later put loose "
            "recovered files back where they belong."
        ),
    )
    parser.add_argument("--version", action="version", version=f"DP Backup Tool {__version__}")
    parser.add_argument("--log", metavar="PATH", help="write the log here instead of the default location")
    parser.add_argument("-v", "--verbose", action="store_true", help="log debug detail")
    parser.add_argument("-q", "--quiet", action="store_true", help="only print the final summary")

    sub = parser.add_subparsers(dest="command")

    export = sub.add_parser("export", help="scan a directory tree into a structure file")
    export.add_argument("source", help="directory to scan")
    export.add_argument("output", help="structure file to write (.json)")
    export.add_argument(
        "--hash", dest="hash_mode", choices=HASH_MODES, default=HASH_FULL,
        help=(
            "content fingerprint to record: 'full' (default, exact), "
            "'partial' (first and last 64 KiB - faster on big media), "
            "'none' (name and size only, like version 1.x)"
        ),
    )
    export.add_argument(
        "--follow-symlinks", action="store_true",
        help="record what symlinks point at as ordinary files instead of as links",
    )
    export.add_argument("--report", metavar="PATH", help="also write the report to this file")

    restore = sub.add_parser("restore", help="rebuild a tree from a structure file")
    restore.add_argument("structure", help="structure file written by 'export'")
    restore.add_argument("source", help="directory holding the loose recovered files")
    restore.add_argument("destination", help="directory to rebuild the tree in")
    restore.add_argument(
        "-n", "--dry-run", action="store_true",
        help="report what would happen without writing anything",
    )
    restore.add_argument(
        "--overwrite", action="store_true",
        help="replace files that already exist in the destination",
    )
    restore.add_argument(
        "--allow-ambiguous", action="store_true",
        help=(
            "when several source files share a size and nothing else matches, "
            "copy one anyway (this is what version 1.x always did, and it can "
            "put the wrong file in place)"
        ),
    )
    restore.add_argument("--no-symlinks", action="store_true", help="do not recreate symlinks")
    restore.add_argument("--no-mtime", action="store_true", help="do not restore modification times")
    restore.add_argument(
        "--verify", action="store_true",
        help="re-read each copied file and check its digest",
    )
    restore.add_argument(
        "--skip-invalid", action="store_true",
        help="carry on when the structure file contains unusable entries",
    )
    restore.add_argument("--report", metavar="PATH", help="also write the report to this file (.json for JSON)")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return EXIT_ERROR

    log_path = configure_logging(args.log, verbose=args.verbose, quiet=args.quiet)
    if log_path and not args.quiet:
        print(f"Log: {log_path}")

    try:
        if args.command == "export":
            return _run_export(args)
        return _run_restore(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        logger.warning("Interrupted by the user")
        return EXIT_INTERRUPTED


def _run_export(args: argparse.Namespace) -> int:
    show_progress = _interactive(args.quiet)

    def progress(count: int, path: str) -> None:
        if show_progress:
            print(f"  scanned {count} files...", end="\r", flush=True)

    try:
        result = export_structure(
            args.source, args.output,
            hash_mode=args.hash_mode,
            follow_symlinks=args.follow_symlinks,
            progress=progress,
        )
    except ExportError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        logger.error("Export failed: %s", exc)
        return EXIT_ERROR

    _clear_progress(show_progress)
    text = format_export_report(result, os.path.abspath(args.source))
    if not args.quiet:
        print(text)
    if args.report:
        try:
            write_report(text, args.report)
        except OSError as exc:
            print(f"Warning: could not write the report: {exc}", file=sys.stderr)
    return EXIT_OK if result.ok else EXIT_ATTENTION


def _run_restore(args: argparse.Namespace) -> int:
    try:
        structure, problems = load_structure(args.structure)
    except StructureError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        logger.error("Could not load the structure file: %s", exc)
        return EXIT_ERROR

    if problems and not args.skip_invalid:
        print(
            f"Error: the structure file has {len(problems)} unusable entr"
            f"{'y' if len(problems) == 1 else 'ies'}:",
            file=sys.stderr,
        )
        for problem in problems[:20]:
            print(f"  {problem}", file=sys.stderr)
        if len(problems) > 20:
            print(f"  ... and {len(problems) - 20} more", file=sys.stderr)
        print(
            "\nRe-run with --skip-invalid to restore the entries that are usable.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    if not structure.entries:
        print("The structure file describes no files or directories; nothing to do.")
        return EXIT_OK

    show_progress = _interactive(args.quiet)

    def progress(position: int, total: int, path: str) -> None:
        # Throttled: redrawing per file dominates the runtime on large trees.
        if show_progress and (position % 50 == 0 or position == total):
            print(f"  {position}/{total} files...", end="\r", flush=True)

    try:
        result = restore_structure(
            structure, args.source, args.destination,
            dry_run=args.dry_run,
            allow_ambiguous=args.allow_ambiguous,
            overwrite=args.overwrite,
            restore_symlinks=not args.no_symlinks,
            restore_mtime=not args.no_mtime,
            verify=args.verify,
            progress=progress,
        )
    except RestoreError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        logger.error("Restore failed: %s", exc)
        return EXIT_ERROR

    result.structure_problems = [str(p) for p in problems]

    structure_path = os.path.abspath(args.structure)
    source_dir = os.path.abspath(args.source)
    destination_dir = os.path.abspath(args.destination)
    _clear_progress(show_progress)
    text = format_restore_report(result, structure_path, source_dir, destination_dir)
    print(text)

    if args.report:
        try:
            if args.report.lower().endswith(".json"):
                payload = restore_result_to_json(
                    result, structure_path, source_dir, destination_dir
                )
                write_report(payload, args.report)
            else:
                write_report(text, args.report)
        except OSError as exc:
            print(f"Warning: could not write the report: {exc}", file=sys.stderr)

    return EXIT_ATTENTION if result.needs_attention else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
