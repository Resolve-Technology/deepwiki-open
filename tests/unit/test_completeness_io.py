import json
import os

from api.completeness_io import atomic_write_text, read_completeness_report


def _write(tmp_path, suffix, text):
    (tmp_path / f"deepwiki_cache_x{suffix}").write_text(text, encoding="utf-8")
    return str(tmp_path / "deepwiki_cache_x.json")


def test_reads_json_report(tmp_path):
    cache = _write(tmp_path, ".completeness.json",
                   json.dumps({"summary": {"headings": {"content": 1}}}))
    res = read_completeness_report(cache, "json")
    assert res is not None
    data, media = res
    assert data["summary"]["headings"]["content"] == 1
    assert media is None


def test_reads_md_report(tmp_path):
    cache = _write(tmp_path, ".completeness.md", "# Report\n\nbody\n")
    res = read_completeness_report(cache, "md")
    assert res is not None
    text, media = res
    assert text.startswith("# Report")
    assert media == "text/markdown; charset=utf-8"


def test_absent_report_returns_none(tmp_path):
    cache = str(tmp_path / "deepwiki_cache_x.json")  # no sibling files written
    assert read_completeness_report(cache, "json") is None
    assert read_completeness_report(cache, "md") is None


def test_malformed_json_returns_none(tmp_path):
    cache = _write(tmp_path, ".completeness.json", "{ not valid json ")
    assert read_completeness_report(cache, "json") is None


def test_atomic_write_creates_complete_file_no_tmp_left(tmp_path):
    dest = tmp_path / "out.json"
    atomic_write_text(str(dest), '{"a": 1}')
    assert dest.read_text() == '{"a": 1}'
    leftovers = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def _wikicache(tmp_path):
    cache = tmp_path / "deepwiki_cache_x.json"
    cache.write_text(json.dumps({
        "repo": {"owner": "poc", "repo": "x"}, "provider": "claude", "model": "haiku",
        "generated_at": "2026-07-01T00:00:00Z",
        "generated_pages": {"page-tsd-a": {"id": "page-tsd-a", "title": "A",
                                           "content": "## a (Alpha)\nreal\n"}},
    }), encoding="utf-8")
    return str(cache)


def test_self_heal_rebuilds_missing_json_from_wikicache(tmp_path):
    cache = _wikicache(tmp_path)  # no .completeness.json present
    res = read_completeness_report(cache, "json")
    assert res is not None
    data, media = res
    assert media is None
    assert "by_document" in data["summary"] and data["repo"] == "poc/x"
    # healed on disk
    assert (tmp_path / "deepwiki_cache_x.completeness.json").exists()
    assert (tmp_path / "deepwiki_cache_x.completeness.md").exists()


def test_self_heal_md(tmp_path):
    cache = _wikicache(tmp_path)
    res = read_completeness_report(cache, "md")
    assert res is not None
    text, media = res
    assert media == "text/markdown; charset=utf-8" and "TSD" in text


def test_no_report_and_no_wikicache_returns_none(tmp_path):
    assert read_completeness_report(str(tmp_path / "deepwiki_cache_x.json"), "json") is None


def test_self_heal_legacy_cache_uses_repo_url(tmp_path):
    # Old-format cache: no `repo` dict, only `repo_url`.
    cache = tmp_path / "deepwiki_cache_x.json"
    cache.write_text(json.dumps({
        "repo_url": "https://gitlab/x/y", "provider": "claude", "model": "haiku",
        "generated_at": "2026-07-01T00:00:00Z",
        "generated_pages": {"page-tsd-a": {"id": "page-tsd-a", "title": "A",
                                           "content": "## a (Alpha)\nreal\n"}},
    }), encoding="utf-8")
    data, _ = read_completeness_report(str(cache), "json")
    assert data["repo"] == "https://gitlab/x/y"  # not "None/None"
