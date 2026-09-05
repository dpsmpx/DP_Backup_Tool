"""Rebuilding a tree, including the failure modes that motivated version 2."""

import errno
import os
import shutil
import time

import pytest

from conftest import build_tree, read, structure_json, tree_contents, write
from dp_backup.export import export_structure
from dp_backup.restore import (
    FAILED,
    MISSING,
    PLANNED,
    REJECTED,
    RESTORED,
    SKIPPED_EXISTS,
    UNRESOLVED,
    RestoreError,
    restore_structure,
)
from dp_backup.structure import load_structure


def export_and_load(original, tmp_path, hash_mode="full", name="s.json"):
    out = str(tmp_path / name)
    export_structure(str(original), out, hash_mode=hash_mode)
    structure, problems = load_structure(out)
    assert problems == []
    return structure, out


def statuses(result):
    return {o.path: o.status for o in result.outcomes}


# -- the bugs that version 1 shipped with --------------------------------


def test_same_size_files_are_not_swapped_when_hashed(tmp_path, dirs):
    """1.x matched on size alone and silently swapped these two."""
    original, source, destination = dirs
    build_tree(original, {"docs/report.txt": "AAAA", "docs/photo.txt": "BBBB"})
    build_tree(source, {"photo.txt": "BBBB", "report.txt": "AAAA"})
    structure, _ = export_and_load(original, tmp_path)

    restore_structure(structure, str(source), str(destination))

    assert read(destination / "docs" / "report.txt") == "AAAA"
    assert read(destination / "docs" / "photo.txt") == "BBBB"


def test_same_size_files_are_not_swapped_using_names(tmp_path, dirs):
    """Even with no digests, a matching name beats an arbitrary pick."""
    original, source, destination = dirs
    build_tree(original, {"docs/report.txt": "AAAA", "docs/photo.txt": "BBBB"})
    build_tree(source, {"photo.txt": "BBBB", "report.txt": "AAAA"})
    structure, _ = export_and_load(original, tmp_path, hash_mode="none")

    result = restore_structure(structure, str(source), str(destination))

    assert read(destination / "docs" / "report.txt") == "AAAA"
    assert read(destination / "docs" / "photo.txt") == "BBBB"
    assert all(o.confidence == "name" for o in result.outcomes if o.status == RESTORED)


def test_duplicates_in_the_original_tree_are_all_restored(tmp_path, dirs):
    """1.x consumed the single source copy and dropped the second file."""
    original, source, destination = dirs
    build_tree(original, {"a/dup.txt": "SAME", "b/dup.txt": "SAME"})
    build_tree(source, {"only_copy.txt": "SAME"})
    structure, _ = export_and_load(original, tmp_path)

    result = restore_structure(structure, str(source), str(destination))

    assert read(destination / "a" / "dup.txt") == "SAME"
    assert read(destination / "b" / "dup.txt") == "SAME"
    assert set(statuses(result).values()) == {RESTORED}


def test_genuinely_ambiguous_files_are_not_guessed(tmp_path, dirs):
    """1.x picked one at random; version 2 refuses and says so."""
    original, source, destination = dirs
    build_tree(original, {"x/one.dat": "1111", "x/two.dat": "2222"})
    build_tree(source, {"f0001.rec": "1111", "f0002.rec": "2222"})
    structure, _ = export_and_load(original, tmp_path, hash_mode="none")

    result = restore_structure(structure, str(source), str(destination))

    assert set(statuses(result).values()) == {UNRESOLVED}
    assert tree_contents(destination) == {}
    assert result.needs_attention


def test_ambiguous_files_resolve_once_hashed(tmp_path, dirs):
    original, source, destination = dirs
    build_tree(original, {"x/one.dat": "1111", "x/two.dat": "2222"})
    build_tree(source, {"f0001.rec": "1111", "f0002.rec": "2222"})
    structure, _ = export_and_load(original, tmp_path, hash_mode="full")

    restore_structure(structure, str(source), str(destination))

    assert read(destination / "x" / "one.dat") == "1111"
    assert read(destination / "x" / "two.dat") == "2222"


def test_allow_ambiguous_restores_the_old_behaviour(tmp_path, dirs):
    original, source, destination = dirs
    build_tree(original, {"x/one.dat": "1111", "x/two.dat": "2222"})
    build_tree(source, {"f0001.rec": "1111", "f0002.rec": "2222"})
    structure, _ = export_and_load(original, tmp_path, hash_mode="none")

    result = restore_structure(
        structure, str(source), str(destination), allow_ambiguous=True
    )
    assert set(statuses(result).values()) == {RESTORED}
    assert all(
        o.confidence == "size-guess" for o in result.outcomes if o.status == RESTORED
    )


