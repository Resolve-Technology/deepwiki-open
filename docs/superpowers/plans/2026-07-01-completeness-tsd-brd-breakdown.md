# TSD/BRD Completeness Breakdown + Bilingual Rows — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Report TSD and BRD completeness as two separate blocks (ok/None/dropped each) across the checker JSON, Markdown report, log line, and Jobs-panel UI; make Impact-Analysis rows match on translated wikis; and add a guard that the required-header list stays complete against the PCALT templates.

**Architecture:** Bucket every checked page by its `page-tsd-*`/`page-brd-*` id prefix into a `by_document` summary (grand totals kept for backward compat). A prompt-only change makes required table rows bilingual (`翻譯 (English)`) so the existing English match catches them. A committed `TSD_BRD_TEMPLATE_HEADERS` constant + guard test locks header coverage without depending on the untracked `.docx` files.

**Tech Stack:** Python 3.12 / FastAPI, Next.js / React / TypeScript. Backend tests: `.venv/bin/python -m pytest`. Frontend tests: vitest in a `node:20-slim` container.

## Global Constraints

- Bucket by id prefix: `page-tsd-*` → "TSD", `page-brd-*` → "BRD". `by_document` always contains both keys, zero-initialized.
- Keep the existing top-level `summary.headings` and `summary.rows` grand totals unchanged (backward compatibility with the deployed UI).
- Bilingual-row rule: required template table rows must be written `<lang> translation FOLLOWED BY the exact English label in parentheses` — same form headings already use. No checker matching change.
- The `.docx` templates are untracked; the guard test checks the committed `TSD_BRD_TEMPLATE_HEADERS` constant, never reads `.docx` at test time.
- Backend tests: `.venv/bin/python -m pytest tests/unit/<file> -q`. Frontend: `docker run --rm -v "$PWD":/app -w /app node:20-slim sh -c "npx vitest run <file>"`.
- Do not import `api.api` in a unit test (unwritable-log-hostile in this sandbox).

---

### Task 1: `by_document` bucketing in the checker

**Files:**
- Modify: `api/tsd_brd_completeness.py` (`check_tsd_brd_completeness`, add `_document_of`)
- Test: `tests/unit/test_tsd_brd_completeness.py`

**Interfaces:**
- Produces: `_document_of(page_id: str) -> "TSD" | "BRD" | None`; `check_tsd_brd_completeness` now returns `summary.by_document = {"TSD": {"headings": {content,empty_none,missing}, "rows": {present,missing}}, "BRD": {...}}` in addition to the unchanged grand totals.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/test_tsd_brd_completeness.py
from api.tsd_brd_completeness import _document_of


def test_document_of_classifies_by_prefix():
    assert _document_of("page-tsd-scope") == "TSD"
    assert _document_of("page-brd-reference") == "BRD"
    assert _document_of("page-wiki-overview") is None


def test_by_document_buckets_tsd_and_brd_independently():
    outlines = {
        "page-tsd-a": "## Alpha\n## Beta\n",
        "page-brd-b": "## Gamma\n",
    }
    pages = [
        {"id": "page-tsd-a", "title": "A",
         "content": "## a (Alpha)\nreal\n## b (Beta)\nNone\n"},
        # page-brd-b absent -> its heading counts missing under BRD
    ]
    rep = check_tsd_brd_completeness(pages, outlines)
    bd = rep["summary"]["by_document"]
    assert bd["TSD"]["headings"] == {"content": 1, "empty_none": 1, "missing": 0}
    assert bd["BRD"]["headings"] == {"content": 0, "empty_none": 0, "missing": 1}
    # grand totals still equal the sum of both documents
    assert rep["summary"]["headings"] == {"content": 1, "empty_none": 1, "missing": 1}


