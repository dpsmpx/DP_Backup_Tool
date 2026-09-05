"""Loading, validating and writing structure files."""

import json
import os

import pytest

from conftest import structure_json, write
from dp_backup.structure import (
    KIND_DIRECTORY,
    KIND_FILE,
    Entry,
    Structure,
    StructureError,
    load_structure,
    save_structure,
)


# -- file-level failures -------------------------------------------------


def test_missing_file(tmp_path):
    with pytest.raises(StructureError, match="not found"):
        load_structure(str(tmp_path / "nope.json"))


def test_directory_instead_of_file(tmp_path):
    (tmp_path / "adir").mkdir()
    with pytest.raises(StructureError, match="is a directory"):
        load_structure(str(tmp_path / "adir"))


def test_invalid_json(tmp_path):
    path = write(tmp_path / "bad.json", "{not json")
    with pytest.raises(StructureError, match="not valid JSON"):
        load_structure(path)


def test_not_utf8(tmp_path):
    path = write(tmp_path / "enc.json", b"\xff\xfe{}")
    with pytest.raises(StructureError, match="not valid UTF-8"):
        load_structure(path)


@pytest.mark.parametrize("body", ["42", '"text"', "null", "true"])
def test_wrong_top_level_type(tmp_path, body):
    path = write(tmp_path / "x.json", body)
    with pytest.raises(StructureError):
        load_structure(path)


def test_foreign_format_is_refused(tmp_path):
    path = write(tmp_path / "f.json", json.dumps({"format": "something_else", "items": []}))
    with pytest.raises(StructureError, match="not a DP Backup structure file"):
        load_structure(path)


def test_future_version_is_refused(tmp_path):
    path = write(
        tmp_path / "f.json",
        json.dumps({"format": "dp_backup_structure", "version": 99, "items": []}),
    )
    with pytest.raises(StructureError, match="newer version"):
        load_structure(path)


@pytest.mark.parametrize("items", [None, {}, "list", 5])
def test_items_must_be_a_list(tmp_path, items):
    path = write(
        tmp_path / "f.json",
        json.dumps({"format": "dp_backup_structure", "version": 2, "items": items}),
    )
    with pytest.raises(StructureError, match="no usable 'items'"):
        load_structure(path)


# -- entry-level problems are collected, never raised --------------------


def test_entry_problems_do_not_raise(tmp_path):
    path = structure_json(
        tmp_path / "s.json",
        [
            "not an object",
            123,
            {"kind": "file", "size": 1},                       # no path
            {"path": "../escape", "kind": "file", "size": 1},  # traversal
            {"path": "/abs", "kind": "file", "size": 1},       # absolute
            {"path": "a.txt"},                                 # no kind
            {"path": "b.txt", "kind": "nonsense"},
            {"path": "c.txt", "kind": "file", "size": "12"},   # size not an int
            {"path": "d.txt", "kind": "file", "size": -1},
            {"path": "e.txt", "kind": "file", "size": True},   # bool is not a size
            {"path": "f.txt", "kind": "file"},                 # the 1.x KeyError crash
            {"path": "g.txt", "kind": "file", "size": 1.5},
            {"path": "h", "kind": "symlink"},                  # no target
            {"path": "i", "kind": "symlink", "target": ""},
            {"path": "j.txt", "kind": "file", "size": 1, "mtime": "soon"},
            {"path": "k.txt", "kind": "file", "size": 1, "digest": 5},
            {"path": "ok.txt", "kind": "file", "size": 3, "mtime": 1.0, "digest": "ab"},
            {"path": "okdir", "kind": "directory"},
        ],
    )
    structure, problems = load_structure(path)
    assert [e.path for e in structure.entries] == ["ok.txt", "okdir"]
    assert len(problems) == 16


def test_duplicate_paths_are_rejected_once(tmp_path):
    path = structure_json(
        tmp_path / "s.json",
        [
            {"path": "a.txt", "kind": "file", "size": 1},
            {"path": "a.txt", "kind": "file", "size": 1},
        ],
    )
    structure, problems = load_structure(path)
    assert len(structure.entries) == 1
    assert "duplicate" in str(problems[0])


def test_problem_list_is_capped(tmp_path):
    path = structure_json(tmp_path / "s.json", [{"bad": i} for i in range(500)])
    _, problems = load_structure(path)
    assert len(problems) == 201
    assert "further problem" in str(problems[-1])


def test_unknown_hash_mode_falls_back_to_none(tmp_path):
    path = structure_json(tmp_path / "s.json", [], hash_mode="md5")
    structure, _ = load_structure(path)
    assert structure.hash_mode == "none"


# -- version 1 compatibility --------------------------------------------


def test_reads_version_1_files(tmp_path):
    path = write(
        tmp_path / "old.json",
        json.dumps(
            [
                {"original_relative_path": "a", "type": "directory", "name": "a"},
                {
                    "original_relative_path": "a/b.txt",
                    "type": "file",
                    "name": "b.txt",
                    "size_bytes": 10,
                },
            ]
        ),
    )
    structure, problems = load_structure(path)
    assert problems == []
    assert structure.version == 1
    assert structure.hash_mode == "none"
    assert [(e.path, e.kind, e.size) for e in structure.entries] == [
        ("a", KIND_DIRECTORY, None),
        ("a/b.txt", KIND_FILE, 10),
    ]


def test_version_1_missing_size_is_a_problem_not_a_crash(tmp_path):
    path = write(
        tmp_path / "old.json",
        json.dumps([{"original_relative_path": "c.txt", "type": "file", "name": "c.txt"}]),
    )
    structure, problems = load_structure(path)
    assert structure.entries == []
    assert len(problems) == 1


def test_empty_v1_list(tmp_path):
    path = write(tmp_path / "old.json", "[]")
    structure, problems = load_structure(path)
    assert structure.entries == [] and problems == []


# -- writing -------------------------------------------------------------


def test_round_trip(tmp_path):
    structure = Structure(hash_mode="full", source_root="/somewhere")
    structure.entries = [
        Entry(path="dir", kind=KIND_DIRECTORY, mtime=1.0),
        Entry(path="dir/f.txt", kind=KIND_FILE, size=5, mtime=2.0, digest="deadbeef"),
        Entry(path="link", kind="symlink", target="dir/f.txt", mtime=3.0),
    ]
    out = str(tmp_path / "s.json")
    save_structure(structure, out)

    loaded, problems = load_structure(out)
    assert problems == []
    assert loaded.hash_mode == "full"
    assert [e.path for e in loaded.entries] == ["dir", "dir/f.txt", "link"]
    assert loaded.files[0].digest == "deadbeef"
    assert loaded.symlinks[0].target == "dir/f.txt"


def test_save_is_atomic_and_leaves_no_scratch(tmp_path):
    structure = Structure()
    structure.entries = [Entry(path="a.txt", kind=KIND_FILE, size=1)]
    out = str(tmp_path / "s.json")
    save_structure(structure, out)
    assert [p.name for p in tmp_path.iterdir()] == ["s.json"]


def test_save_creates_missing_directories(tmp_path):
    structure = Structure()
    out = str(tmp_path / "deep" / "nested" / "s.json")
    save_structure(structure, out)
    assert os.path.exists(out)


def test_entry_helpers():
    entry = Entry(path="a/b/My.File.TXT", kind=KIND_FILE, size=1)
    assert entry.name == "My.File.TXT"
    assert entry.extension == ".txt"
    assert Entry(path="noext", kind=KIND_FILE).extension == ""
    assert Entry(path=".hidden", kind=KIND_FILE).extension == ""
