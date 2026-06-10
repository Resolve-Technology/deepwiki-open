"""Tests for chunk line-span computation (RAG citation accuracy).

compute_line_span derives a chunk's absolute start/end line from its splitter
`order`, using the same word-window stepping adalflow's TextSplitter uses.
"""
from types import SimpleNamespace

from api.data_pipeline import compute_line_span, attach_chunk_line_spans


# A 4-word parent spanning 3 lines. Word split on " ":
#   words = ["alpha", "beta\ngamma", "delta\nepsilon", "zeta"]
#   char offsets: alpha@0, beta\ngamma@6, delta\nepsilon@17, zeta@31
PARENT = "alpha beta\ngamma delta\nepsilon zeta"


def test_compute_line_span_first_chunk():
    assert compute_line_span(PARENT, order=0, chunk_text="alpha beta\ngamma ", step=2) == (1, 2)


def test_compute_line_span_second_chunk():
    assert compute_line_span(PARENT, order=1, chunk_text="delta\nepsilon zeta", step=2) == (2, 3)


def test_compute_line_span_order_past_end_returns_none():
    assert compute_line_span(PARENT, order=99, chunk_text="x", step=2) is None


def test_attach_chunk_line_spans_writes_fresh_per_chunk_dict():
    shared_meta = {"file_path": "p.cbl"}
    parent = SimpleNamespace(id="P1", text=PARENT)
    c0 = SimpleNamespace(parent_doc_id="P1", order=0,
                         text="alpha beta\ngamma ", meta_data=shared_meta)
    c1 = SimpleNamespace(parent_doc_id="P1", order=1,
                         text="delta\nepsilon zeta", meta_data=shared_meta)

    attach_chunk_line_spans([c0, c1], [parent], step=2)

    assert c0.meta_data["start_line"] == 1 and c0.meta_data["end_line"] == 2
    assert c1.meta_data["start_line"] == 2 and c1.meta_data["end_line"] == 3
    assert c0.meta_data is not c1.meta_data
    assert c0.meta_data["file_path"] == "p.cbl"


def test_attach_chunk_line_spans_skips_unknown_parent():
    c = SimpleNamespace(parent_doc_id="MISSING", order=0, text="x",
                        meta_data={"file_path": "p"})
    attach_chunk_line_spans([c], [], step=2)
    assert "start_line" not in c.meta_data