def test_right_size_wrong_content_is_refused(tmp_path, dirs):
    original, source, destination = dirs
    build_tree(original, {"keep.txt": "KEEP"})
    build_tree(source, {"keep.txt": "WRNG"})  # same size, different bytes
    structure, _ = export_and_load(original, tmp_path)

    result = restore_structure(structure, str(source), str(destination))

    assert statuses(result) == {"keep.txt": MISSING}
    assert not (destination / "keep.txt").exists()


# -- path safety ---------------------------------------------------------


def test_traversal_entries_never_escape(tmp_path, dirs):
    _, source, destination = dirs
    outside = tmp_path / "outside"
    outside.mkdir()
    build_tree(source, {"a.txt": "SECRET!"})

    path = structure_json(
        tmp_path / "evil.json",
        [
            {"path": "../outside/pwned.txt", "kind": "file", "size": 7},
            {"path": "/tmp/pwned.txt", "kind": "file", "size": 7},
            {"path": "..\\..\\pwned.txt", "kind": "file", "size": 7},
            {"path": "fine.txt", "kind": "file", "size": 7},
        ],
    )
    structure, problems = load_structure(path)

    assert len(problems) == 3          # rejected before the restore even starts
    result = restore_structure(structure, str(source), str(destination))
    assert statuses(result) == {"fine.txt": RESTORED}
    assert list(outside.iterdir()) == []
    assert not os.path.exists("/tmp/pwned.txt")


def test_symlink_in_destination_cannot_redirect_a_write(tmp_path, dirs):
    _, source, destination = dirs
    outside = tmp_path / "outside"
    outside.mkdir()
    build_tree(source, {"a.txt": "SECRET!"})
    os.symlink(str(outside), str(destination / "escape"))

    path = structure_json(
        tmp_path / "s.json",
        [{"path": "escape/payload.txt", "kind": "file", "size": 7}],
    )
    structure, _ = load_structure(path)

    result = restore_structure(structure, str(source), str(destination))
    assert statuses(result) == {"escape/payload.txt": REJECTED}
    assert list(outside.iterdir()) == []


# -- argument and overlap checks ----------------------------------------


def test_overlapping_directories_are_refused(tmp_path):
    path = structure_json(tmp_path / "s.json", [])
    structure, _ = load_structure(path)
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)

    for source, destination in (
        (outer, outer),
        (outer, inner),
        (inner, outer),
    ):
        with pytest.raises(RestoreError, match="refusing to run"):
            restore_structure(structure, str(source), str(destination))


def test_missing_source_is_refused(tmp_path, dirs):
    _, _, destination = dirs
    structure, _ = load_structure(structure_json(tmp_path / "s.json", []))
    with pytest.raises(RestoreError, match="source directory does not exist"):
        restore_structure(structure, str(tmp_path / "nope"), str(destination))


def test_destination_that_is_a_file_is_refused(tmp_path, dirs):
    _, source, _ = dirs
    target = write(tmp_path / "afile", "x")
    structure, _ = load_structure(structure_json(tmp_path / "s.json", []))
    with pytest.raises(RestoreError, match="not a directory"):
        restore_structure(structure, str(source), target)


def test_destination_is_created_when_absent(tmp_path, dirs):
    original, source, _ = dirs
    build_tree(original, {"a.txt": "alpha"})
    build_tree(source, {"a.txt": "alpha"})
    structure, _ = export_and_load(original, tmp_path)

    destination = tmp_path / "brand" / "new"
    restore_structure(structure, str(source), str(destination))
    assert read(destination / "a.txt") == "alpha"


def test_dry_run_requires_an_existing_destination(tmp_path, dirs):
    _, source, _ = dirs
    structure, _ = load_structure(structure_json(tmp_path / "s.json", []))
    with pytest.raises(RestoreError, match="destination directory does not exist"):
        restore_structure(
            structure, str(source), str(tmp_path / "nope"), dry_run=True
        )


# -- behaviour -----------------------------------------------------------


def test_dry_run_writes_nothing(tmp_path, dirs):
    original, source, destination = dirs
    build_tree(original, {"a.txt": "alpha", "sub/b.txt": "bravo"})
    build_tree(source, {"a.txt": "alpha", "b.txt": "bravo"})
    structure, _ = export_and_load(original, tmp_path)

    result = restore_structure(structure, str(source), str(destination), dry_run=True)

    assert set(statuses(result).values()) == {PLANNED}
    assert tree_contents(destination) == {}
    assert list(destination.iterdir()) == []
    assert result.directories_created == 1


def test_existing_files_are_not_overwritten(tmp_path, dirs):
    original, source, destination = dirs
    build_tree(original, {"a.txt": "alpha"})
    build_tree(source, {"a.txt": "alpha"})
    build_tree(destination, {"a.txt": "MINE"})
    structure, _ = export_and_load(original, tmp_path)

    result = restore_structure(structure, str(source), str(destination))

    assert statuses(result) == {"a.txt": SKIPPED_EXISTS}
    assert read(destination / "a.txt") == "MINE"


