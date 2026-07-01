# TSD/BRD Completeness — Per-Document Breakdown + Bilingual Rows — Design

**Date:** 2026-07-01
**Status:** Approved (design), pending implementation plan

## Problem

The completeness report currently presents one blended summary
(`45 ok / 6 None / 1 MISSING; rows 21/22`). Two shortcomings surfaced in use:

1. **No TSD-vs-BRD separation.** A reader can't tell how the Technical
   Specification (TSD) document fared versus the Business Requirements (BRD)
   document — they want each document's own `ok / None / dropped` counts.
2. **Impact-Analysis rows never match after translation.** The prompt lists the
   22 Impact-Analysis row labels in English but doesn't require the generated
   table to keep them, so a non-English wiki (zh-tw) translates the Item cells
   to pure Chinese. The checker matches rows by their English label, so it finds
   none → `rows 0/22`, rendering all 22 rows as `✗` even though the heading is
   present. This reads as a wall of failures next to a green "0 dropped".
3. **No assurance the required-header list is complete.** "0 dropped" is only
   meaningful if `TSD_BRD_OUTLINES` actually enumerates every intended header
   from the source PCALT templates.

## Goals

- Report TSD and BRD as two clearly separate blocks, each with
  `ok / None / dropped` heading counts and its own row count — in the log, the
  Markdown report, and the Jobs-panel UI.
- Make Impact-Analysis rows match on non-English wikis (fix `rows 0/22`).
- Verify `TSD_BRD_OUTLINES` covers every intended header in the PCALT TSD/BRD
  templates; add any that are genuinely missing.

## Non-goals (YAGNI)

- No checker-side translation map — the prompt change carries the English label
  alongside the translation, so matching stays language-agnostic.
- No change to how program-analysis / developer-wiki pages are handled (they're
  not template pages and aren't checked).
- Rows stay present/missing (no None bucket for rows).

## Architecture

### Part 1 — Header audit (one-time correction + a guard test)

The authoritative intended-header list is the Table of Contents of
`PCALT_TSD_Template -.docx` and `PCALT_BRD_Template.docx` (repo root). A `.docx`
is a zip; `word/document.xml` holds the TOC paragraphs under TOC-level paragraph styles
(the exact style ids differ per file — e.g. `10`/`22` in the TSD file; confirm
by inspection when re-extracting). Extract the TOC header text (strip the
leading section
number, the trailing `PAGEREF …`, and the TOC field code), dedupe, and compare
against the H2/H3 labels in `TSD_BRD_OUTLINES`.

A first manual pass shows the outline is already complete; this task therefore
mainly **confirms** completeness and adds any genuinely-missing header.

**The `.docx` templates are untracked repo-root reference files**, so a test
that reads them would break in CI / a fresh clone. Instead, the header list is
extracted from the template TOCs **once, during implementation** (a throwaway
script, not committed) and its curated result is baked into a committed
constant — `TSD_BRD_TEMPLATE_HEADERS: dict[str, list[str]]` keyed `"TSD"`/`"BRD"`
in `api/tsd_brd_completeness.py`. The lasting artifact is a unit test
(`test_tsd_brd_outline_completeness`) that asserts every header in
`TSD_BRD_TEMPLATE_HEADERS` (normalized) is present somewhere in
`TSD_BRD_OUTLINES` — no `.docx` dependency at test time. If someone later edits
the templates, they update the constant (re-running the same extraction), and
the guard re-verifies coverage.

Normalization for comparison reuses the checker's `_normalize` (lowercase, strip
non-alphanumerics). Template placeholder headers that are per-instance examples
rather than fixed sections are excluded when building the constant: `Physical
file XXXXPF (Create or modify)`, `Logical file XXXXLF (Create or modify)`,
`Program1`, `Program2`, `Program3`, `FR-0001`, `FR-0002` (these map to the
outline's generic "Physical Files (PF)", "Logical Files (LF)", program
inventory, and FR-table headers rather than being literal required headings).

### Part 2 — Bilingual Impact-Analysis rows (prompt-only)

In `api/wiki_prompts.py`, the template `outline_clause` (where the
bilingual-heading rule already lives) gains one sentence: any REQUIRED TABLE ROWS
listed in the template (e.g. the Impact-Analysis items) must be written with each
row's label cell as the `{lang_name}` translation FOLLOWED BY the exact English
label in parentheses — the same `翻譯 (English)` form headings use, e.g.
`現有應用程式 (Existing applications)`.