def test_by_document_buckets_rows_under_tsd():
    outlines = {"page-tsd-system-overview":
                "## Impact Analysis\n- Existing applications\n- Existing reports\n"}
    pages = [{"id": "page-tsd-system-overview", "title": "SO",
              "content": "## x (Impact Analysis)\n| Existing applications | None | |\n"}]
    rep = check_tsd_brd_completeness(pages, outlines)
    assert rep["summary"]["by_document"]["TSD"]["rows"] == {"present": 1, "missing": 1}
    assert rep["summary"]["by_document"]["BRD"]["rows"] == {"present": 0, "missing": 0}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_tsd_brd_completeness.py -q`
Expected: FAIL — `ImportError: cannot import name '_document_of'`

- [ ] **Step 3: Write minimal implementation**

Add `_document_of` above `check_tsd_brd_completeness`:

```python
def _document_of(page_id: str) -> "str | None":
    """Which template document a page id belongs to."""
    if page_id.startswith("page-tsd-"):
        return "TSD"
    if page_id.startswith("page-brd-"):
        return "BRD"
    return None
```

Replace the body of `check_tsd_brd_completeness` with the version that also fills `by_document`:

```python
def check_tsd_brd_completeness(pages: list, outlines: dict = TSD_BRD_OUTLINES) -> dict:
    """Build the completeness report over the template TSD/BRD pages."""
    by_id = {p.get("id"): p for p in (pages or [])}
    headings_count = {"content": 0, "empty_none": 0, "missing": 0}
    rows_count = {"present": 0, "missing": 0}
    by_document = {
        "TSD": {"headings": {"content": 0, "empty_none": 0, "missing": 0},
                "rows": {"present": 0, "missing": 0}},
        "BRD": {"headings": {"content": 0, "empty_none": 0, "missing": 0},
                "rows": {"present": 0, "missing": 0}},
    }
    pages_missing, page_reports = [], []

    for pid, outline in outlines.items():
        doc = _document_of(pid)
        page = by_id.get(pid)
        if page is None:
            pages_missing.append(pid)
            reqs = parse_required_headings(outline)
            page_reports.append({
                "id": pid, "title": None, "present": False,
                "headings": [{"label": r["label"], "level": r["level"],
                              "status": "missing"} for r in reqs]})
            headings_count["missing"] += len(reqs)
            if doc:
                by_document[doc]["headings"]["missing"] += len(reqs)
            continue
        report = classify_page(pid, page.get("content") or "", outline)
        report["title"] = page.get("title")
        page_reports.append(report)
        for h in report["headings"]:
            headings_count[h["status"]] += 1
            if doc:
                by_document[doc]["headings"][h["status"]] += 1
            for row in h.get("rows", []):
                rows_count[row["status"]] += 1
                if doc:
                    by_document[doc]["rows"][row["status"]] += 1

    return {"summary": {"headings": headings_count, "rows": rows_count,
                        "pages_missing": pages_missing, "by_document": by_document},
            "pages": page_reports}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_tsd_brd_completeness.py -q`
Expected: PASS (all, incl. the 3 new tests)

- [ ] **Step 5: Commit**

```bash
git add api/tsd_brd_completeness.py tests/unit/test_tsd_brd_completeness.py
git commit -m "feat: per-document (TSD/BRD) breakdown in completeness summary"
```

---

### Task 2: Group the Markdown report by TSD/BRD

**Files:**
- Modify: `api/tsd_brd_completeness.py` (`render_markdown_report`)
- Test: `tests/unit/test_tsd_brd_completeness.py`

**Interfaces:**
- Consumes: `summary.by_document`, `_document_of` (Task 1).
- Produces: `render_markdown_report(report)` output grouped under `## TSD` / `## BRD` sections, each with that document's summary line; per-page blocks use `### {title}`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/test_tsd_brd_completeness.py
def test_render_markdown_groups_by_document():
    # Fixture exercises all three heading marks (✓/○/✗) AND nested row marks
    # (✓/✗), so it fully replaces the old flat-format renderer test.
    report = {
        "summary": {
            "headings": {"content": 1, "empty_none": 1, "missing": 1},
            "rows": {"present": 1, "missing": 1}, "pages_missing": ["page-brd-b"],
            "by_document": {
                "TSD": {"headings": {"content": 1, "empty_none": 1, "missing": 0},
                        "rows": {"present": 1, "missing": 1}},
                "BRD": {"headings": {"content": 0, "empty_none": 0, "missing": 1},
                        "rows": {"present": 0, "missing": 0}}}},
        "pages": [
            {"id": "page-tsd-a", "title": "A", "present": True, "headings": [
                {"label": "Alpha", "level": 2, "status": "content", "rows": [
                    {"label": "Existing applications", "status": "present"},
                    {"label": "Existing reports", "status": "missing"}]},
                {"label": "Beta", "level": 2, "status": "empty_none"}]},
            {"id": "page-brd-b", "title": None, "present": False, "headings": [
                {"label": "Gamma", "level": 2, "status": "missing"}]}]}
    md = render_markdown_report(report)
    assert "## TSD" in md
    assert "## BRD" in md
    # each document header carries its own summary line
    assert "1 ok / 1 None / 0 MISSING (rows 1/2)" in md   # TSD
    assert "0 ok / 0 None / 1 MISSING (rows 0/0)" in md    # BRD
    # TSD section appears before BRD, and page A renders under TSD
    assert md.index("## TSD") < md.index("### A") < md.index("## BRD")
    assert "✓ Alpha" in md          # content mark
    assert "○ Beta" in md           # empty_none mark
    assert "✗ Gamma" in md          # missing mark
    assert "✓ Existing applications" in md   # nested row present
    assert "✗ Existing reports" in md        # nested row missing
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_tsd_brd_completeness.py::test_render_markdown_groups_by_document -q`
Expected: FAIL (no `## TSD` grouping yet)