def test_overwrite_replaces_them(tmp_path, dirs):
    original, source, destination = dirs
    build_tree(original, {"a.txt": "alpha"})
    build_tree(source, {"a.txt": "alpha"})
    build_tree(destination, {"a.txt": "MINE"})
    structure, _ = export_and_load(original, tmp_path)

    restore_structure(structure, str(source), str(destination), overwrite=True)
    assert read(destination / "a.txt") == "alpha"


def test_modification_times_are_restored(tmp_path, dirs):
    original, source, destination = dirs
    build_tree(original, {"a.txt": "alpha", "sub/b.txt": "bravo"})
    build_tree(source, {"a.txt": "alpha", "b.txt": "bravo"})
    old = time.time() - 86400 * 30
    for path in (original / "a.txt", original / "sub"):
        os.utime(path, (old, old))
    structure, _ = export_and_load(original, tmp_path)

    restore_structure(structure, str(source), str(destination))

    assert abs(os.path.getmtime(destination / "a.txt") - old) < 2
    assert abs(os.path.getmtime(destination / "sub") - old) < 2


def test_no_mtime_option(tmp_path, dirs):
    original, source, destination = dirs
    build_tree(original, {"a.txt": "alpha"})
    build_tree(source, {"a.txt": "alpha"})
    old = time.time() - 86400 * 30
    os.utime(original / "a.txt", (old, old))
    structure, _ = export_and_load(original, tmp_path)

    restore_structure(structure, str(source), str(destination), restore_mtime=False)
    assert abs(os.path.getmtime(destination / "a.txt") - old) > 60


def test_symlinks_are_recreated(tmp_path, dirs):
    original, source, destination = dirs
    build_tree(original, {"a.txt": "alpha"})
    os.symlink("a.txt", str(original / "alias"))
    build_tree(source, {"a.txt": "alpha"})
    structure, _ = export_and_load(original, tmp_path)

    result = restore_structure(structure, str(source), str(destination))

    assert os.path.islink(destination / "alias")
    assert os.readlink(destination / "alias") == "a.txt"
    assert result.symlinks_created == 1


def test_symlinks_can_be_skipped(tmp_path, dirs):
    original, source, destination = dirs
    build_tree(original, {"a.txt": "alpha"})
    os.symlink("a.txt", str(original / "alias"))
    build_tree(source, {"a.txt": "alpha"})
    structure, _ = export_and_load(original, tmp_path)

    restore_structure(
        structure, str(source), str(destination), restore_symlinks=False
    )
    assert not os.path.lexists(destination / "alias")


def test_empty_directories_are_recreated(tmp_path, dirs):
    original, source, destination = dirs
    (original / "empty" / "deeper").mkdir(parents=True)
    structure, _ = export_and_load(original, tmp_path)

    restore_structure(structure, str(source), str(destination))
    assert (destination / "empty" / "deeper").is_dir()


def test_verify_detects_corruption_mid_copy(tmp_path, dirs, monkeypatch):
    original, source, destination = dirs
    build_tree(original, {"a.txt": "alpha"})
    build_tree(source, {"a.txt": "alpha"})
    structure, _ = export_and_load(original, tmp_path)

    def corrupting_copy(src, dst, **kwargs):
        with open(dst, "w", encoding="utf-8") as handle:
            handle.write("BAD!!")  # same length, wrong bytes
        return dst

    monkeypatch.setattr(shutil, "copyfile", corrupting_copy)
    result = restore_structure(structure, str(source), str(destination), verify=True)

    assert statuses(result) == {"a.txt": FAILED}
    assert not (destination / "a.txt").exists()


def test_size_mismatch_is_caught_without_verify(tmp_path, dirs, monkeypatch):
    original, source, destination = dirs
    build_tree(original, {"a.txt": "alpha"})
    build_tree(source, {"a.txt": "alpha"})
    structure, _ = export_and_load(original, tmp_path)

    def truncating_copy(src, dst, **kwargs):
        with open(dst, "w", encoding="utf-8") as handle:
            handle.write("ab")
        return dst

    monkeypatch.setattr(shutil, "copyfile", truncating_copy)
    result = restore_structure(structure, str(source), str(destination))

    assert statuses(result) == {"a.txt": FAILED}
    assert not (destination / "a.txt").exists()


# -- resilience ----------------------------------------------------------


def leftover_parts(root):
    return [
        name
        for _, _, filenames in os.walk(root)
        for name in filenames
        if ".part" in name or name.startswith(".dp_restore")
    ]


