"""Scanning a tree into a structure file."""

import json
import os
import stat

import pytest

from conftest import build_tree, write
from dp_backup.export import ExportError, export_structure
from dp_backup.structure import load_structure


def test_records_a_tree(tmp_path):
    root = build_tree(
        tmp_path / "root",
        {"a.txt": "alpha", "sub/b.txt": "bravo", "sub/deep/c.txt": ""},
    )
    (tmp_path / "root" / "empty").mkdir()
    out = str(tmp_path / "s.json")

    result = export_structure(str(root), out, hash_mode="full")

    assert result.ok
    assert result.counts["files"] == 3
    assert result.counts["directories"] == 3  # sub, sub/deep, empty
    structure, problems = load_structure(out)
    assert problems == []
    assert {e.path for e in structure.files} == {"a.txt", "sub/b.txt", "sub/deep/c.txt"}
    assert all(e.digest for e in structure.files)
    assert all(e.mtime is not None for e in structure.entries)


def test_zero_byte_files_are_recorded(tmp_path):
    root = build_tree(tmp_path / "root", {"empty.txt": ""})
    out = str(tmp_path / "s.json")
    export_structure(str(root), out, hash_mode="full")
    structure, _ = load_structure(out)
    assert structure.files[0].size == 0
    assert structure.files[0].digest


def test_unicode_and_awkward_names(tmp_path):
    names = {
        "Юникод/файл «кавычки».txt": "russian",
        "with spaces/and 'quotes'.txt": "quotes",
        "emoji 🎉.dat": "emoji",
    }
    root = build_tree(tmp_path / "root", names)
    out = str(tmp_path / "s.json")
    export_structure(str(root), out, hash_mode="none")
    structure, problems = load_structure(out)
    assert problems == []
    assert {e.path for e in structure.files} == set(names)


def test_hash_modes(tmp_path):
    root = build_tree(tmp_path / "root", {"a.txt": "x"})
    for mode, expect_digest in (("full", True), ("partial", True), ("none", False)):
        out = str(tmp_path / f"{mode}.json")
        export_structure(str(root), out, hash_mode=mode)
        structure, _ = load_structure(out)
        assert structure.hash_mode == mode
        assert bool(structure.files[0].digest) is expect_digest


def test_symlinks_are_recorded_not_followed(tmp_path):
    root = tmp_path / "root"
    build_tree(root, {"a.txt": "alpha", "sub/b.txt": "bravo"})
    os.symlink("a.txt", str(root / "to_file"))
    os.symlink("sub", str(root / "to_dir"))
    os.symlink("/nowhere/at/all", str(root / "broken"))

    out = str(tmp_path / "s.json")
    result = export_structure(str(root), out)

    structure, _ = load_structure(out)
    links = {e.path: e.target for e in structure.symlinks}
    assert links == {"to_file": "a.txt", "to_dir": "sub", "broken": "/nowhere/at/all"}
    # The symlinked directory is not descended into.
    assert "to_dir/b.txt" not in {e.path for e in structure.files}
    assert result.counts["files"] == 2