- [ ] **Step 3: Write minimal implementation**

Replace `render_markdown_report` with the grouped version:

```python
def _render_page(lines: list, page: dict) -> None:
    title = page.get("title") or page["id"]
    lines.append(f"### {title} (`{page['id']}`)")
    if not page.get("present", True):
        lines.append("_Page missing from generated wiki._")
    for hd in page["headings"]:
        indent = "  " * (hd["level"] - 2) if hd["level"] >= 2 else ""
        lines.append(f"- {indent}{_MARK[hd['status']]} {hd['label']}")
        for row in hd.get("rows", []):
            lines.append(f"    - {_MARK[row['status']]} {row['label']}")
    lines.append("")


def render_markdown_report(report: dict) -> str:
    """Human-readable Markdown grouped into TSD and BRD sections."""
    bd = report["summary"]["by_document"]
    lines = ["# TSD/BRD Completeness Report", ""]
    missing_pages = report["summary"]["pages_missing"]
    if missing_pages:
        lines += ["**Pages entirely missing:** " + ", ".join(missing_pages), ""]
    for doc in ("TSD", "BRD"):
        h = bd[doc]["headings"]
        r = bd[doc]["rows"]
        lines.append(f"## {doc} — {h['content']} ok / {h['empty_none']} None / "
                     f"{h['missing']} MISSING (rows {r['present']}/"
                     f"{r['present'] + r['missing']})")
        lines.append("")
        for page in report["pages"]:
            if _document_of(page["id"]) == doc:
                _render_page(lines, page)
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

The pre-existing test `test_render_markdown_report_marks_statuses`
(`tests/unit/test_tsd_brd_completeness.py`, ~line 152) builds a report fixture
**without** `summary.by_document` and asserts the old flat header line
`"1 ok / 1 None / 1 MISSING"`. The new `render_markdown_report` reads
`summary.by_document`, so that test would now `KeyError`. **Delete that test** —
the new `test_render_markdown_groups_by_document` (Step 1) supersedes it (it
covers ✓/○/✗ marks, nested rows via the page it renders, and the missing page).

Run: `.venv/bin/python -m pytest tests/unit/test_tsd_brd_completeness.py -q`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add api/tsd_brd_completeness.py tests/unit/test_tsd_brd_completeness.py
git commit -m "feat: group completeness Markdown report by TSD/BRD"
```

---

### Task 3: Split the log line by document

**Files:**
- Modify: `api/wiki_generator.py` (the log line in the best-effort completeness block, ~line 584-589)

