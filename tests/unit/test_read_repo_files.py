"""Tests for read_repo_files: load full repo file contents for citation grounding."""
import os
import tempfile

from api.data_pipeline import read_repo_files


def _write(root, rel, data, encoding="utf-8"):
    full = os.path.join(root, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding=encoding) as f:
        f.write(data)


def test_reads_files_keyed_by_relative_path():
    with tempfile.TemporaryDirectory() as d:
        _write(d, "CAL101.txt", "P1\nP2")
        _write(d, "copybook/VARCOM.txt", "A\nB\nC")
        out = read_repo_files(d, ["CAL101.txt", "copybook/VARCOM.txt"])
        assert out == {"CAL101.txt": "P1\nP2", "copybook/VARCOM.txt": "A\nB\nC"}


def test_skips_missing_files_without_raising():
    with tempfile.TemporaryDirectory() as d:
        _write(d, "real.txt", "x")
        out = read_repo_files(d, ["real.txt", "gone.txt"])
        assert out == {"real.txt": "x"}


def test_ignores_empty_paths():
    with tempfile.TemporaryDirectory() as d:
        _write(d, "a.txt", "y")
        out = read_repo_files(d, ["a.txt", "", None])
        assert out == {"a.txt": "y"}


def test_reads_non_utf8_via_latin1_fallback():
    with tempfile.TemporaryDirectory() as d:
        full = os.path.join(d, "ebcdic.txt")
        with open(full, "wb") as f:
            f.write(b"caf\xe9")  # 0xe9 is invalid UTF-8, valid latin-1
        out = read_repo_files(d, ["ebcdic.txt"])
        assert out["ebcdic.txt"] == "café"
