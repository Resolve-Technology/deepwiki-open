"""Tests that read_all_documents indexes large source files stored as .txt.

Regression test for the README-only index bug: COBOL programs stored as
.txt fall under doc_extensions, and any doc file over 8192 tokens was
skipped wholesale — leaving the FAISS index with only the README, so every
non-deep-dive wiki page was generated from README chunks alone.
"""
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from api.data_pipeline import read_all_documents


@pytest.fixture
def repo_dir():
    """A minimal repo: small README + a large COBOL-as-.txt program.

    NOT under pytest's tmp_path — /tmp is a DEFAULT_EXCLUDED_DIRS segment,
    so should_process_file would drop every file in it.
    """
    base = Path(tempfile.mkdtemp(prefix="deepwiki-idxtest-",
                                 dir=os.path.expanduser("~")))
    (base / "README.md").write_text("# demo repo\n")
    program_dir = base / "1.BBC15 - Billing SPLITTER PROGRAM"
    program_dir.mkdir()
    # ~15k tokens, mirroring BBC15.txt (14,097 tokens) that was skipped
    line = "MOVE WS-BILLING-RECORD TO OUT-SPLIT-RECORD PERFORM 9000-WRITE\n"
    (program_dir / "BBC15.txt").write_text(line * 2200)
    yield str(base)
    shutil.rmtree(base, ignore_errors=True)


def test_large_txt_program_is_indexed(repo_dir):
    docs = read_all_documents(repo_dir)
    paths = {d.meta_data["file_path"] for d in docs}
    assert any(p.endswith("BBC15.txt") for p in paths), (
        f"large .txt program file missing from index; indexed: {paths}")


def test_cap_is_env_configurable(repo_dir, monkeypatch):
    # A 1-token cap must exclude everything, proving the env var is honored.
    monkeypatch.setenv("DEEPWIKI_MAX_INDEX_FILE_TOKENS", "1")
    import importlib
    import api.data_pipeline as dp
    importlib.reload(dp)
    try:
        docs = dp.read_all_documents(repo_dir)
        assert docs == []
    finally:
        monkeypatch.delenv("DEEPWIKI_MAX_INDEX_FILE_TOKENS")
        importlib.reload(dp)