**Interfaces:**
- Consumes: `report["summary"]["by_document"]` (Task 1).

- [ ] **Step 1: Replace the log line**

In `api/wiki_generator.py`, replace:

```python
        h = report["summary"]["headings"]
        r = report["summary"]["rows"]
        logger.info(f"TSD/BRD completeness [{repo.repo}]: headings "
                    f"{h['content']} ok / {h['empty_none']} None / "
                    f"{h['missing']} MISSING; rows {r['present']}/"
                    f"{r['present'] + r['missing']}")
```

with:

```python
        bd = report["summary"]["by_document"]
        def _doc_summary(d: dict) -> str:
            hh, rr = d["headings"], d["rows"]
            return (f"{hh['content']} ok / {hh['empty_none']} None / "
                    f"{hh['missing']} dropped (rows {rr['present']}/"
                    f"{rr['present'] + rr['missing']})")
        logger.info(f"TSD/BRD completeness [{repo.repo}]: "
                    f"TSD {_doc_summary(bd['TSD'])} · BRD {_doc_summary(bd['BRD'])}")
```

- [ ] **Step 2: Verify syntax + no regression**

Run: `.venv/bin/python -c "import ast; ast.parse(open('api/wiki_generator.py').read()); print('syntax OK')"`
Expected: `syntax OK`

Run: `.venv/bin/python -m pytest tests/unit/test_tsd_brd_completeness.py -q`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add api/wiki_generator.py
git commit -m "feat: split completeness log line into TSD and BRD"
```

---

### Task 4: Bilingual required table rows (prompt)

**Files:**
- Modify: `api/wiki_prompts.py` (`build_page_prompt`, the `outline_clause`)
- Test: `tests/unit/test_wiki_prompts.py`, `tests/unit/test_tsd_brd_completeness.py`

**Interfaces:**
- Consumes: existing `build_page_prompt(page_title, file_paths, language, deep_dive, repo_url, repo_type, default_branch, required_outline=...)`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/unit/test_wiki_prompts.py
from api.wiki_prompts import build_page_prompt, TSD_BRD_OUTLINES


def test_outline_clause_requires_bilingual_table_rows():
    prompt = build_page_prompt(
        "系統概觀 (System Overview)", ["BV401.txt"], "zh-tw", False,
        "https://x/repo", "gitlab", "main",
        required_outline=TSD_BRD_OUTLINES["page-tsd-system-overview"])
    # the clause must instruct that required TABLE ROWS also carry the English
    # label in parentheses, like headings
    assert "table row" in prompt.lower()
    assert "in parentheses" in prompt.lower()
```

```python
# append to tests/unit/test_tsd_brd_completeness.py
def test_bilingual_row_cell_is_matched_present():
    # A row cell written as "<translation> (English)" must match the English label.
    outline = ("## Impact Analysis\n- Existing applications\n")
    content = ("## 影響分析 (Impact Analysis)\n"
               "| 項目 | Impact |\n|---|---|\n"
               "| 現有應用程式 (Existing applications) | None |\n")
    page = classify_page("page-tsd-system-overview", content, outline)
    rows = {r["label"]: r["status"] for r in page["headings"][0]["rows"]}
    assert rows == {"Existing applications": "present"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_wiki_prompts.py::test_outline_clause_requires_bilingual_table_rows tests/unit/test_tsd_brd_completeness.py::test_bilingual_row_cell_is_matched_present -q`
Expected: the prompt test FAILS (instruction absent); the row-match test PASSES already (proves the checker needs no change — the fix is the prompt making rows carry the English label).

- [ ] **Step 3: Add the instruction to `outline_clause`**

In `api/wiki_prompts.py`, inside the `if required_outline:` block, extend the clause string. Change the sentence that ends `"...You MAY add extra detail or sub-headings under them where the source supports it.\n"` to also cover rows — insert this sentence immediately before `"[TEMPLATE]\n"`:

