"""Tests for citation grounding (verify citations against provided source)."""
from types import SimpleNamespace

from api.citation_grounding import (FileSource, build_source_map,
                                    parse_citation_label, resolve_citation,
                                    verify_page_citations)
from api.prompt_assembly import number_source_lines


def test_parse_citation_label_range():
    assert parse_citation_label("prog.cbl:12-34") == ("prog.cbl", 12, 34)


def test_parse_citation_label_single_line():
    assert parse_citation_label("prog.cbl:12") == ("prog.cbl", 12, None)


def test_parse_citation_label_whole_file():
    assert parse_citation_label("prog.cbl") == ("prog.cbl", None, None)


def test_parse_citation_label_rejects_non_citation():
    # No file extension -> not a citation (matches frontend CITATION_RE).
    assert parse_citation_label("see the docs") is None


def test_build_source_map_from_numbered_deep_dive():
    # Deep-dive injects line-numbered content; the map stores RAW text by line.
    numbered = number_source_lines("ALPHA\nBETA\nGAMMA")
    smap = build_source_map(numbered, "prog.cbl", [])
    fs = smap["prog.cbl"]
    assert fs.lines == {1: "ALPHA", 2: "BETA", 3: "GAMMA"}


def test_build_source_map_from_rag_chunk_with_span():
    doc = SimpleNamespace(
        text="READ-MASTER.\n    READ FILE",
        meta_data={"file_path": "PAY.cbl", "start_line": 120, "end_line": 121})
    smap = build_source_map("", "", [doc])
    assert smap["PAY.cbl"].lines == {120: "READ-MASTER.", 121: "    READ FILE"}


def test_build_source_map_rag_chunk_without_span_is_whole_file_only():
    # Old indexes carry no start_line: file is present but has no line text.
    doc = SimpleNamespace(text="whatever", meta_data={"file_path": "a.py"})
    smap = build_source_map("", "", [doc])
    assert "a.py" in smap
    assert smap["a.py"].lines == {}


def test_build_source_map_ignores_docs_without_file_path():
    doc = SimpleNamespace(text="x", meta_data={})
    smap = build_source_map("", "", [doc])
    assert smap == {}


def _smap():
    return {"prog.cbl": FileSource(lines={12: "MOVE A TO B", 13: "ADD 1 TO C"})}


def test_resolve_verified_range_returns_snippet():
    info = resolve_citation("prog.cbl:12-13", _smap())
    assert info["status"] == "verified"
    assert info["snippet"] == "MOVE A TO B\nADD 1 TO C"
    assert info["filePath"] == "prog.cbl"
    assert info["startLine"] == 12 and info["endLine"] == 13


def test_resolve_verified_single_line():
    info = resolve_citation("prog.cbl:12", _smap())
    assert info["status"] == "verified"
    assert info["snippet"] == "MOVE A TO B"


def test_resolve_broken_file_not_provided():
    info = resolve_citation("ghost.cbl:1-3", _smap())
    assert info["status"] == "broken"
    assert info["reason"] == "file not provided"
    assert info["snippet"] is None


def test_resolve_broken_lines_out_of_range():
    info = resolve_citation("prog.cbl:12-99", _smap())
    assert info["status"] == "broken"
    assert info["reason"] == "lines not in provided source"


def test_resolve_whole_file_present_is_verified_without_snippet():
    info = resolve_citation("prog.cbl", _smap())
    assert info["status"] == "verified"
    assert info["snippet"] is None


def test_resolve_whole_file_absent_is_broken():
    info = resolve_citation("ghost.cbl", _smap())
    assert info["status"] == "broken"
    assert info["reason"] == "file not provided"


def test_resolve_line_range_when_no_line_info_is_broken():
    # File present but no line text (old RAG chunk) -> ranged cite can't verify.
    info = resolve_citation("a.py:5", {"a.py": FileSource(lines={})})
    assert info["status"] == "broken"
    assert info["reason"] == "lines not in provided source"


def test_resolve_non_citation_returns_none():
    assert resolve_citation("just prose", _smap()) is None


def test_verify_page_citations_extracts_empty_href_links_only():
    content = (
        "Intro. Sources: [prog.cbl:12-13]()\n\n"
        "More. Sources: [ghost.cbl:1-2]()\n\n"
        "A real link [docs](https://example.com/x) and prose [not a cite]()."
    )
    out = verify_page_citations(content, _smap())
    assert out["prog.cbl:12-13"]["status"] == "verified"
    assert out["ghost.cbl:1-2"]["status"] == "broken"
    # Real-href link and the non-citation empty link are not included.
    assert "docs" not in out
    assert "not a cite" not in out


def test_verify_page_citations_dedupes_repeated_label():
    content = "Sources: [prog.cbl:12](). Again Sources: [prog.cbl:12]()."
    out = verify_page_citations(content, _smap())
    assert list(out.keys()) == ["prog.cbl:12"]


def test_verify_page_citations_round_trip_with_numbered_source():
    # number_source_lines -> build_source_map -> verify recovers the real lines.
    numbered = number_source_lines("FIRST LINE\nSECOND LINE\nTHIRD LINE")
    smap = build_source_map(numbered, "x.cbl", [])
    out = verify_page_citations("Sources: [x.cbl:1-2]()", smap)
    assert out["x.cbl:1-2"]["snippet"] == "FIRST LINE\nSECOND LINE"


def test_resolve_inverted_range_is_broken():
    # end < start -> empty range; must be broken, not a spuriously-verified
    # empty snippet.
    info = resolve_citation("prog.cbl:13-12", _smap())
    assert info["status"] == "broken"
    assert info["reason"] == "lines not in provided source"
    assert info["snippet"] is None
