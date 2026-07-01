# TSD/BRD Completeness Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After each generation, verify every TSD/BRD template field is present, legitimately empty (`None`), or dropped by the model, and emit JSON + Markdown reports next to the wikicache.

**Architecture:** A pure, I/O-free checker module (`api/tsd_brd_completeness.py`) parses the required fields from `TSD_BRD_OUTLINES`, parses the generated markdown headings, and classifies each field into `content` / `empty_none` / `MISSING` (plus present/missing for enumerated rows). A best-effort integration in `run_generation` writes the two report files and logs a one-line summary.

**Tech Stack:** Python 3.12, pytest, stdlib `re`/`json`. Run tests with `.venv/bin/python -m pytest`.

## Global Constraints

- Checker module is **pure** — no file/network I/O, no imports of FastAPI/runtime state. Only `re` and `from api.wiki_prompts import TSD_BRD_OUTLINES`.
- Heading matching anchors on the **English wording in trailing parens** of a generated heading; normalization = lowercase then strip all non-`[a-z0-9]`.
- None-token set (a page body counts as empty): `none`, `無`, `無相關`, `無相關資訊`, `n/a`, `na`, `not applicable`, `-` (case-insensitive; trailing `.`/`。` stripped).
- Enumerated row checks only for sections in `ENUMERATED_SECTIONS`; v1 = `{("page-tsd-system-overview", "Impact Analysis")}`.
- Integration is **best-effort**: wrapped in `try/except`, logs a warning on failure, never fails a generation job.
- Report files share the wikicache base path: `X.json` → `X.completeness.json` and `X.completeness.md`.
- Run all tests via `.venv/bin/python -m pytest` (system `python`/`pytest` are absent).

---

### Task 1: Module scaffold — normalization, anchor, required-heading parsing

**Files:**
- Create: `api/tsd_brd_completeness.py`
- Test: `tests/unit/test_tsd_brd_completeness.py`

**Interfaces:**
- Produces: `_normalize(text: str) -> str`; `_anchor_from_heading(text: str) -> str`; `parse_required_headings(outline: str) -> list[dict]` where each dict is `{"label": str, "level": int}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_tsd_brd_completeness.py
from api.tsd_brd_completeness import (
    _normalize, _anchor_from_heading, parse_required_headings)


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_tsd_brd_completeness.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'api.tsd_brd_completeness'`

- [ ] **Step 3: Write minimal implementation**