```python
            "If the template lists REQUIRED TABLE ROWS (e.g. the Impact Analysis "
            "items), write each row's label cell the SAME bilingual way: the "
            f"{lang_name} translation FOLLOWED BY the exact English label in "
            "parentheses, e.g. `現有應用程式 (Existing applications)`, so every "
            "required row is present and matchable.\n"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_wiki_prompts.py tests/unit/test_tsd_brd_completeness.py -q`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add api/wiki_prompts.py tests/unit/test_wiki_prompts.py tests/unit/test_tsd_brd_completeness.py
git commit -m "feat: require bilingual labels on template table rows so they match"
```

---

### Task 5: Header-coverage guard against the PCALT templates

**Files:**
- Modify: `api/tsd_brd_completeness.py` (add `TSD_BRD_TEMPLATE_HEADERS`, `_AUDIT_ALIASES`, `uncovered_template_headers`)
- Test: `tests/unit/test_tsd_brd_completeness.py`

**Interfaces:**
- Consumes: `_normalize`, `parse_required_headings`, `_document_of`, `TSD_BRD_OUTLINES` (existing).
- Produces: `TSD_BRD_TEMPLATE_HEADERS: dict[str, list[str]]`; `uncovered_template_headers(headers=TSD_BRD_TEMPLATE_HEADERS, outlines=TSD_BRD_OUTLINES) -> dict[str, list[str]]` returning, per document, template headers NOT covered by the outlines (empty dict when fully covered).

The canonical lists below were extracted from the PCALT template TOCs (with the example-placeholder rows — `Physical file XXXXPF …`, `Program1/2/3`, `FR-0001/0002` — excluded, since those map to the outline's generic PF/LF/program-inventory/FR-table headers).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/test_tsd_brd_completeness.py
from api.tsd_brd_completeness import (TSD_BRD_TEMPLATE_HEADERS,
                                      uncovered_template_headers)


def test_template_headers_constant_has_both_documents():
    assert set(TSD_BRD_TEMPLATE_HEADERS) == {"TSD", "BRD"}
    assert "Impact Analysis" in TSD_BRD_TEMPLATE_HEADERS["TSD"]
    assert "Reference" in TSD_BRD_TEMPLATE_HEADERS["BRD"]


def test_outline_covers_every_template_header():
    # Every intended PCALT template header must be represented in TSD_BRD_OUTLINES
    # (as an H2/H3 label or a page id). If this fails, add the missing header.
    assert uncovered_template_headers() == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_tsd_brd_completeness.py -k template -q`
Expected: FAIL — `ImportError: cannot import name 'TSD_BRD_TEMPLATE_HEADERS'`

- [ ] **Step 3: Write minimal implementation**

Add these definitions in `api/tsd_brd_completeness.py` immediately before
`check_tsd_brd_completeness` (after `_document_of` from Task 1):