def test_interrupt_leaves_no_partial_files(tmp_path, dirs, monkeypatch):
    original, source, destination = dirs
    files = {f"f{i}.txt": f"content-{i}" for i in range(5)}
    build_tree(original, files)
    build_tree(source, files)
    structure, _ = export_and_load(original, tmp_path)

    real = shutil.copyfile
    calls = {"n": 0}

    def interrupting(src, dst, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:
            raise KeyboardInterrupt
        return real(src, dst, **kwargs)

    monkeypatch.setattr(shutil, "copyfile", interrupting)
    with pytest.raises(KeyboardInterrupt):
        restore_structure(structure, str(source), str(destination))

    assert leftover_parts(destination) == []
    assert len(tree_contents(destination)) == 2  # the two that finished


def test_out_of_space_aborts_instead_of_failing_every_file(tmp_path, dirs, monkeypatch):
    original, source, destination = dirs
    files = {f"f{i}.txt": f"content-{i}" for i in range(50)}
    build_tree(original, files)
    build_tree(source, files)
    structure, _ = export_and_load(original, tmp_path)

    real = shutil.copyfile
    calls = {"n": 0}

    def out_of_space(src, dst, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 3:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(src, dst, **kwargs)

    monkeypatch.setattr(shutil, "copyfile", out_of_space)
    result = restore_structure(structure, str(source), str(destination))

    assert result.aborted
    assert len(result.outcomes) == 3      # stopped, did not attempt all 50
    assert leftover_parts(destination) == []


def test_one_io_error_does_not_stop_the_rest(tmp_path, dirs, monkeypatch):
    original, source, destination = dirs
    files = {f"f{i}.txt": f"content-{i}" for i in range(5)}
    build_tree(original, files)
    build_tree(source, files)
    structure, _ = export_and_load(original, tmp_path)

    real = shutil.copyfile
    calls = {"n": 0}

    def flaky(src, dst, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(errno.EIO, "Input/output error")
        return real(src, dst, **kwargs)

    monkeypatch.setattr(shutil, "copyfile", flaky)
    result = restore_structure(structure, str(source), str(destination))

    tally = result.counts
    assert tally[RESTORED] == 4 and tally[FAILED] == 1
    assert leftover_parts(destination) == []


def test_empty_structure_is_a_no_op(tmp_path, dirs):
    _, source, destination = dirs
    structure, _ = load_structure(structure_json(tmp_path / "s.json", []))
    result = restore_structure(structure, str(source), str(destination))
    assert result.outcomes == []
    assert not result.needs_attention


def test_empty_source_reports_everything_missing(tmp_path, dirs):
    original, source, destination = dirs
    build_tree(original, {"a.txt": "alpha"})
    structure, _ = export_and_load(original, tmp_path)

    result = restore_structure(structure, str(source), str(destination))
    assert statuses(result) == {"a.txt": MISSING}


def test_unused_source_files_are_counted(tmp_path, dirs):
    original, source, destination = dirs
    build_tree(original, {"a.txt": "alpha"})
    build_tree(source, {"a.txt": "alpha", "junk.bin": "unrelated stuff"})
    structure, _ = export_and_load(original, tmp_path)

    result = restore_structure(structure, str(source), str(destination))
    assert result.source_files_seen == 2
    assert result.source_files_unused == 1


def test_version_1_structure_still_restores(tmp_path, dirs):
    """A structure file made by the old tool must keep working."""
    import json

    original, source, destination = dirs
    build_tree(original, {"docs/notes.txt": "hello"})
    build_tree(source, {"notes.txt": "hello"})

    legacy = tmp_path / "old.json"
    legacy.write_text(
        json.dumps(
            [
                {"original_relative_path": "docs", "type": "directory", "name": "docs"},
                {
                    "original_relative_path": "docs/notes.txt",
                    "type": "file",
                    "name": "notes.txt",
                    "size_bytes": 5,
                },
            ]
        ),
        encoding="utf-8",
    )
    structure, problems = load_structure(str(legacy))
    assert problems == []

    result = restore_structure(structure, str(source), str(destination))
    assert read(destination / "docs" / "notes.txt") == "hello"
    assert result.counts[RESTORED] == 1


def test_full_round_trip_is_byte_exact(tmp_path, dirs):
    original, source, destination = dirs
    files = {
        "a.txt": "alpha",
        "sub/b.bin": "b" * 100,
        "sub/deep/c.txt": "",
        "Юникод/файл.txt": "юникод",
        "dup1.dat": "duplicate",
        "sub/dup2.dat": "duplicate",
    }
    build_tree(original, files)
    # The source keeps the bytes but loses every name and folder.
    for index, content in enumerate(files.values()):
        write(source / f"rec{index:04d}.bin", content)
    structure, _ = export_and_load(original, tmp_path)

    result = restore_structure(structure, str(source), str(destination), verify=True)

    assert not result.needs_attention
    assert tree_contents(destination) == tree_contents(original)
