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