No checker change is needed: a cell containing `… (Existing applications)`
normalizes to include `existingapplications`, which the existing row match
(`_normalize(label) in body_norm`) already catches. Effect is on **future**
regenerations only.

### Part 3 — Per-document (TSD/BRD) breakdown

**Checker (`api/tsd_brd_completeness.py`).** Every checked page id starts with
`page-tsd-` or `page-brd-`; bucket by that prefix. `check_tsd_brd_completeness`
adds to the report:

```json
"summary": {
  "headings": {"content": N, "empty_none": N, "missing": N},   // grand total (kept)
  "rows": {"present": N, "missing": N},                         // grand total (kept)
  "pages_missing": [...],
  "by_document": {
    "TSD": {"headings": {"content": N, "empty_none": N, "missing": N},
            "rows": {"present": N, "missing": N}},
    "BRD": {"headings": {"content": N, "empty_none": N, "missing": N},
            "rows": {"present": N, "missing": N}}
  }
}
```

A helper `_document_of(page_id) -> "TSD" | "BRD" | None` classifies by prefix
(None for any non-template id, which is never in the outline anyway). Grand
totals are retained so the existing UI keeps working during rollout.

**Markdown report (`render_markdown_report`).** Group pages under two top-level
sections, `## TSD` and `## BRD`, each preceded by that document's summary line;
pages render beneath their document. Missing-pages list stays in the header.

**Log (`api/wiki_generator.py`).** Replace the single summary line with a
two-document line built from `by_document`:

```
TSD/BRD completeness [<repo>]: TSD <c> ok / <n> None / <m> dropped (rows <p>/<t>) · BRD <c> ok / <n> None / <m> dropped (rows <p>/<t>)
```

### Part 4 — Jobs-panel UI (`src/components/CompletenessSummary.tsx`)

Extend `CompletenessReport` with the optional `summary.by_document`. When
present, render TSD and BRD on their own lines:

```
TSD: 30 ok / 2 None / 0 ✗   ·   Impact rows: 22/22
BRD: 18 ok / 2 None / 0 ✗
[view full report ↗]
```

- Each document's `✗` (dropped) count is red when `> 0`.
- The `Impact rows: {present}/{total}` line appears under TSD (rows only exist
  for the TSD system-overview page); its text is red when any row is missing, so
  a wall of missing rows no longer hides behind a green heading count.
- If `by_document` is absent (older report), fall back to the current single
  grand-total line (backward compatible).

The parent `JobsPanel` wiring is unchanged (it already fetches the report and
passes it in).

## Data flow

Generation writes the enriched `…completeness.json` (now with `by_document`) and
the grouped `…completeness.md`, and logs the two-document line → the Jobs panel
fetches the JSON via the existing proxy → `CompletenessSummary` renders the
TSD/BRD split → "view full report" opens the grouped Markdown.

## Error handling

- `_document_of` returns None for unexpected ids; such pages (never in the
  outline) simply don't contribute to either bucket. `by_document` always has
  both "TSD" and "BRD" keys (zero-initialized), so consumers need no presence
  checks.
- The audit test reads the `.docx` files; if a template file is missing it fails
  loudly (that is the point — the guard must not silently pass).
- All report writing remains best-effort in `run_generation` (unchanged).

## Testing

Backend (`.venv/bin/python -m pytest`):
1. `by_document` bucketing: a fixture with tsd + brd pages aggregates each
   document's content/empty_none/missing and rows independently; grand totals
   equal the sum of the two.
2. `_document_of`: `page-tsd-*` → "TSD", `page-brd-*` → "BRD", other → None.
3. `render_markdown_report` groups pages under `## TSD` / `## BRD` with each
   document's summary line.
4. Row match after bilingual rendering: a section body containing
   `現有應用程式 (Existing applications)` marks that row present.
5. `wiki_prompts`: `build_page_prompt(..., required_outline=...)` output contains
   the row-bilingual instruction.
6. Outline-completeness guard: every header in the committed
   `TSD_BRD_TEMPLATE_HEADERS` constant (normalized) is present in
   `TSD_BRD_OUTLINES` (no `.docx` dependency at test time).

Frontend (vitest in `node:20-slim`):
7. `CompletenessSummary` with `by_document` renders a TSD line and a BRD line
   with the right counts, the `Impact rows: p/t` line, red `✗` when a
   document's missing > 0, and red rows text when rows missing > 0.
8. `CompletenessSummary` with a report lacking `by_document` falls back to the
   single grand-total line.
