"""The command-line interface and its exit codes."""

import json
import os

import pytest

from conftest import build_tree, read, structure_json, tree_contents
from dp_backup.cli import (
    EXIT_ATTENTION,
    EXIT_ERROR,
    EXIT_INTERRUPTED,
    EXIT_OK,
    main,
)


@pytest.fixture(autouse=True)
def log_to_tmp(tmp_path, monkeypatch):
    """Keep tests from writing to the real user log directory."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))


def test_no_command_prints_help(capsys):
    assert main([]) == EXIT_ERROR
    assert "usage:" in capsys.readouterr().out


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "DP Backup Tool" in capsys.readouterr().out


def test_export_then_restore_round_trip(tmp_path, capsys):
    original = build_tree(tmp_path / "orig", {"a.txt": "alpha", "sub/b.txt": "bravo"})
    source = build_tree(tmp_path / "src", {"one.rec": "alpha", "two.rec": "bravo"})
    destination = tmp_path / "dst"
    structure = str(tmp_path / "s.json")

    assert main(["-q", "export", str(original), structure]) == EXIT_OK
    assert os.path.exists(structure)

    assert main(["-q", "restore", structure, str(source), str(destination)]) == EXIT_OK
    assert tree_contents(destination) == tree_contents(original)


def test_restore_exit_code_signals_attention(tmp_path):
    original = build_tree(tmp_path / "orig", {"a.txt": "alpha"})
    source = build_tree(tmp_path / "src", {})
    destination = tmp_path / "dst"
    structure = str(tmp_path / "s.json")

    main(["-q", "export", str(original), structure])
    assert main(["-q", "restore", structure, str(source), str(destination)]) == EXIT_ATTENTION


def test_dry_run_writes_nothing(tmp_path):
    original = build_tree(tmp_path / "orig", {"a.txt": "alpha"})
    source = build_tree(tmp_path / "src", {"a.txt": "alpha"})
    destination = tmp_path / "dst"
    destination.mkdir()
    structure = str(tmp_path / "s.json")

    main(["-q", "export", str(original), structure])
    main(["-q", "restore", structure, str(source), str(destination), "--dry-run"])
    assert tree_contents(destination) == {}


def test_export_of_a_missing_directory(tmp_path, capsys):
    code = main(["export", str(tmp_path / "nope"), str(tmp_path / "s.json")])
    assert code == EXIT_ERROR
    assert "does not exist" in capsys.readouterr().err


def test_restore_with_a_broken_structure_file(tmp_path, capsys):
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    source = build_tree(tmp_path / "src", {})
    code = main(["restore", str(broken), str(source), str(tmp_path / "dst")])
    assert code == EXIT_ERROR
    assert "not valid JSON" in capsys.readouterr().err


def test_invalid_entries_block_the_run_until_acknowledged(tmp_path, capsys):
    structure = structure_json(
        tmp_path / "s.json",
        [
            {"path": "../escape", "kind": "file", "size": 1},
            {"path": "fine.txt", "kind": "file", "size": 5},
        ],
    )
    source = build_tree(tmp_path / "src", {"fine.txt": "alpha"})
    destination = tmp_path / "dst"

    code = main(["restore", structure, str(source), str(destination)])
    assert code == EXIT_ERROR
    assert "--skip-invalid" in capsys.readouterr().err
    assert not destination.exists() or tree_contents(destination) == {}

    code = main(["-q", "restore", structure, str(source), str(destination), "--skip-invalid"])
    assert code == EXIT_ATTENTION       # ran, but the rejects are reported
    assert read(destination / "fine.txt") == "alpha"


def test_empty_structure_is_reported_not_an_error(tmp_path, capsys):
    structure = structure_json(tmp_path / "s.json", [])
    source = build_tree(tmp_path / "src", {})
    code = main(["restore", structure, str(source), str(tmp_path / "dst")])
    assert code == EXIT_OK
    assert "nothing to do" in capsys.readouterr().out


def test_overlapping_directories_are_refused(tmp_path, capsys):
    structure = structure_json(tmp_path / "s.json", [{"path": "a", "kind": "directory"}])
    shared = build_tree(tmp_path / "shared", {})
    code = main(["restore", structure, str(shared), str(shared)])
    assert code == EXIT_ERROR
    assert "refusing to run" in capsys.readouterr().err


def test_reports_are_written(tmp_path):
    original = build_tree(tmp_path / "orig", {"a.txt": "alpha"})
    source = build_tree(tmp_path / "src", {"a.txt": "alpha"})
    structure = str(tmp_path / "s.json")
    export_report = tmp_path / "export.txt"
    restore_report = tmp_path / "restore.txt"
    json_report = tmp_path / "restore.json"

    main(["-q", "export", str(original), structure, "--report", str(export_report)])
    assert "export report" in export_report.read_text(encoding="utf-8")

    main(["-q", "restore", structure, str(source), str(tmp_path / "dst"),
          "--report", str(restore_report)])
    assert "restore report" in restore_report.read_text(encoding="utf-8")

    main(["-q", "restore", structure, str(source), str(tmp_path / "dst2"),
          "--report", str(json_report)])
    payload = json.loads(json_report.read_text(encoding="utf-8"))
    assert payload["counts"]["restored"] == 1
    assert payload["entries"][0]["path"] == "a.txt"


def test_hash_mode_is_passed_through(tmp_path):
    original = build_tree(tmp_path / "orig", {"a.txt": "alpha"})
    structure = str(tmp_path / "s.json")
    main(["-q", "export", str(original), structure, "--hash", "none"])
    payload = json.loads(open(structure, encoding="utf-8").read())
    assert payload["hash_mode"] == "none"
    assert "digest" not in payload["items"][0]


def test_interrupt_gives_the_conventional_exit_code(tmp_path, monkeypatch, capsys):
    original = build_tree(tmp_path / "orig", {"a.txt": "alpha"})

    import dp_backup.cli as cli

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "export_structure", interrupt)
    assert main(["export", str(original), str(tmp_path / "s.json")]) == EXIT_INTERRUPTED
    assert "Interrupted" in capsys.readouterr().err


def test_log_option_is_honoured(tmp_path):
    original = build_tree(tmp_path / "orig", {"a.txt": "alpha"})
    log = tmp_path / "custom" / "run.log"
    main(["-q", "--log", str(log), "export", str(original), str(tmp_path / "s.json")])
    assert log.exists() and log.read_text(encoding="utf-8")


def test_quiet_suppresses_the_report(tmp_path, capsys):
    original = build_tree(tmp_path / "orig", {"a.txt": "alpha"})
    main(["-q", "export", str(original), str(tmp_path / "s.json")])
    assert capsys.readouterr().out == ""
