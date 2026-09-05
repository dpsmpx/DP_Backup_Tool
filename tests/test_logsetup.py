"""Log configuration must never be the reason a restore cannot run."""

import logging
import os

import pytest

from dp_backup.logsetup import configure_logging, default_log_directory


@pytest.fixture(autouse=True)
def clean_logger():
    yield
    logger = logging.getLogger("dp_backup")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def test_default_directory_is_per_user():
    assert os.path.isabs(default_log_directory())


def test_creates_the_log_and_writes_to_it(tmp_path):
    path = configure_logging(str(tmp_path / "deep" / "run.log"))
    logging.getLogger("dp_backup.test").warning("something happened")
    assert path and os.path.exists(path)
    assert "something happened" in open(path, encoding="utf-8").read()


def test_falls_back_to_console_when_nothing_is_writable(monkeypatch):
    import dp_backup.logsetup as logsetup

    monkeypatch.setattr(
        logsetup, "_candidate_paths", lambda _: ["/proc/nonexistent/dir/x.log"]
    )
    assert configure_logging() == ""
    # Still usable: a console handler is attached and logging does not raise.
    logging.getLogger("dp_backup.test").warning("still works")


def test_handlers_are_not_duplicated_on_repeated_setup(tmp_path):
    for _ in range(3):
        configure_logging(str(tmp_path / "run.log"))
    assert len(logging.getLogger("dp_backup").handlers) == 2  # console + file


def test_rotation_is_configured(tmp_path):
    configure_logging(str(tmp_path / "run.log"))
    handlers = logging.getLogger("dp_backup").handlers
    rotating = [h for h in handlers if hasattr(h, "maxBytes")]
    assert rotating and rotating[0].maxBytes > 0 and rotating[0].backupCount > 0


def test_log_does_not_grow_without_bound(tmp_path):
    """The 1.x logger could write gigabytes; this one rotates."""
    import dp_backup.logsetup as logsetup

    monkeypatch_max = 8 * 1024
    original = logsetup.MAX_BYTES
    logsetup.MAX_BYTES = monkeypatch_max
    try:
        configure_logging(str(tmp_path / "run.log"))
        logger = logging.getLogger("dp_backup.test")
        for index in range(2000):
            logger.warning("a long message to fill the log up quickly %d %s", index, "x" * 200)
    finally:
        logsetup.MAX_BYTES = original

    total = sum(
        os.path.getsize(tmp_path / name) for name in os.listdir(tmp_path)
    )
    assert total < 8 * monkeypatch_max


def test_quiet_silences_the_console(tmp_path):
    configure_logging(str(tmp_path / "run.log"), quiet=True)
    console = [
        h for h in logging.getLogger("dp_backup").handlers if not hasattr(h, "maxBytes")
    ]
    assert console[0].level > logging.CRITICAL
