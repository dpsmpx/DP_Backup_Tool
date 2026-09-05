"""Content fingerprinting."""

import os

import pytest

from conftest import write
from dp_backup.hashing import (
    HASH_FULL,
    HASH_NONE,
    HASH_PARTIAL,
    PARTIAL_EDGE,
    hash_file,
)


def test_none_mode_returns_empty(tmp_path):
    path = write(tmp_path / "a.txt", "content")
    assert hash_file(path, HASH_NONE) == ""


def test_unknown_mode_is_rejected(tmp_path):
    path = write(tmp_path / "a.txt", "content")
    with pytest.raises(ValueError):
        hash_file(path, "md5")


def test_empty_file_hashes(tmp_path):
    path = write(tmp_path / "empty", "")
    assert hash_file(path, HASH_FULL) == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    assert hash_file(path, HASH_PARTIAL)


def test_same_content_hashes_alike(tmp_path):
    a = write(tmp_path / "a", "identical")
    b = write(tmp_path / "b", "identical")
    assert hash_file(a, HASH_FULL) == hash_file(b, HASH_FULL)
    assert hash_file(a, HASH_PARTIAL) == hash_file(b, HASH_PARTIAL)


def test_different_content_of_equal_size_differs(tmp_path):
    a = write(tmp_path / "a", "AAAA")
    b = write(tmp_path / "b", "BBBB")
    assert hash_file(a, HASH_FULL) != hash_file(b, HASH_FULL)
    assert hash_file(a, HASH_PARTIAL) != hash_file(b, HASH_PARTIAL)


def test_partial_and_full_never_collide(tmp_path):
    path = write(tmp_path / "small", "tiny")
    assert hash_file(path, HASH_PARTIAL) != hash_file(path, HASH_FULL)


def test_partial_differs_when_size_differs(tmp_path):
    a = write(tmp_path / "a", b"x" * 10)
    b = write(tmp_path / "b", b"x" * 11)
    assert hash_file(a, HASH_PARTIAL) != hash_file(b, HASH_PARTIAL)


def test_partial_reads_both_ends(tmp_path):
    size = 4 * PARTIAL_EDGE
    base = bytearray(b"z" * size)
    head = bytearray(base)
    head[0] = ord("A")
    tail = bytearray(base)
    tail[-1] = ord("A")

    a = write(tmp_path / "base", bytes(base))
    b = write(tmp_path / "head", bytes(head))
    c = write(tmp_path / "tail", bytes(tail))

    assert hash_file(a, HASH_PARTIAL) != hash_file(b, HASH_PARTIAL)
    assert hash_file(a, HASH_PARTIAL) != hash_file(c, HASH_PARTIAL)


def test_partial_is_blind_to_the_middle_by_design(tmp_path):
    """Documented trade-off: use full hashing when this matters."""
    size = 4 * PARTIAL_EDGE
    base = bytearray(b"z" * size)
    middle = bytearray(base)
    middle[size // 2] = ord("A")

    a = write(tmp_path / "a", bytes(base))
    b = write(tmp_path / "b", bytes(middle))

    assert hash_file(a, HASH_PARTIAL) == hash_file(b, HASH_PARTIAL)
    assert hash_file(a, HASH_FULL) != hash_file(b, HASH_FULL)


def test_missing_file_raises_oserror(tmp_path):
    with pytest.raises(OSError):
        hash_file(str(tmp_path / "nope"), HASH_FULL)


def test_size_argument_is_optional(tmp_path):
    path = write(tmp_path / "a", "hello")
    assert hash_file(path, HASH_PARTIAL) == hash_file(
        path, HASH_PARTIAL, size=os.path.getsize(path)
    )
