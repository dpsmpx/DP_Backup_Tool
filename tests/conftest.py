"""Shared fixtures and helpers for the test suite."""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def write(path, content: str | bytes = ""):
    """Create a file (and its parents) with the given content."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    mode = "wb" if isinstance(content, bytes) else "w"
    kwargs = {} if isinstance(content, bytes) else {"encoding": "utf-8"}
    with open(path, mode, **kwargs) as handle:
        handle.write(content)
    return path


def build_tree(root, files: dict[str, str | bytes]):
    """Create a tree from a {relative path: content} mapping."""
    os.makedirs(root, exist_ok=True)
    for rel, content in files.items():
        write(os.path.join(root, rel), content)
    return root


def structure_json(path, items, hash_mode="none", version=2):
    """Write a hand-made structure file, for testing the loader's defences."""
    payload = {
        "format": "dp_backup_structure",
        "version": version,
        "hash_mode": hash_mode,
        "items": items,
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return str(path)


def read(path) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def tree_contents(root) -> dict[str, str]:
    """Every regular file under *root*, as {relative posix path: content}."""
    out = {}
    for dirpath, _, filenames in os.walk(root):
        for filename in filenames:
            full = os.path.join(dirpath, filename)
            if os.path.islink(full):
                continue
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            with open(full, "r", encoding="utf-8", errors="replace") as handle:
                out[rel] = handle.read()
    return out


@pytest.fixture
def dirs(tmp_path):
    """Three ready-made directories: original, source and destination."""
    original = tmp_path / "original"
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    for path in (original, source, destination):
        path.mkdir()
    return original, source, destination
