"""The tier logic, exercised directly."""

import pytest

from conftest import build_tree, write
from dp_backup.matching import (
    AMBIGUOUS,
    MATCH_CONTENT,
    MATCH_EXTENSION,
    MATCH_NAME,
    MATCH_NAME_AMBIGUOUS,
    MATCH_SIZE_GUESS,
    MATCH_UNIQUE_SIZE,
    NO_CONTENT_MATCH,
    NO_SIZE_MATCH,
    SourceIndex,
)
from dp_backup.hashing import HASH_FULL, HASH_NONE, hash_file


def index_of(tmp_path, files, hash_mode=HASH_NONE):
    root = build_tree(tmp_path / "src", files)
    return SourceIndex.build(str(root), hash_mode), root


def test_no_file_of_that_size(tmp_path):
    index, _ = index_of(tmp_path, {"a.txt": "abc"})
    decision = index.match(size=999, name="a.txt")
    assert not decision.matched and decision.reason == NO_SIZE_MATCH


def test_unique_size_matches(tmp_path):
    index, _ = index_of(tmp_path, {"whatever.bin": "abc"})
    decision = index.match(size=3, name="different.txt")
    assert decision.confidence == MATCH_UNIQUE_SIZE


def test_name_beats_position(tmp_path):
    index, root = index_of(tmp_path, {"aaa.txt": "1111", "target.txt": "2222"})
    decision = index.match(size=4, name="target.txt")
    assert decision.confidence == MATCH_NAME
    assert decision.source.name == "target.txt"


def test_name_match_is_case_insensitive(tmp_path):
    index, _ = index_of(tmp_path, {"Report.TXT": "1111", "other.bin": "2222"})
    decision = index.match(size=4, name="report.txt")
    assert decision.confidence == MATCH_NAME


def test_several_files_share_name_and_size(tmp_path):
    index, _ = index_of(tmp_path, {"a/dup.txt": "1111", "b/dup.txt": "2222"})
    decision = index.match(size=4, name="dup.txt")
    assert decision.confidence == MATCH_NAME_AMBIGUOUS
    assert "share this name" in decision.detail


def test_extension_tier(tmp_path):
    index, _ = index_of(tmp_path, {"one.jpg": "1111", "two.txt": "2222"})
    decision = index.match(size=4, name="renamed.jpg")
    assert decision.confidence == MATCH_EXTENSION
    assert decision.source.name == "one.jpg"


def test_ambiguous_is_refused(tmp_path):
    index, _ = index_of(tmp_path, {"a.rec": "1111", "b.rec": "2222"})
    decision = index.match(size=4, name="wanted.dat")
    assert not decision.matched and decision.reason == AMBIGUOUS


def test_ambiguous_can_be_forced(tmp_path):
    index, _ = index_of(tmp_path, {"a.rec": "1111", "b.rec": "2222"})
    decision = index.match(size=4, name="wanted.dat", allow_ambiguous=True)
    assert decision.confidence == MATCH_SIZE_GUESS


def test_exhausted_bucket_reports_clearly(tmp_path):
    index, _ = index_of(tmp_path, {"only.dat": "1111"})
    first = index.match(size=4, name="one.dat")
    index.consume(first.source)
    second = index.match(size=4, name="two.dat")
    assert not second.matched and second.reason == AMBIGUOUS
    assert "already restored" in second.detail


def test_digest_tier_picks_the_right_file(tmp_path):
    files = {"x.bin": "1111", "y.bin": "2222"}
    index, root = index_of(tmp_path, files, hash_mode=HASH_FULL)
    wanted = hash_file(str(root / "y.bin"), HASH_FULL)

    decision = index.match(size=4, name="anything.dat", digest=wanted)
    assert decision.confidence == MATCH_CONTENT
    assert decision.source.name == "y.bin"


def test_digest_tier_reports_when_content_is_absent(tmp_path):
    index, _ = index_of(tmp_path, {"x.bin": "1111"}, hash_mode=HASH_FULL)
    decision = index.match(size=4, name="x.bin", digest="0" * 64)
    assert not decision.matched and decision.reason == NO_CONTENT_MATCH


def test_digest_tier_allows_reuse_for_duplicates(tmp_path):
    index, root = index_of(tmp_path, {"only.bin": "SAME"}, hash_mode=HASH_FULL)
    digest = hash_file(str(root / "only.bin"), HASH_FULL)

    first = index.match(size=4, name="a.bin", digest=digest)
    index.consume(first.source)
    second = index.match(size=4, name="b.bin", digest=digest)

    assert second.confidence == MATCH_CONTENT
    assert second.source is first.source
    assert "reused" in second.detail


def test_digest_tier_prefers_an_unused_copy(tmp_path):
    index, root = index_of(
        tmp_path, {"one.bin": "SAME", "two.bin": "SAME"}, hash_mode=HASH_FULL
    )
    digest = hash_file(str(root / "one.bin"), HASH_FULL)

    first = index.match(size=4, name="a.bin", digest=digest)
    index.consume(first.source)
    second = index.match(size=4, name="b.bin", digest=digest)

    assert second.source is not first.source


def test_entry_without_a_digest_falls_back_to_name(tmp_path):
    """Export can record a file it could not read; matching must still work."""
    index, _ = index_of(
        tmp_path, {"wanted.txt": "1111", "other.txt": "2222"}, hash_mode=HASH_FULL
    )
    decision = index.match(size=4, name="wanted.txt", digest="")
    assert decision.confidence == MATCH_NAME


def test_unreadable_source_file_does_not_crash_matching(tmp_path, monkeypatch):
    index, root = index_of(tmp_path, {"a.bin": "1111"}, hash_mode=HASH_FULL)
    import dp_backup.hashing as hashing

    monkeypatch.setattr(
        hashing, "open",
        lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "denied")),
        raising=False,
    )
    decision = index.match(size=4, name="a.bin", digest="0" * 64)
    assert not decision.matched
    assert index.hash_errors


def test_index_skips_symlinks_by_default(tmp_path):
    import os

    root = build_tree(tmp_path / "src", {"real.txt": "abcd"})
    os.symlink("real.txt", str(root / "alias.txt"))
    index = SourceIndex.build(str(root), HASH_NONE)
    assert index.file_count == 1


def test_index_counts_and_leftovers(tmp_path):
    index, _ = index_of(tmp_path, {"a": "1", "b": "22", "c": "333"})
    assert index.file_count == 3
    assert index.total_bytes == 6
    decision = index.match(size=1, name="a")
    index.consume(decision.source)
    assert len(list(index.unused_files())) == 2
