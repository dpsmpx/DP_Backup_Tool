"""Logging setup.

Logging is configured by the *application* (CLI or GUI), never on import, so
that importing the library can never create a file or fail. The log rotates,
which is what keeps a pathological run from filling the disk the way version
1.x could.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import tempfile

LOG_FILENAME = "backup_tool.log"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def default_log_directory() -> str:
    """Return the per-user location for the log, by platform convention."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "DP_Backup_Tool")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Logs/DP_Backup_Tool")
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "dp_backup_tool")


def configure_logging(
    log_path: str | None = None, *, verbose: bool = False, quiet: bool = False
) -> str:
    """Attach handlers to the ``dp_backup`` logger and return the log path used.

    Falls back to the temporary directory, and then to console-only logging, if
    the preferred location is not writable. Never raises: a tool that cannot
    write its log must still be able to restore files.
    """
    logger = logging.getLogger("dp_backup")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.propagate = False

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.CRITICAL + 1 if quiet else logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(console)

    for candidate in _candidate_paths(log_path):
        try:
            os.makedirs(os.path.dirname(candidate) or ".", exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                candidate, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT,
                encoding="utf-8",
            )
        except OSError:
            continue
        file_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
        file_handler.setFormatter(logging.Formatter(_FORMAT))
        logger.addHandler(file_handler)
        return candidate

    logger.warning("No writable location for the log file; logging to the console only.")
    return ""


def _candidate_paths(log_path: str | None) -> list[str]:
    if log_path:
        return [os.path.abspath(log_path)]
    return [
        os.path.join(default_log_directory(), LOG_FILENAME),
        os.path.join(tempfile.gettempdir(), LOG_FILENAME),
    ]