```python
# Canonical intended headers, extracted once from the PCALT TSD/BRD template
# TOCs (PCALT_TSD_Template / PCALT_BRD_Template). Committed here so the coverage
# guard does not depend on the untracked .docx files. Example-placeholder rows
# (Physical file XXXXPF, Program1/2/3, FR-0001/0002) are intentionally excluded.
TSD_BRD_TEMPLATE_HEADERS = {
    "TSD": [
        "Introduction", "Scope", "Assumptions", "Inclusions", "Exclusions",
        "Constraints", "Functional Specification", "Functional Requirements",
        "Non-Functional Requirements", "System Overview", "System Platform",
        "Impact Analysis", "Program Flow", "Security Control",
        "Identity and Access Management", "Log and Event Management",
        "Encryption", "Network", "Database Security", "Application Security",
        "General Security", "System Interface", "Database Design/Change",
        "Table Change", "Program Change", "Schedule Change", "Appendix",
    ],
    "BRD": [
        "Background", "Boundaries", "Business Requirements",
        "Functional Requirements", "Non-Functional Requirements",
        "Security Control", "Reference", "Scope", "Assumptions", "Constraints",
        "Current Processing", "Requirement Specification",
        "Business Flow Diagram", "Data Archive and Housekeeping",
        "Performance Requirements", "Capacity Requirements",
        "Availability Requirements", "Reliability Requirements",
        "Usability Requirements", "Other Requirements",
        "Identity and Access Management", "Log and Event Management",
        "Encryption", "Network", "Database Security", "Application Security",
        "General Security", "Definition of Terminologies", "Attachment",
    ],
}

# A few template headers use different wording than the outline/page ids they map
# to. Map template header -> a term the outline uses, so coverage matches.
_AUDIT_ALIASES = {
    "Program Change": "program inventory",
    "Schedule Change": "schedule batch processing",
}


def _coverage_tokens(document: str, outlines: dict) -> set:
    """Normalized tokens that can satisfy a template header for a document:
    every H2/H3 label plus each page-id tail."""
    toks = set()
    for pid, outline in outlines.items():
        if _document_of(pid) != document:
            continue
        toks.add(_normalize(pid.replace("page-tsd-", "").replace("page-brd-", "")))
        for h in parse_required_headings(outline):
            toks.add(_normalize(h["label"]))
    return {t for t in toks if t}


def uncovered_template_headers(headers: dict = TSD_BRD_TEMPLATE_HEADERS,
                               outlines: dict = TSD_BRD_OUTLINES) -> dict:
    """Per document, the template headers NOT represented in the outlines.
    A header is covered when its normalized form (or its alias's) is a substring
    of some coverage token, or vice versa. Empty dict => fully covered.

    The bidirectional substring match is intentionally lenient so template
    wording like "Table Change" matches the outline's "Table Changes" and
    page-id tails ("functionalspec" ⊂ "functionalspecification") count. This
    trades some strictness for tolerance of minor wording drift; it is verified
    to return {} against the current outlines. If a future edit needs a tighter
    guard, switch to exact-match + an expanded alias table."""
    out = {}
    for doc, hs in headers.items():
        toks = _coverage_tokens(doc, outlines)
        missing = []
        for h in hs:
            cand = _normalize(_AUDIT_ALIASES.get(h, h))
            if not any(cand in t or t in cand for t in toks):
                missing.append(h)
        if missing:
            out[doc] = missing
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_tsd_brd_completeness.py -q`
Expected: PASS. If `test_outline_covers_every_template_header` reports uncovered headers, add each missing header to the correct page's outline in `api/wiki_prompts.py` (`TSD_BRD_OUTLINES`) as an `## `/`### ` line, then re-run. (Manual reconciliation indicates the outline is already complete, so this should pass without outline edits.)

- [ ] **Step 5: Commit**

```bash
git add api/tsd_brd_completeness.py tests/unit/test_tsd_brd_completeness.py
git commit -m "feat: guard that TSD_BRD_OUTLINES covers every PCALT template header"
```

---

### Task 6: TSD/BRD split in the Jobs-panel UI

**Files:**
- Modify: `src/components/CompletenessSummary.tsx`
- Test: `src/components/CompletenessSummary.test.tsx`

**Interfaces:**
- Consumes: `report.summary.by_document` (Task 1 JSON shape).
- Produces: `CompletenessReport` interface extended with optional `summary.by_document`; `CompletenessSummary` renders a TSD line, a BRD line, an `Impact rows: p/t` line, and the report link when `by_document` is present; falls back to the single grand-total line otherwise.

- [ ] **Step 1: Write the failing test**