```python
# api/tsd_brd_completeness.py
"""Completeness check for the template-driven TSD/BRD wiki pages.

Compares the generated markdown for each fixed-template page against the
required heading structure in ``TSD_BRD_OUTLINES`` and classifies every field
as present-with-content, legitimately empty (``None``), or dropped (MISSING).
Pure and I/O-free so it is fully unit-testable; the caller does all file I/O.
"""
import re

from api.wiki_prompts import TSD_BRD_OUTLINES

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_TRAILING_PARENS = re.compile(r"\(([^)]*)\)\s*$")
# Outline headings are H2/H3 only; generated pages may use H1-H6.
_OUTLINE_H = re.compile(r"^(#{2,3})\s+(.+?)\s*$")


def _normalize(text: str) -> str:
    """Lowercase and drop every non-alphanumeric character (incl. CJK/space)."""
    return _NON_ALNUM.sub("", (text or "").lower())


def _anchor_from_heading(text: str) -> str:
    """Match anchor for a heading: the English wording in trailing parens if
    present (the prompt mandates ``翻譯 (English)``), else the whole text."""
    m = _TRAILING_PARENS.search((text or "").strip())
    return _normalize(m.group(1) if m else text)


def parse_required_headings(outline: str) -> list:
    """Parse an outline block into its ordered required headings."""
    out = []
    for line in (outline or "").splitlines():
        m = _OUTLINE_H.match(line)
        if m:
            out.append({"label": m.group(2).strip(), "level": len(m.group(1))})
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_tsd_brd_completeness.py -q`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add api/tsd_brd_completeness.py tests/unit/test_tsd_brd_completeness.py
git commit -m "feat: TSD/BRD completeness — normalization + required-heading parsing"
```

---

### Task 2: Enumerated-row parsing

**Files:**
- Modify: `api/tsd_brd_completeness.py`
- Test: `tests/unit/test_tsd_brd_completeness.py`

**Interfaces:**
- Consumes: `_OUTLINE_H`, `_normalize` (Task 1).
- Produces: `ENUMERATED_SECTIONS: set[tuple[str, str]]`; `parse_required_rows(outline: str, section_label: str) -> list[str]`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/test_tsd_brd_completeness.py
from api.tsd_brd_completeness import (
    ENUMERATED_SECTIONS, parse_required_rows)
from api.wiki_prompts import TSD_BRD_OUTLINES


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_tsd_brd_completeness.py -q`
Expected: FAIL — `ImportError: cannot import name 'ENUMERATED_SECTIONS'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to api/tsd_brd_completeness.py (after parse_required_headings)

# (page_id, section English label) blocks whose outline bullets are required
# table rows. Extend this set to row-check more sections — the labels already
# live in TSD_BRD_OUTLINES, so no new data is needed.
ENUMERATED_SECTIONS = {("page-tsd-system-overview", "Impact Analysis")}


def parse_required_rows(outline: str, section_label: str) -> list:
    """The ``- `` bullet labels directly under ``## <section_label>`` (up to the
    next heading). Prose lines and bullets in other sections are ignored."""
    target = _normalize(section_label)
    rows, in_section = [], False
    for line in (outline or "").splitlines():
        m = _OUTLINE_H.match(line)
        if m:
            in_section = _normalize(m.group(2)) == target
            continue
        if in_section and line.strip().startswith("- "):
            rows.append(line.strip()[2:].strip())
    return rows
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_tsd_brd_completeness.py -q`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add api/tsd_brd_completeness.py tests/unit/test_tsd_brd_completeness.py
git commit -m "feat: TSD/BRD completeness — enumerated-row parsing"
```

---

### Task 3: Generated-heading parsing with section bodies

**Files:**
- Modify: `api/tsd_brd_completeness.py`
- Test: `tests/unit/test_tsd_brd_completeness.py`

**Interfaces:**
- Consumes: `_anchor_from_heading` (Task 1).
- Produces: `_parse_generated_headings(content: str) -> list[dict]` where each dict is `{"level": int, "anchor": str, "body": str}` in document order. `body` is the text between this heading and the next heading of level ≤ its own. Headings inside ``` fences are ignored.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/test_tsd_brd_completeness.py
from api.tsd_brd_completeness import _parse_generated_headings


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_tsd_brd_completeness.py -q`
Expected: FAIL — `ImportError: cannot import name '_parse_generated_headings'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to api/tsd_brd_completeness.py
_MD_H = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def _parse_generated_headings(content: str) -> list:
    """Headings (H1-H6) of a generated page, each with its section body.

    Body = lines after the heading up to the next heading of level <= its own
    (or end of page). Lines inside ``` code fences are not treated as headings.
    """
    lines = (content or "").splitlines()
    heads, in_fence = [], False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _MD_H.match(line)
        if m:
            heads.append({"level": len(m.group(1)),
                          "anchor": _anchor_from_heading(m.group(2)),
                          "line": i})
    for idx, h in enumerate(heads):
        end = len(lines)
        for nxt in heads[idx + 1:]:
            if nxt["level"] <= h["level"]:
                end = nxt["line"]
                break
        h["body"] = "\n".join(lines[h["line"] + 1:end]).strip()
        del h["line"]
    return heads
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_tsd_brd_completeness.py -q`
Expected: PASS (10 passed)

- [ ] **Step 5: Commit**

```bash
git add api/tsd_brd_completeness.py tests/unit/test_tsd_brd_completeness.py
git commit -m "feat: TSD/BRD completeness — generated-heading + body parsing"
```

---

### Task 4: None-detection and per-page classification

**Files:**
- Modify: `api/tsd_brd_completeness.py`
- Test: `tests/unit/test_tsd_brd_completeness.py`

**Interfaces:**
- Consumes: `parse_required_headings`, `parse_required_rows`, `ENUMERATED_SECTIONS`, `_parse_generated_headings`, `_normalize` (Tasks 1-3).
- Produces: `_is_none_body(body: str) -> bool`; `classify_page(page_id: str, content: str, outline: str) -> dict`. Page dict: `{"id": str, "present": True, "headings": [ {"label", "level", "status"} (+ "rows": [{"label","status"}] for enumerated sections) ]}`. `status` ∈ `{"content","empty_none","missing"}`; row `status` ∈ `{"present","missing"}`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/test_tsd_brd_completeness.py
from api.tsd_brd_completeness import _is_none_body, classify_page


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_tsd_brd_completeness.py -q`
Expected: FAIL — `ImportError: cannot import name '_is_none_body'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to api/tsd_brd_completeness.py
NONE_TOKENS = {"none", "無", "無相關", "無相關資訊", "n/a", "na",
               "not applicable", "-"}


def _is_none_body(body: str) -> bool:
    """True when a section body is empty or one of the none-tokens."""
    s = (body or "").strip().strip("*_`> ").strip().rstrip(".。 ").strip()
    if not s:
        return True
    return s.casefold() in {t.casefold() for t in NONE_TOKENS}


