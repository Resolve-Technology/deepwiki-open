# TSD Content Consolidation — Dedup Intros + Program Inventory Table — Design

**Date:** 2026-07-01
**Status:** Approved (design), pending implementation plan

## Problem

Two content-quality issues in the generated TSD wiki:

1. **Repeated intros.** The standard page-generation prompt tells *every* page to
   open with a 1-2 paragraph general introduction (purpose/scope/overview of the
   project). Across the many TSD chapters this produces near-duplicate
   project-overview blurbs scattered everywhere, instead of the overview living
   in one place.
2. **Program Inventory is prose, not a table.** The Program Inventory section's
   outline only *optionally* ("may") summarises programs as a table, so the model
   usually writes free-form prose, making it hard to scan each program's role.

## Goals

1. TSD chapters (other than the Introduction chapter) stop writing a general
   project introduction/overview; that content is consolidated in the
   Introduction chapter (`## Purpose` / `## Document Overview`). Each other
   chapter goes straight into its template sections.
2. The Program Inventory section MUST lead with a summary table —
   `Program | Function | Key Logic`, one row per program — followed by a short
   `###` key-points subsection per program.

## Scope / non-goals (YAGNI)

- Applies to **TSD** template pages only (`page-tsd-*`). BRD (`page-brd-*`) and
  the developer Wiki pages keep their current behaviour (the user scoped this to
  TSD).
- The `🔬 Program Analysis` deep-dive pages are unchanged (different prompt).
- No change to the completeness checker or its header guard — the
  `## Program Inventory` heading text is unchanged (only its guidance prose), and
  no headings are added/removed, so `TSD_BRD_OUTLINES` heading coverage is
  unaffected.
- Effect is on **future generations** only; existing wikis must be regenerated.

## Architecture

### Change A — suppress the general intro on TSD pages (`api/wiki_prompts.py`)

`build_page_prompt(...)` gains a parameter `omit_general_intro: bool = False`.
In the standard (non-deep-dive) page template, the current instruction:

> `1. **Introduction:** Start with a concise introduction (1-2 paragraphs)
> explaining the purpose, scope, and high-level overview of "{page_title}" ...`

becomes conditional:
- When `omit_general_intro` is `False` (default — BRD, developer Wiki, and any
  non-template page): unchanged.
- When `True`: replace it with an instruction that the page must NOT add a
  general project introduction/overview (the Introduction chapter covers that)
  and should start directly with its required template sections. Wording, e.g.:
  > `1. **No general introduction:** Do NOT open with a general introduction or
  > project/overview paragraph — the dedicated Introduction chapter covers the
  > overall purpose, scope, and overview. Begin directly with this page's
  > required template sections below.`
  The subsequent numbered items (Detailed Sections, Mermaid, etc.) are unchanged.

`run_generation` (`api/wiki_generator.py`) passes
`omit_general_intro=page["id"].startswith("page-tsd-")` into `build_page_prompt`.
The Introduction chapter (`page-tsd-introduction`) also gets `True`, but still
produces the overall intro because its own `required_outline`
(`## Purpose` / `## Document Overview`) is injected via the existing
`outline_clause` — the intro content comes from the outline, not the generic
instruction. So the overall intro is consolidated onto that one page.

### Change B — Program Inventory mandatory table (`api/wiki_prompts.py`)

`TSD_BRD_OUTLINES["page-tsd-program-inventory"]` changes from:

```
## Program Inventory
(One ### subsection per program/module: business function and key logic. May also summarise as a table: Program | Business Function | Key Logic.)
```

to:

```
## Program Inventory
(REQUIRED: FIRST a summary table with columns `Program | Function | Key Logic` — one row per program/module (the source program members). THEN, below the table, one `###` subsection per program with its brief key points. Do not omit the table.)
```

The table column labels are English in the template; the existing `outline_clause`
already instructs the model to render headings/labels in the target language, so
they display translated (e.g. `程式 | 作用 | 主要邏輯`).

## Data flow

Generation of a `page-tsd-*` page → `run_generation` passes
`omit_general_intro=True` → `build_page_prompt` emits the "no general intro"
instruction + the (now table-requiring) Program Inventory outline for that page →
the model produces chapters without repeated overviews and a Program Inventory
that leads with a table. The Introduction chapter still carries the single
consolidated overview.

## Error handling

- Purely prompt-text changes; no new runtime code paths. `omit_general_intro`
  defaults to `False`, so any caller that doesn't set it (or non-`page-tsd-*`
  pages) keeps today's behaviour.

## Testing

`tests/unit/test_wiki_prompts.py`:
1. `build_page_prompt(..., omit_general_intro=True)` → prompt does NOT contain
   `"concise introduction (1-2 paragraphs)"` and DOES contain the "No general
   introduction" / "Do NOT open with a general introduction" wording.
2. `build_page_prompt(..., omit_general_intro=False)` (default) → prompt STILL
   contains the original `"concise introduction (1-2 paragraphs)"` instruction
   (BRD/Wiki unchanged).
3. `TSD_BRD_OUTLINES["page-tsd-program-inventory"]` requires a table — contains
   `"Program | Function | Key Logic"` and `"REQUIRED"`.

`api/wiki_generator.py`: the one-line flag wiring
(`omit_general_intro=page["id"].startswith("page-tsd-")`) is verified by
`ast.parse` syntax check + the behaviour tests above; importing `api.api` is out
of scope in this sandbox, so no full-generation unit test.

E2e: regenerate 401 (haiku) and confirm (a) non-Introduction TSD chapters no
longer open with a general project overview, and (b) the Program Inventory leads
with a `Program | Function | Key Logic` table.