```tsx
// append to src/components/CompletenessSummary.test.tsx
const byDocReport: CompletenessReport = {
  summary: {
    headings: { content: 48, empty_none: 4, missing: 1 },
    rows: { present: 20, missing: 2 }, pages_missing: [],
    by_document: {
      TSD: { headings: { content: 30, empty_none: 2, missing: 1 },
             rows: { present: 20, missing: 2 } },
      BRD: { headings: { content: 18, empty_none: 2, missing: 0 },
             rows: { present: 0, missing: 0 } },
    },
  },
};

describe('CompletenessSummary by_document', () => {
  it('renders separate TSD and BRD lines with an Impact rows line', () => {
    const html = renderToStaticMarkup(
      React.createElement(CompletenessSummary, { report: byDocReport, mdHref: '/x?format=md' }));
    expect(html).toContain('TSD:');
    expect(html).toContain('30 ok / 2 None /');
    expect(html).toContain('BRD:');
    expect(html).toContain('18 ok / 2 None /');
    expect(html).toContain('Impact rows: 20/22');
    expect(html).toMatch(/text-red-600/);          // TSD missing=1 and rows missing>0
    expect(html).toContain('view full report');
  });

  it('falls back to the grand-total line when by_document is absent', () => {
    const legacy: CompletenessReport = {
      summary: { headings: { content: 45, empty_none: 6, missing: 1 },
                 rows: { present: 21, missing: 1 }, pages_missing: [] } };
    const html = renderToStaticMarkup(
      React.createElement(CompletenessSummary, { report: legacy, mdHref: '/x?format=md' }));
    expect(html).toContain('Completeness: 45 ok / 6 None /');
    expect(html).not.toContain('TSD:');
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker run --rm -v "$PWD":/app -w /app node:20-slim sh -c "npx vitest run src/components/CompletenessSummary.test.tsx"`
Expected: FAIL (no TSD/BRD lines; `by_document` not on the type).

- [ ] **Step 3: Write minimal implementation**

Replace `CompletenessSummary.tsx` contents from the `CompletenessReport` interface through the component with:

```tsx
interface DocCounts {
  headings: { content: number; empty_none: number; missing: number };
  rows: { present: number; missing: number };
}

export interface CompletenessReport {
  summary: {
    headings: { content: number; empty_none: number; missing: number };
    rows: { present: number; missing: number };
    pages_missing: string[];
    by_document?: { TSD: DocCounts; BRD: DocCounts };
  };
}
```

(Keep `completenessQuery` unchanged.) Replace the component body:

```tsx
const DocLine: React.FC<{ label: string; d: DocCounts; showRows?: boolean }> = ({ label, d, showRows }) => {
  const h = d.headings;
  const rowTotal = d.rows.present + d.rows.missing;
  return (
    <div>
      {label}: {h.content} ok / {h.empty_none} None{' / '}
      <span className={h.missing > 0 ? 'text-red-600 font-medium' : ''}>{h.missing} ✗</span>
      {showRows && rowTotal > 0 && (
        <>
          {'  ·  '}
          <span className={d.rows.missing > 0 ? 'text-red-600 font-medium' : ''}>
            Impact rows: {d.rows.present}/{rowTotal}
          </span>
        </>
      )}
    </div>
  );
};

export const CompletenessSummary: React.FC<{
  report: CompletenessReport | null;
  mdHref: string;
}> = ({ report, mdHref }) => {
  if (!report) return null;
  const bd = report.summary.by_document;
  const link = (
    <a href={mdHref} target="_blank" rel="noopener noreferrer"
       className="text-[var(--accent-primary)] hover:underline">view full report ↗</a>
  );
  if (bd) {
    return (
      <div className="mt-1 text-[11px] text-[var(--muted)]">
        {/* Only TSD has enumerated rows today (Impact Analysis is the sole
            ENUMERATED_SECTIONS entry); showRows is passed to the TSD line only. */}
        <DocLine label="TSD" d={bd.TSD} showRows />
        <DocLine label="BRD" d={bd.BRD} />
        <div>{link}</div>
      </div>
    );
  }
  const h = report.summary.headings;
  return (
    <div className="mt-1 text-[11px] text-[var(--muted)]">
      Completeness: {h.content} ok / {h.empty_none} None{' / '}
      <span className={h.missing > 0 ? 'text-red-600 font-medium' : ''}>{h.missing} ✗</span>
      {' · '}{link}
    </div>
  );
};
```

- [ ] **Step 4: Run test + type-check**

Run: `docker run --rm -v "$PWD":/app -w /app node:20-slim sh -c "npx vitest run src/components/CompletenessSummary.test.tsx"`
Expected: PASS (all — the earlier grand-total render test still passes via the fallback branch).

