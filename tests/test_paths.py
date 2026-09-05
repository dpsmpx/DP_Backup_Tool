"""Path validation: the defence against a hostile or corrupt structure file."""

import os

import pytest

from dp_backup.paths import (
    UnsafePathError,
    describe_overlap,
    is_within,
    normalize_relpath,
    safe_join,
)


@pytest.mark.parametrize(
    "value",
    [
        "../escape.txt",
        "a/../../escape.txt",
        "..",
        "../../..",
        "/etc/passwd",
        "/",
        "C:/Windows/system32",
        "c:\\Windows",
        "..\\..\\escape.txt",
        "a\\..\\..\\b",
        "",
        ".",
        "./",
        "with\x00nul",
    ],
)
def test_rejects_unsafe_paths(value):
    with pytest.raises(UnsafePathError):
        normalize_relpath(value)


@pytest.mark.parametrize("value", [None, 42, [], {}, 3.5, True])
def test_rejects_non_strings(value):
    with pytest.raises(UnsafePathError):
        normalize_relpath(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("a.txt", "a.txt"),
        ("a/b/c.txt", "a/b/c.txt"),
        ("a//b///c.txt", "a/b/c.txt"),
        ("./a/./b.txt", "a/b.txt"),
        ("a\\b\\c.txt", "a/b/c.txt"),
        ("Юникод/файл «кавычки».txt", "Юникод/файл «кавычки».txt"),
        ("file with spaces.txt", "file with spaces.txt"),
        ("...hidden", "...hidden"),
        ("a/..b/c", "a/..b/c"),
    ],
)
def test_accepts_and_normalises(value, expected):
    assert normalize_relpath(value) == expected


@pytest.mark.skipif(os.name == "nt", reason="these names are legal on POSIX only")
def test_posix_names_that_windows_reserves_are_allowed():
    assert normalize_relpath("aux.txt") == "aux.txt"
    assert normalize_relpath("con") == "con"


def test_safe_join_stays_inside(tmp_path):
    root = str(tmp_path)
    assert safe_join(root, "a/b.txt") == os.path.join(root, "a", "b.txt")


def test_safe_join_refuses_traversal(tmp_path):
    with pytest.raises(UnsafePathError):
        safe_join(str(tmp_path), "../outside.txt")


def test_safe_join_refuses_writing_through_a_symlink(tmp_path):
    """A symlink planted in the destination must not redirect a write."""
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    os.symlink(str(outside), str(root / "escape"))

    with pytest.raises(UnsafePathError):
        safe_join(str(root), "escape/payload.txt")


def test_is_within(tmp_path):
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    assert is_within(str(root), str(root))
    assert is_within(str(root), str(root / "sub"))
    assert not is_within(str(root), str(tmp_path))


def test_describe_overlap(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    inner = a / "inner"
    inner.mkdir(parents=True)
    b.mkdir()

    assert describe_overlap(str(a), str(a)) is not None
    assert describe_overlap(str(a), str(inner)) is not None
    assert describe_overlap(str(inner), str(a)) is not None
    assert describe_overlap(str(a), str(b)) is None
