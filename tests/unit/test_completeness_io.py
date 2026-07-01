import json

from api.completeness_io import read_completeness_report


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