Run: `docker run --rm -v "$PWD":/app -w /app node:20-slim sh -c "npx tsc --noEmit -p tsconfig.json 2>&1 | grep -E 'CompletenessSummary|JobsPanel|error TS' | head"`
Expected: no errors referencing `CompletenessSummary.tsx` or `JobsPanel.tsx`.

- [ ] **Step 5: Commit**

```bash
git add src/components/CompletenessSummary.tsx src/components/CompletenessSummary.test.tsx
git commit -m "feat: show TSD/BRD split + Impact rows in the Jobs-panel completeness summary"
```

---

### Task 7: Version bump + end-to-end verification

**Files:**
- Modify: `src/version.ts`

- [ ] **Step 1: Bump APP_VERSION**

Edit `src/version.ts`, increment the patch (`0.3.24` → `0.3.25`).

- [ ] **Step 2: Rebuild + redeploy**

```bash
docker compose build deepwiki
docker compose up -d --no-deps deepwiki
```
Expected: image builds (exit 0); healthy; `curl -s http://localhost:3000 | grep -oE '0\.3\.[0-9]+'` shows `0.3.25`.

- [ ] **Step 3: Regenerate 401 (haiku) to exercise bilingual rows + breakdown**

Submit a `force_regenerate` job for `code1_cbl_bv401` (provider `claude`, model `claude-haiku-4-5-20251001`, language `zh-tw`, comprehensive, self_review) via `POST /api/wiki_jobs` with the repo token, and wait for `done`.

- [ ] **Step 4: Verify the split log line + row match**

```bash
docker logs --since 40m deepwiki-open-deepwiki-1 2>&1 | grep "TSD/BRD completeness" | tail -1
```
Expected: a line of the form `TSD/BRD completeness [code1_cbl_bv401]: TSD <c> ok / <n> None / <m> dropped (rows <p>/<t>) · BRD <c> ok / <n> None / <m> dropped (rows <p>/<t>)`, with TSD `rows` now `22/22` (bilingual rows matched), not `0/22`.

- [ ] **Step 5: Verify the endpoint serves by_document + commit**

```bash
BASE="owner=poc&repo=code1_cbl_bv401&repo_type=gitlab&language=zh-tw&provider=claude&model=claude-haiku-4-5-20251001"
curl -s "http://localhost:3000/api/wiki_completeness?${BASE}&format=json" | python3 -c "import sys,json; print(json.load(sys.stdin)['summary']['by_document'])"
```
Expected: a dict with `TSD` and `BRD` keys, each with `headings` and `rows`.

```bash
git add src/version.ts
git commit -m "chore: bump APP_VERSION for TSD/BRD completeness breakdown"
```

---

## Self-Review

**Spec coverage:**
- Part 1 header audit (constant + guard test, reconcile if needed) → Task 5. ✓
- Part 2 bilingual rows (prompt) → Task 4 (+ row-match regression test). ✓
- Part 3 per-document breakdown: checker `by_document` → Task 1; Markdown grouping → Task 2; log split → Task 3. ✓
- Part 4 Jobs-panel UI split + Impact rows line + fallback → Task 6. ✓
- Backward-compat grand totals kept → Task 1 (retained), Task 6 fallback. ✓
- e2e verify (rows 22/22, by_document served) → Task 7. ✓

**Placeholder scan:** none — every code/test step has complete code.

**Type consistency:** `by_document.{TSD,BRD}.{headings:{content,empty_none,missing},rows:{present,missing}}` identical across Task 1 (Python), Task 2/3 (consumers), and Task 6 (`DocCounts` TS). `_document_of`, `uncovered_template_headers`, `TSD_BRD_TEMPLATE_HEADERS` consistent between Tasks 1 and 5. Log/report use "dropped"/"MISSING" for the `missing` status consistently (user-facing wording), JSON keeps `missing`.

**Note on Task 2:** if the prior feature's renderer test (`test_render_markdown_report_marks_statuses`) asserts the old flat header line, it must be updated to the grouped format — flagged in Task 2 Step 4.
