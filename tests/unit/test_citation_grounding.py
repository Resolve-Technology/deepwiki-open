"""Tests for citation grounding (verify citations against provided source)."""
from types import SimpleNamespace

from api.citation_grounding import (FileSource, build_source_map,
                                    parse_citation_label)
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