def test_symlink_loop_does_not_hang(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    os.symlink("loop", str(root / "loop"))
    os.symlink(str(root), str(root / "self"))

    out = str(tmp_path / "s.json")
    result = export_structure(str(root), out)
    assert result.counts["symlinks"] == 2


def test_follow_symlinks_records_the_target_as_a_file(tmp_path):
    root = tmp_path / "root"
    build_tree(root, {"a.txt": "alpha"})
    os.symlink("a.txt", str(root / "to_file"))

    out = str(tmp_path / "s.json")
    export_structure(str(root), out, hash_mode="full", follow_symlinks=True)
    structure, _ = load_structure(out)
    assert {e.path for e in structure.files} == {"a.txt", "to_file"}
    assert structure.symlinks == []


def test_broken_symlink_under_follow_is_reported_not_fatal(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    os.symlink("/nowhere/at/all", str(root / "broken"))

    out = str(tmp_path / "s.json")
    result = export_structure(str(root), out, follow_symlinks=True)
    assert result.errors
    assert os.path.exists(out)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX FIFOs")
def test_special_files_are_skipped(tmp_path):
    root = tmp_path / "root"
    build_tree(root, {"a.txt": "alpha"})
    os.mkfifo(str(root / "a_fifo"))

    out = str(tmp_path / "s.json")
    result = export_structure(str(root), out)
    assert len(result.skipped_special) == 1
    assert result.counts["files"] == 1


def test_output_inside_the_scanned_root_is_never_recorded(tmp_path):
    root = build_tree(tmp_path / "root", {"a.txt": "alpha"})
    out = str(tmp_path / "root" / "structure.json")

    export_structure(str(root), out)
    export_structure(str(root), out)  # again, with the file already there

    structure, _ = load_structure(out)
    assert {e.path for e in structure.files} == {"a.txt"}


def test_scan_is_reproducible(tmp_path):
    root = build_tree(tmp_path / "root", {f"f{i}.txt": str(i) for i in range(20)})
    first = str(tmp_path / "a.json")
    second = str(tmp_path / "b.json")
    export_structure(str(root), first, hash_mode="full")
    export_structure(str(root), second, hash_mode="full")

    load = lambda p: [i["path"] for i in json.load(open(p, encoding="utf-8"))["items"]]
    assert load(first) == load(second)


def test_unreadable_file_is_recorded_without_a_digest(tmp_path, monkeypatch):
    root = build_tree(tmp_path / "root", {"a.txt": "alpha", "b.txt": "bravo"})
    out = str(tmp_path / "s.json")

    real = os.open

    def failing_open(path, *args, **kwargs):
        if str(path).endswith("a.txt"):
            raise PermissionError(13, "Permission denied")
        return real(path, *args, **kwargs)

    import dp_backup.hashing as hashing

    def failing_builtin_open(path, *args, **kwargs):
        if str(path).endswith("a.txt"):
            raise PermissionError(13, "Permission denied")
        return open(path, *args, **kwargs)

    monkeypatch.setattr(hashing, "open", failing_builtin_open, raising=False)

    result = export_structure(str(root), out, hash_mode="full")
    structure, _ = load_structure(out)
    digests = {e.path: e.digest for e in structure.files}
    assert digests["a.txt"] == ""      # kept, just without a fingerprint
    assert digests["b.txt"] != ""
    assert result.unreadable


def test_unlistable_directory_is_reported(tmp_path, monkeypatch):
    root = build_tree(tmp_path / "root", {"a.txt": "alpha"})
    out = str(tmp_path / "s.json")

    import dp_backup.export as export_module

    real_walk = export_module.os.walk

    def walk_with_error(path, onerror=None, followlinks=False):
        if onerror:
            onerror(PermissionError(13, "Permission denied", str(root / "secret")))
        yield from real_walk(path, onerror=onerror, followlinks=followlinks)

    monkeypatch.setattr(export_module.os, "walk", walk_with_error)
    result = export_structure(str(root), out)
    assert result.errors and not result.ok
    assert os.path.exists(out)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"root_dir": "missing", "output_path": "o.json"}, "does not exist"),
        ({"root_dir": "", "output_path": "o.json"}, "no source directory"),
        ({"root_dir": ".", "output_path": ""}, "no output file"),
        ({"root_dir": ".", "output_path": "o.json", "hash_mode": "md5"}, "unknown hash mode"),
    ],
)
def test_argument_errors(tmp_path, monkeypatch, kwargs, match):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ExportError, match=match):
        export_structure(**kwargs)


def test_root_that_is_a_file(tmp_path):
    path = write(tmp_path / "a.txt", "alpha")
    with pytest.raises(ExportError, match="not a directory"):
        export_structure(path, str(tmp_path / "o.json"))


def test_output_that_is_a_directory(tmp_path):
    root = build_tree(tmp_path / "root", {"a.txt": "x"})
    (tmp_path / "outdir").mkdir()
    with pytest.raises(ExportError, match="output path is a directory"):
        export_structure(str(root), str(tmp_path / "outdir"))


def test_unwritable_output_raises(tmp_path):
    root = build_tree(tmp_path / "root", {"a.txt": "x"})
    with pytest.raises(ExportError, match="could not write"):
        export_structure(str(root), "/proc/nonexistent/dir/s.json")


def test_progress_callback_fires(tmp_path):
    root = build_tree(tmp_path / "root", {f"f{i}.txt": str(i) for i in range(450)})
    seen = []
    export_structure(
        str(root), str(tmp_path / "s.json"), hash_mode="none",
        progress=lambda n, p: seen.append(n),
    )
    assert seen == [200, 400]