def classify_page(page_id: str, content: str, outline: str) -> dict:
    """Classify every required heading (and enumerated row) of one page."""
    gen = _parse_generated_headings(content)
    by_anchor = {}
    for h in gen:
        by_anchor.setdefault(h["anchor"], h)  # first match wins

    headings = []
    for req in parse_required_headings(outline):
        gh = by_anchor.get(_normalize(req["label"]))
        entry = {"label": req["label"], "level": req["level"]}
        if gh is None:
            entry["status"] = "missing"
        else:
            entry["status"] = "empty_none" if _is_none_body(gh["body"]) else "content"
        if (page_id, req["label"]) in ENUMERATED_SECTIONS:
            body_norm = _normalize(gh["body"]) if gh else ""
            # Guard against an empty-normalized label (e.g. a future CJK-only
            # row) matching everything via "" in body_norm.
            entry["rows"] = [
                {"label": r,
                 "status": "present" if _normalize(r) and _normalize(r) in body_norm
                           else "missing"}
                for r in parse_required_rows(outline, req["label"])]
        headings.append(entry)
    return {"id": page_id, "present": True, "headings": headings}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_tsd_brd_completeness.py -q`
Expected: PASS (13 passed)

- [ ] **Step 5: Commit**

```bash
git add api/tsd_brd_completeness.py tests/unit/test_tsd_brd_completeness.py
git commit -m "feat: TSD/BRD completeness — None-detection + per-page classification"
```

---

### Task 5: Top-level checker + summary aggregation

**Files:**
- Modify: `api/tsd_brd_completeness.py`
- Test: `tests/unit/test_tsd_brd_completeness.py`

**Interfaces:**
- Consumes: `classify_page`, `parse_required_headings`, `TSD_BRD_OUTLINES` (Tasks 1-4).
- Produces: `check_tsd_brd_completeness(pages: list[dict], outlines: dict = TSD_BRD_OUTLINES) -> dict`. Report: `{"summary": {"headings": {"content","empty_none","missing"}, "rows": {"present","missing"}, "pages_missing": [str]}, "pages": [page-report]}`. A page dict in `pages` needs `id`, `title`, `content`. Pages whose id is not in `outlines` are ignored. A page id in `outlines` but absent from `pages` → `present: False`, all headings `missing`, id added to `pages_missing`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/test_tsd_brd_completeness.py
from api.tsd_brd_completeness import check_tsd_brd_completeness


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_tsd_brd_completeness.py -q`
Expected: FAIL — `ImportError: cannot import name 'check_tsd_brd_completeness'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to api/tsd_brd_completeness.py
def check_tsd_brd_completeness(pages: list, outlines: dict = TSD_BRD_OUTLINES) -> dict:
    """Build the completeness report over the template TSD/BRD pages."""
    by_id = {p.get("id"): p for p in (pages or [])}
    headings_count = {"content": 0, "empty_none": 0, "missing": 0}
    rows_count = {"present": 0, "missing": 0}
    pages_missing, page_reports = [], []

    for pid, outline in outlines.items():
        page = by_id.get(pid)
        if page is None:
            pages_missing.append(pid)
            reqs = parse_required_headings(outline)
            page_reports.append({
                "id": pid, "title": None, "present": False,
                "headings": [{"label": r["label"], "level": r["level"],
                              "status": "missing"} for r in reqs]})
            headings_count["missing"] += len(reqs)
            continue
        report = classify_page(pid, page.get("content") or "", outline)
        report["title"] = page.get("title")
        page_reports.append(report)
        for h in report["headings"]:
            headings_count[h["status"]] += 1
            for row in h.get("rows", []):
                rows_count[row["status"]] += 1

    return {"summary": {"headings": headings_count, "rows": rows_count,
                        "pages_missing": pages_missing},
            "pages": page_reports}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_tsd_brd_completeness.py -q`
Expected: PASS (15 passed)

- [ ] **Step 5: Commit**

```bash
git add api/tsd_brd_completeness.py tests/unit/test_tsd_brd_completeness.py
git commit -m "feat: TSD/BRD completeness — top-level checker + summary"
```

---

### Task 6: Markdown report renderer + report path helper

**Files:**
- Modify: `api/tsd_brd_completeness.py`
- Test: `tests/unit/test_tsd_brd_completeness.py`

