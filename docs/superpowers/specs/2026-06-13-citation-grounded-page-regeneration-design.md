# Citation-grounded page regeneration

**Date:** 2026-06-13
**Status:** Design — pending implementation plan

## Problem

Wiki page generation must be "based on fact only — do not make things up." Today the
generator already instructs the model not to invent (`prompt_assembly.py:42-43`,
`wiki_prompts.py:498`, `wiki_prompts.py:316`), runs Claude at temperature 0.3, and
**verifies** every `[file:lines]()` citation against the source the model saw plus the
full repo files (`citation_grounding.py`, see the prior spec
`2026-06-12-citation-grounding-and-inline-source-design.md`). Each citation is marked
`verified` or `broken`.

The gap: when the model fabricates a claim, the citation is flagged `broken` but the
**fabricated prose stays in the saved page** (`wiki_generator.py:468` stores content and
citations together; nothing corrects or removes ungrounded claims). "Don't make things
up" is therefore enforced by instruction and flagging, not by removal.

## Goal

A page is saved only after its citations verify. If the model produced broken
citations, run a targeted correction loop; if claims still can't be grounded after the
loop, strip them. Verification-driven mutation must **never** run when verification
itself is untrustworthy (embedder outage), or correct pages would be destroyed.

## Decisions (settled during brainstorming)

- On unverified content: **regenerate the page** (not strip-only, not prompt-only).
- Regeneration mechanism: **targeted correction loop** — feed the model its own page +
  the explicit broken-citation list, fix or remove only those claims, re-verify.
- Final fallback after the loop exhausts: **strip the still-unverified claims**.
- Max correction attempts: **2** (configurable).
- Strip granularity: **per markdown block — remove a block only when ALL its citations
  are broken; keep it if any citation verifies.**

## Flow

Replaces the per-page tail of `run_generation()` in `wiki_generator.py`
(currently ~lines 451–472):

```
generate page  →  verify_page_citations(content, source_map, repo_map)
                       │
              repo_map empty?  ──yes──►  save as-is (OUTAGE GUARD; log warning)
                       │no
              any broken citations? ──no──►  save
                       │yes
        ┌──── correction loop (max attempts = 2) ────┐
        │  prompt = build_citation_fix_prompt(...)    │
        │  content = dispatch(prompt)                 │
        │  citations = verify_page_citations(...)     │
        │  no broken? ──► save                        │
        └── attempts exhausted, still broken ─────────┘
                       │
        content = strip_unverified_claims(content, citations)
        citations = verify_page_citations(content, ...)   # recompute for storage
                       │
                     save
```

## Components

### 1. `build_citation_fix_prompt(page_title, content, broken_citations, repo_url)` — new
Location: `api/wiki_prompts.py` (alongside `build_self_review_prompt`).

Returns an inner prompt giving the model its own page plus the explicit list of broken
citations (each as `file:start-end` + the verifier's `reason`, e.g. "lines not in
provided source" / "file not provided"). Instruction, in spirit:

> "The citations listed below could not be found in the repository's actual source.
> For each one, either correct the claim so it matches the real code (and cite the
> correct file and line numbers), or remove the claim entirely. Do not add any new
> claims that are not supported by the provided source. Keep the page's structure,
> level of detail, and language. Return the COMPLETE corrected page in markdown — no
> preamble, no code fence around the whole page."

Dispatched through the existing `assemble_envelope(system_prompt, inner, file_content,
file_path, context_text, provider)` so the model has the same grounding context the
page had (deep-dive file content and/or retrieved chunks).

### 2. Correction loop — `api/wiki_generator.py`
After the first `verify_page_citations`, if any citation has `status == "broken"` **and**
`repo_map` is non-empty, loop up to `MAX_CITATION_FIX_ATTEMPTS` (default 2):
build the fix prompt, `timed_dispatch`, strip markdown fences (same regex as page gen),
rebuild the fitted source map (`fit_envelope_inputs` → `build_source_map`), re-verify.
Exit early when no broken citations remain.

- Runs independently of `job.self_review`. The existing self-review pass is unchanged
  and still runs before this (its corrections feed into the first verification).
- Token accounting: reuse `stats_review` (or a new `stats_citation_fix`) — decide in plan.
- Cancellation: `await checkpoint()` each attempt, like the existing loops.
- A dispatch failure inside the loop is caught and breaks the loop (keep best content so
  far), matching the self-review "keep original on failure" pattern.

### 3. `strip_unverified_claims(content, citations)` — new pure module
Location: new `api/citation_stripping.py` (pure, no I/O — mirrors `citation_grounding.py`).

Removes every markdown **block** whose citations are all broken; keeps a block if any of
its citations is verified; always keeps headings.

Block definition: a paragraph or a list item, **together with a trailing
`Sources: [...]()` line that immediately follows it** (the deep-dive format puts the
claim in one line/bullet and its citations on a following `Sources:` line). The claim and
its associated `Sources:` line form one logical block and are stripped or kept as a unit,
so stripping never leaves an orphaned claim or an orphaned `Sources:` line. A block's
citation set is the union of inline `[file:lines]()` citations in the claim and those on
its associated `Sources:` line. Blocks are delimited by blank lines / list markers.

- Input `citations` is the `{label: {status,...}}` map from `verify_page_citations`.
- A block with no citations at all is **kept** (prose like intros/headings is not a
  fabrication signal on its own; only blocks that *cite* and cite only-broken are
  fabrications). This is intentionally conservative.
- Returns cleaned markdown. The caller re-runs `verify_page_citations` on the result so
  the stored `citations` map matches the saved content.

### The outage guard (critical)

Per the embedder-outage signature: when the vLLM embedder is unreachable,
`rag.transformed_docs` is empty → `indexed_paths` empty → `repo_map` empty → **100% of
citations resolve `broken`** even for correct pages. Correction and stripping must be
gated on `repo_map` being non-empty. When `repo_map` is empty, skip both, save the page
unchanged (today's behavior), and log a warning. This makes the feature degrade safely
into current behavior during an outage instead of deleting whole pages.

## Out of scope (YAGNI)

- **Structure generation** — grounded in the real file tree; carries no citations.
- **Mermaid diagrams** — carry no citations, so neither verified nor stripped.
  Acknowledged gap; not addressed here.
- **Non-Claude temperature changes.**

## Configuration

- `MAX_CITATION_FIX_ATTEMPTS` — default `2`. Surface via `config/generator.json` (or a
  module constant with an env override); final location decided in the plan.

## Testing

Unit (`strip_unverified_claims`):
- All-broken block → removed.
- Mixed block (one verified, one broken citation) → kept whole.
- Heading lines → always kept.
- Trailing `Sources:` line whose citations are all broken → removed with its claim.
- Block with no citations → kept.

Unit (`build_citation_fix_prompt`):
- Output contains every broken label and its reason; contains the page content.

Integration (mocked `dispatch`):
- broken → corrected-clean: saves the corrected content, no stripping.
- broken → still-broken after 2 attempts: stripping runs; saved content has the
  fabricated block removed.
- **empty `repo_map`: neither correction nor stripping runs; page saved intact** (outage
  guard).
- clean first pass: no extra dispatch calls.

## Files touched

- `api/wiki_prompts.py` — add `build_citation_fix_prompt`.
- `api/citation_stripping.py` — new pure module `strip_unverified_claims`.
- `api/wiki_generator.py` — correction loop + outage guard + final strip, replacing the
  per-page citation tail (~451–472).
- `api/config/generator.json` (or constant) — `MAX_CITATION_FIX_ATTEMPTS`.
- Tests under the existing api test layout.
