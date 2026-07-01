from api.tsd_brd_completeness import (
    _normalize, _anchor_from_heading, parse_required_headings,
    ENUMERATED_SECTIONS, parse_required_rows, _parse_generated_headings)
from api.wiki_prompts import TSD_BRD_OUTLINES


def test_normalize_strips_case_and_non_alnum():
    assert _normalize("System Platform!") == "systemplatform"
    assert _normalize("N/A") == "na"


def test_anchor_extracts_trailing_parens():
    # Generated heading form: "<translation> (English Wording)"
    assert _anchor_from_heading("系統平台 (System Platform)") == "systemplatform"


def test_anchor_falls_back_to_whole_text_without_parens():
    assert _anchor_from_heading("System Platform") == "systemplatform"


def test_parse_required_headings_reads_h2_and_h3():
    outline = "## System Platform\n## Security Control\n### Network\n"
    assert parse_required_headings(outline) == [
        {"label": "System Platform", "level": 2},
        {"label": "Security Control", "level": 2},
        {"label": "Network", "level": 3},
    ]


def test_parse_required_headings_ignores_prose_and_bullets():
    outline = "## Impact Analysis\nPresent as a table.\n- Existing applications\n"
    assert parse_required_headings(outline) == [
        {"label": "Impact Analysis", "level": 2}]


def test_enumerated_sections_contains_impact_analysis():
    assert ("page-tsd-system-overview", "Impact Analysis") in ENUMERATED_SECTIONS


def test_parse_required_rows_reads_bullets_until_next_heading():
    outline = ("## Impact Analysis\nProse line.\n- Existing applications\n"
               "- Existing reports\n\n## Program Flow\n- not a row\n")
    assert parse_required_rows(outline, "Impact Analysis") == [
        "Existing applications", "Existing reports"]


def test_parse_required_rows_impact_analysis_has_22_rows():
    outline = TSD_BRD_OUTLINES["page-tsd-system-overview"]
    rows = parse_required_rows(outline, "Impact Analysis")
    assert len(rows) == 22
    assert rows[0] == "Existing applications"


def test_parse_generated_headings_bodies_and_anchors():
    content = (
        "# 標題 (Title)\n"
        "## 系統平台 (System Platform)\nDB2/400 on AS400.\n"
        "### 網路 (Network)\nNone\n"
        "## 系統介面 (System Interface)\nInbound only.\n")
    heads = _parse_generated_headings(content)
    anchors = [(h["level"], h["anchor"]) for h in heads]
    assert anchors == [
        (1, "title"), (2, "systemplatform"), (3, "network"), (2, "systeminterface")]
    body = {h["anchor"]: h["body"] for h in heads}
    # An H2 body absorbs its nested H3 block (body runs to the next heading of
    # level <= its own) — so container sections aren't mislabeled empty later.
    assert body["systemplatform"] == "DB2/400 on AS400.\n### 網路 (Network)\nNone"
    assert body["network"] == "None"
    assert body["systeminterface"] == "Inbound only."


def test_parse_generated_headings_ignores_fenced_hashes():
    content = ("## Real (Real)\n```\n## Not A Heading\n```\nbody\n")
    heads = _parse_generated_headings(content)
    assert [h["anchor"] for h in heads] == ["real"]
    assert heads[0]["body"] == "```\n## Not A Heading\n```\nbody"


from api.tsd_brd_completeness import (
    _is_none_body, classify_page, check_tsd_brd_completeness)


def test_is_none_body_variants():
    assert _is_none_body("") is True
    assert _is_none_body("None") is True
    assert _is_none_body("無") is True
    assert _is_none_body("N/A。") is True
    assert _is_none_body("DB2/400 on AS400.") is False


def test_classify_page_content_none_and_missing():
    outline = "## System Platform\n## Program Flow\n## System Interface\n"
    content = ("## 系統平台 (System Platform)\nDB2/400 on AS400.\n"
               "## 程式流程 (Program Flow)\nNone\n")  # System Interface dropped
    page = classify_page("page-tsd-system-overview", content, outline)
    statuses = {h["label"]: h["status"] for h in page["headings"]}
    assert statuses == {"System Platform": "content",
                        "Program Flow": "empty_none",
                        "System Interface": "missing"}


def test_classify_page_enumerated_rows_present_and_missing():
    outline = ("## Impact Analysis\nPresent as a table.\n"
               "- Existing applications\n- Existing reports\n")
    content = ("## 影響分析 (Impact Analysis)\n"
               "| Item | Impact | Remarks |\n|---|---|---|\n"
               "| Existing applications | None | |\n")  # Existing reports missing
    page = classify_page("page-tsd-system-overview", content, outline)
    rows = {r["label"]: r["status"] for r in page["headings"][0]["rows"]}
    assert rows == {"Existing applications": "present",
                    "Existing reports": "missing"}


def test_check_completeness_summary_and_missing_page():
    outlines = {
        "page-a": "## Alpha\n## Beta\n",
        "page-b": "## Gamma\n",   # this page will be absent from `pages`
    }
    pages = [
        {"id": "page-a", "title": "A", "content":
            "## a (Alpha)\nreal content\n## b (Beta)\nNone\n"},
        {"id": "page-ignored", "title": "X", "content": "## whatever\n"},
    ]
    report = check_tsd_brd_completeness(pages, outlines)
    assert report["summary"]["headings"] == {
        "content": 1, "empty_none": 1, "missing": 1}
    assert report["summary"]["pages_missing"] == ["page-b"]
    ids = [p["id"] for p in report["pages"]]
    assert ids == ["page-a", "page-b"]           # ignored page excluded, order preserved
    assert report["pages"][1]["present"] is False


def test_check_completeness_aggregates_rows():
    outlines = {"page-tsd-system-overview":
                "## Impact Analysis\n- Existing applications\n- Existing reports\n"}
    pages = [{"id": "page-tsd-system-overview", "title": "SO", "content":
              "## x (Impact Analysis)\n| Existing applications | None | |\n"}]
    report = check_tsd_brd_completeness(pages, outlines)
    assert report["summary"]["rows"] == {"present": 1, "missing": 1}


from api.tsd_brd_completeness import (
    render_markdown_report, completeness_report_paths)


def test_completeness_report_paths_derives_siblings():
    j, m = completeness_report_paths("/x/deepwiki_cache_foo.json")
    assert j == "/x/deepwiki_cache_foo.completeness.json"
    assert m == "/x/deepwiki_cache_foo.completeness.md"


def test_render_markdown_report_marks_statuses():
    report = {
        "summary": {"headings": {"content": 1, "empty_none": 1, "missing": 1},
                    "rows": {"present": 1, "missing": 1},
                    "pages_missing": ["page-b"]},
        "pages": [
            {"id": "page-a", "title": "A", "present": True, "headings": [
                {"label": "Alpha", "level": 2, "status": "content"},
                {"label": "Beta", "level": 2, "status": "empty_none"},
                {"label": "Impact Analysis", "level": 2, "status": "content",
                 "rows": [{"label": "Existing applications", "status": "present"},
                          {"label": "Existing reports", "status": "missing"}]}]},
            {"id": "page-b", "title": None, "present": False, "headings": [
                {"label": "Gamma", "level": 2, "status": "missing"}]}]}
    md = render_markdown_report(report)
    assert "1 ok / 1 None / 1 MISSING" in md
    assert "✓ Alpha" in md
    assert "○ Beta" in md
    assert "✗ Gamma" in md
    assert "✗ Existing reports" in md
    assert "page-b" in md  # missing page listed