**Interfaces:**
- Consumes: report dict from `check_tsd_brd_completeness` (Task 5).
- Produces: `render_markdown_report(report: dict) -> str`; `completeness_report_paths(cache_path: str) -> tuple[str, str]` returning `(json_path, md_path)` by replacing a trailing `.json` with `.completeness.json` / `.completeness.md`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/test_tsd_brd_completeness.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_tsd_brd_completeness.py -q`
Expected: FAIL — `ImportError: cannot import name 'render_markdown_report'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to api/tsd_brd_completeness.py
_MARK = {"content": "✓", "empty_none": "○", "missing": "✗",
         "present": "✓"}


def completeness_report_paths(cache_path: str) -> tuple:
    """(json, md) report paths beside a wikicache path ending in .json."""
    base = cache_path[:-5] if cache_path.endswith(".json") else cache_path
    return base + ".completeness.json", base + ".completeness.md"


def render_markdown_report(report: dict) -> str:
    """Human-readable Markdown: summary line + per-page ✓/○/✗ tables."""
    h = report["summary"]["headings"]
    r = report["summary"]["rows"]
    lines = ["# TSD/BRD Completeness Report", "",
             f"**Headings:** {h['content']} ok / {h['empty_none']} None / "
             f"{h['missing']} MISSING",
             f"**Rows:** {r['present']} present / {r['missing']} missing", ""]
    missing_pages = report["summary"]["pages_missing"]
    if missing_pages:
        lines += ["**Pages entirely missing:** " + ", ".join(missing_pages), ""]
    for page in report["pages"]:
        title = page.get("title") or page["id"]
        lines.append(f"## {title} (`{page['id']}`)")
        if not page.get("present", True):
            lines.append("_Page missing from generated wiki._")
        for hd in page["headings"]:
            indent = "  " * (hd["level"] - 2) if hd["level"] >= 2 else ""
            lines.append(f"- {indent}{_MARK[hd['status']]} {hd['label']}")
            for row in hd.get("rows", []):
                lines.append(f"    - {_MARK[row['status']]} {row['label']}")
        lines.append("")
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_tsd_brd_completeness.py -q`
Expected: PASS (17 passed)

- [ ] **Step 5: Commit**

```bash
git add api/tsd_brd_completeness.py tests/unit/test_tsd_brd_completeness.py
git commit -m "feat: TSD/BRD completeness — markdown renderer + report paths"
```

---

### Task 7: Wire the best-effort report into run_generation

**Files:**
- Modify: `api/wiki_generator.py` (imports near line 30-40; integration after the final `save_partial` at line 563, before `progress.phase = "done"`)

**Interfaces:**
- Consumes: `check_tsd_brd_completeness`, `render_markdown_report`, `completeness_report_paths` (Tasks 5-6); existing `get_wiki_cache_path` (already imported in wiki_generator from `api.api`), `generated` (dict pid→page), `repo`, `job`.

- [ ] **Step 1: Add the imports**

`api/wiki_generator.py` does NOT currently import `json` or `datetime` (its top imports are asyncio/logging/os/re/time/xml). Add both to the stdlib import block at the top of the file:

```python
import json
from datetime import datetime, timezone
```

Then add, after the existing `from api.citation_grounding import (...)` import block:

```python
from api.tsd_brd_completeness import (check_tsd_brd_completeness,
                                      completeness_report_paths,
                                      render_markdown_report)
```

- [ ] **Step 2: Add the integration block**

In `run_generation`, replace the tail (currently):

```python
    progress.phase = "saving"
    notify()
    await save_partial(generated, structure)
    progress.phase = "done"
    notify()
```

with:

```python
    progress.phase = "saving"
    notify()
    await save_partial(generated, structure)

    # Best-effort TSD/BRD completeness report — diagnostic only, never fatal.
    try:
        report = check_tsd_brd_completeness(list(generated.values()))
        report = {"repo": f"{repo.owner}/{repo.repo}", "provider": job.provider,
                  "model": job.model,
                  "generated_at": datetime.now(timezone.utc).isoformat(),
                  **report}
        cache_path = get_wiki_cache_path(repo.owner, repo.repo, repo.type,
                                         job.language, job.provider, job.model)
        json_path, md_path = completeness_report_paths(cache_path)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(render_markdown_report(report))
        h = report["summary"]["headings"]
        r = report["summary"]["rows"]
        logger.info(f"TSD/BRD completeness [{repo.repo}]: headings "
                    f"{h['content']} ok / {h['empty_none']} None / "
                    f"{h['missing']} MISSING; rows {r['present']}/"
                    f"{r['present'] + r['missing']}")
    except Exception as e:
        logger.warning(f"TSD/BRD completeness check failed: {e}")

    progress.phase = "done"
    notify()
