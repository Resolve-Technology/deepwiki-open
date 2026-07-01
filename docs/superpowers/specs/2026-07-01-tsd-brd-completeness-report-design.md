# TSD/BRD Completeness Verification Report — Design

**Date:** 2026-07-01
**Status:** Approved (design), pending implementation plan

## Problem

The TSD and BRD wiki pages are generated against a fixed template outline
(`TSD_BRD_OUTLINES` in `api/wiki_prompts.py`). The generation prompt tells the
model to reproduce **every** heading — writing `None` where the source has
nothing — but models (especially smaller/faster ones) sometimes **drop**
headings entirely. A dropped heading and a legitimately-empty (`None`) heading
look similar at a glance, yet mean very different things:

- **`None`** — the model kept the required field and correctly marked it empty.
- **MISSING** — the model silently omitted a required field (a defect).

There is currently no way to tell, for a generated wiki, which template fields
are present-with-content, which are legitimately `None`, and which the model
dropped. This feature adds an automatic completeness check that produces a
report answering exactly that.

## Goals

- After each generation, verify every TSD/BRD template field is accounted for.
- Classify each field into 3 buckets: `content` / `empty_none` / `MISSING`.
- Also verify enumerated required rows inside designated sections (v1: the
  Impact Analysis table's 22 items).
- Emit a machine-readable JSON report and a human-readable Markdown report next
  to the wikicache, plus a one-line log summary per job.

## Non-goals (YAGNI)

- No detection of *extra* headings the model added beyond the template.
- No fuzzy/semantic matching of translated headings — anchor on the mandated
  English-in-parens wording only.
- No UI. The report is files on disk + a log line.
- Row-level checks only for sections explicitly designated as enumerated (v1:
  Impact Analysis). No naive bullet-parsing elsewhere.

## Architecture

### New module: `api/tsd_brd_completeness.py`

Pure functions, no I/O — fully unit-testable. Core entry point:

```python
def check_tsd_brd_completeness(pages, outlines=TSD_BRD_OUTLINES) -> dict:
    """Return a structured completeness report for the TSD/BRD template pages."""
```

`pages` is the list of generated page dicts (each has `id`, `title`, `content`)
as stored under `wiki_structure.pages` in the wikicache JSON.

### Required-field model (single source of truth: `TSD_BRD_OUTLINES`)

- **Headings:** parse each `## ` / `### ` line of a page's outline into a
  required heading `{level, english_label}`. `## ` → H2, `### ` → H3.
- **Enumerated rows:** a module-level `ENUMERATED_SECTIONS` set names which
  `(page_id, section_english_label)` blocks carry required rows. For those, the
  outline's `- ` bullet lines (only) become the required row labels. v1:
  `{("page-tsd-system-overview", "Impact Analysis")}` → 22 rows.

This means adding a new enumerated section later is a one-line change and needs
no new data — the labels already live in the outline.

### Matching generated headings ↔ required headings

Generated headings render as `翻譯文字 (English Wording)` per the prompt. The
matcher extracts the text inside the **trailing parentheses**, normalizes it
(lowercase, strip all non-alphanumerics), and compares to the normalized
English label. If a generated heading has no trailing parens, its full
normalized text is used as a fallback anchor. A required heading with no match
is `MISSING` — which also flags prompt non-compliance (model didn't include the
English wording).

### Per-field classification

For each required heading, in outline order:

- **`MISSING`** — no generated heading matches its anchor.
- Otherwise, take the heading's **section body**: the markdown between this
  heading and the next generated heading of the same-or-higher level.
  - **`empty_none`** — stripped body ∈ the none-token set
    (`none`, `無`, `無相關資訊`, `n/a`, `na`, `-`, empty). (Case-insensitive;
    also treats a body that is *only* the none token plus trivial punctuation as
    empty.)
  - **`content`** — anything else.

For an enumerated section, additionally check each required row label: `present`
if its normalized English label appears anywhere in the section body (table cell
or line), else `MISSING`. (Rows are present/missing only — no `None` bucket.)

A page id present in `TSD_BRD_OUTLINES` but **absent from `pages`** → the whole
page is reported missing and all its headings are `MISSING`.

## Report artifacts

Written next to the wikicache JSON, sharing its base path:

- `<cache-base>.completeness.json` — structured:

```json
{
  "repo": "...", "provider": "...", "model": "...", "generated_at": "...",
  "summary": {
    "headings": {"content": 73, "empty_none": 12, "missing": 5},
    "rows": {"present": 20, "missing": 2},
    "pages_missing": ["page-brd-reference"]
  },
  "pages": [
    {
      "id": "page-tsd-system-overview", "title": "系統概觀 (System Overview)",
      "present": true,
      "headings": [
        {"label": "System Platform", "level": 2, "status": "content"},
        {"label": "Impact Analysis", "level": 2, "status": "empty_none",
         "rows": [
           {"label": "Existing applications", "status": "present"},
           {"label": "Existing reports", "status": "missing"}
         ]},
        {"label": "Network", "level": 3, "status": "missing"}
      ]
    }
  ]
}
```

- `<cache-base>.completeness.md` — human-readable: one section per page with a
  small table using ✓ (`content`) / ○ (`empty_none`) / ✗ (`MISSING`) markers,
  and a top summary line. Row-level results shown nested under their section.

- **Log line** per job, e.g.:
  `TSD/BRD completeness [401]: headings 73 ok / 12 None / 5 MISSING; rows 20/22`.

## Integration point

In `run_generation` (`api/wiki_generator.py`), after all pages are generated,
grounded/self-reviewed, **and the wikicache has been saved** (so its path is
known), call the checker over the final page list, write both report files
beside the wikicache, and log the summary.

**Best-effort contract:** the entire block is wrapped in `try/except` and logs a
warning on failure — a bug in the checker must **never** fail or block a
generation job. The report is diagnostic, not gating.

The report base path is derived from the same wikicache path the job already
computes when saving the cache (so `X.json` → `X.completeness.json` /
`X.completeness.md`).

## Error handling & edge cases

- Missing/empty `content` on a page → treated as page-present but every heading
  `MISSING` (the body has nothing to match).
- Duplicate matched headings in the generated page → first match wins; extras
  ignored.
- Section body extent uses the next heading of level ≤ current; the last section
  runs to end-of-page.
- None-token set is a module constant so it can grow without code changes.
- Checker never raises to the caller for malformed input; on internal error it
  returns a minimal report noting the failure and the integration logs a warning.

## Testing

Unit tests (`tests/unit/test_tsd_brd_completeness.py`) with synthetic pages:

1. Fully-complete page → all `content`, no MISSING.
2. Page with `None` sections (literal `None`, `無`, blank) → `empty_none`.
3. Page with a dropped heading → `MISSING`.
4. Heading matched via English-in-parens; and fallback when parens absent.
5. Enumerated rows: all present; some rows missing from the Impact table.
6. Whole page absent from `pages` → page missing, all headings `MISSING`.
7. Summary counts aggregate correctly across pages.
8. Non-template pages (no outline entry) are ignored.
```