```

- [ ] **Step 3: Confirm the new imports are present**

Run: `grep -nE "^import json|^from datetime import|tsd_brd_completeness" api/wiki_generator.py`
Expected: `import json`, `from datetime import datetime, timezone`, and the `tsd_brd_completeness` import all present.

- [ ] **Step 4: Full test suite + import sanity**

Run: `.venv/bin/python -m pytest tests/unit/test_tsd_brd_completeness.py tests/unit/test_citation_grounding.py -q`
Expected: PASS (all)

Run: `.venv/bin/python -c "import ast; ast.parse(open('api/wiki_generator.py').read()); print('syntax OK')"`
Expected: `syntax OK`

- [ ] **Step 5: Commit**

```bash
git add api/wiki_generator.py
git commit -m "feat: write best-effort TSD/BRD completeness report after generation"
```

---

### Task 8: Bump version + end-to-end verification

**Files:**
- Modify: `src/version.ts`

- [ ] **Step 1: Bump APP_VERSION**

Edit `src/version.ts`, increment the patch (`0.3.22` → `0.3.23`).

- [ ] **Step 2: Rebuild + redeploy**

```bash
docker compose build deepwiki
docker compose up -d --no-deps deepwiki
```
Expected: image builds (exit 0); container recreated and healthy.

- [ ] **Step 3: Regenerate one repo and confirm the report appears**

Submit a `force_regenerate` job for `code1_cbl_bv401` (claude/haiku, zh-tw) via `POST /api/wiki_jobs`, wait for `done`, then:

```bash
docker exec deepwiki-open-deepwiki-1 sh -c 'ls -1 /root/.adalflow/wikicache/ | grep bv401 | grep completeness'
```
Expected: `...bv401...completeness.json` and `...completeness.md` exist.

```bash
docker logs --since 10m deepwiki-open-deepwiki-1 2>&1 | grep "TSD/BRD completeness"
```
Expected: a summary line `headings N ok / N None / N MISSING; rows N/22`.

- [ ] **Step 4: Sanity-check the report content**

```bash
docker exec deepwiki-open-deepwiki-1 sh -c 'python3 -c "import json;d=json.load(open([__import__(\"glob\").glob(\"/root/.adalflow/wikicache/*bv401*completeness.json\")][0]));print(d[\"summary\"])"'
```
Expected: a `summary` dict with `headings`, `rows`, `pages_missing`.

- [ ] **Step 5: Commit**

```bash
git add src/version.ts
git commit -m "chore: bump APP_VERSION for TSD/BRD completeness report"
```

---

## Self-Review

**Spec coverage:**
- Checker module pure + `check_tsd_brd_completeness` → Tasks 1-5. ✓
- 3-bucket classification (`content`/`empty_none`/`missing`) → Task 4. ✓
- English-in-parens matching + normalization + fallback → Tasks 1, 4. ✓
- Enumerated rows (Impact Analysis, 22) via `ENUMERATED_SECTIONS` → Tasks 2, 4, 5. ✓
- JSON + Markdown artifacts + log summary, beside wikicache → Tasks 6, 7. ✓
- Best-effort integration in `run_generation` → Task 7. ✓
- None-token set incl. CJK → Task 4 (`NONE_TOKENS`). ✓
- Whole-page-missing handling → Task 5. ✓
- Tests enumerated in spec (8 cases) → covered across Tasks 1-6. ✓
- Version bump + e2e verify → Task 8. ✓

**Placeholder scan:** none — every code/test step contains complete code.

**Type consistency:** `check_tsd_brd_completeness(pages, outlines)`, `classify_page(page_id, content, outline)`, `_parse_generated_headings(content)`, `parse_required_headings/rows`, `render_markdown_report(report)`, `completeness_report_paths(cache_path)` used identically across tasks. Page-report shape (`id`/`title`/`present`/`headings[]` with optional `rows[]`) consistent between Tasks 4, 5, 6. Status vocabularies (`content`/`empty_none`/`missing`; `present`/`missing`) consistent.

**Known v1 limitation (documented, accepted):** enumerated-row matching keys on the English label appearing in the section body. If the model fully translates a row label with no English, it will be reported `missing` — acceptable signal for v1, noted here so implementers don't "fix" it by adding fuzzy matching (out of scope).
