# Program Deep-Dive Pages Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generated documentation gains a fourth top-level section — **🔬 Program Analysis** — with one deep-dive page per COBOL program whose detail level *exceeds* the reference document `BV401.pdf` (8 pages: program info, file tables, working-storage fields, per-paragraph PROCEDURE DIVISION analysis with flowcharts, incremental-control mechanism, data flow, error handling, related programs, gotchas).

**Architecture:** The backend already supports full-source injection (`ChatCompletionRequest.filePath` → `get_file_content` → `<currentFileContent>` in the prompt, `api/websocket_wiki.py:406-431`) but the frontend never uses it for page generation — pages are built from top-k RAG fragments (350-word chunks), which caps the achievable detail. The fix: (1) the structure prompt requests one `page-analysis-*` page per program source file; (2) `generatePageContent` detects those pages, switches to a dedicated exhaustive-analysis prompt, and sends `filePath` so the model sees the entire program; (3) the backend gets a provider-aware prompt-token budget that drops redundant RAG context and (only if still needed) truncates huge sources DIVISION-aware, so the same flow works on claude (1M ctx) and local vLLM/gemma; (4) claude's `max_tokens` rises to 32768 so a single page can hold paragraph-by-paragraph depth.

**Tech Stack:** Next.js frontend (`src/app/[owner]/[repo]/page.tsx` — prompt templates), FastAPI backend (`api/websocket_wiki.py`, `api/simple_chat.py`), no new dependencies.

---

## Constraints & context (read before starting)

1. **Decisions made with the user:** language follows the UI language selector (prompts stay language-parameterized); deep-dive lives in a NEW 4th top-level section (Wiki/TSD/BRD untouched); must work with whichever provider the user selects (claude or vllm), budgets tuned per provider.
2. **Why current output is shallow:** page generation sends only a ~2k-token instruction; context comes from RAG top-k (chunks of 350 words). Whole-program reasoning (call graphs, every paragraph, every field) is impossible from fragments. `BV401.txt` is 668 lines — trivially fits in full. The extreme case `poc_code_advanced/2.B5349.../B5349.txt` is ~147k tokens and is **already skipped by the embedder** (`data_pipeline.py:390` "Skipping large file"), so today its pages are generated nearly blind; with this plan claude sees it whole and vllm sees a DIVISION-aware truncation.
3. **The 8k guard** (`websocket_wiki.py:80-90`, `simple_chat.py:91-99`) counts only the last user message. The deep-dive instruction stays ~2.5k tokens, so the guard never trips; file content is appended server-side and governed by the new budget logic instead. Do not touch the guard.
4. **Reference PDF:** `/home/ubuntu/deepwiki-open/BV401.pdf` (text extract at `/tmp/BV401_extract.txt`, regenerate inside the container with pypdf if missing). Its section list is the *floor*, not the ceiling: it documents only "main" working-storage variables and 7 paragraphs; we require ALL paragraphs and ALL 01/77-level fields.
5. **Deployment:** code is baked into the Docker image — `docker compose up -d --build` to apply. pytest 8.4.2 is in the image; run backend tests via:
   ```bash
   docker run --rm --entrypoint python -v /home/ubuntu/deepwiki-open:/app deepwiki-open-deepwiki -m pytest <testfile> -v
   ```
6. **No frontend test infra exists** (no jest/vitest config) — frontend changes are prompt-text edits verified by the end-to-end regeneration in Task 6. Do not invent a JS test harness.
7. **Wiki regeneration is frontend-driven** (browser calls ws/chat per page). Task 6 therefore needs one manual click from the user (refresh wiki for the `poc_code1_cbl_bv401` repo); the automated parity checker then scores the produced cache JSON.
8. **Existing caches** won't gain the new section until force-regenerated — expected, not a bug.

---

### Task 1: Backend — provider-aware prompt budget (`api/prompt_fit.py`)

**Files:**
- Create: `api/prompt_fit.py`
- Test: `tests/unit/test_prompt_fit.py`
- Modify: `api/websocket_wiki.py` (prompt assembly, ~lines 404-440), `api/simple_chat.py` (equivalent assembly — search for `<currentFileContent`)

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_prompt_fit.py`:
```python
"""Tests for provider-aware prompt budget fitting."""
from api.prompt_fit import fit_to_budget, prompt_token_budget


def test_budget_for_claude_is_large():
    assert prompt_token_budget("claude") >= 500_000


def test_budget_default_is_conservative():
    assert prompt_token_budget("vllm") == 24_000
    assert prompt_token_budget("unknown-provider") == 24_000


def test_fit_noop_when_under_budget():
    file_content, context_text = fit_to_budget(
        file_content="A" * 1000, context_text="B" * 1000,
        base_tokens=500, budget=28_000,
    )
    assert file_content == "A" * 1000
    assert context_text == "B" * 1000


def test_fit_drops_rag_context_first():
    # ~120k chars ≈ 30k tokens of file + 40k chars ≈ 10k tokens of RAG, budget 31k:
    # dropping RAG alone gets under budget, file stays whole.
    file_content, context_text = fit_to_budget(
        file_content="A" * 120_000, context_text="B" * 40_000,
        base_tokens=1000, budget=32_000,
    )
    assert context_text == ""
    assert file_content == "A" * 120_000


def test_fit_truncates_file_middle_keeping_head_and_tail():
    big = "HEAD" + ("M" * 400_000) + "TAIL"
    file_content, context_text = fit_to_budget(
        file_content=big, context_text="", base_tokens=1000, budget=28_000,
    )
    assert file_content.startswith("HEAD")
    assert file_content.endswith("TAIL")
    assert "[TRUNCATED" in file_content
    assert len(file_content) < len(big)


def test_fit_empty_file_content_untouched():
    file_content, context_text = fit_to_budget(
        file_content="", context_text="C" * 200_000, base_tokens=1000, budget=28_000,
    )
    # No file content -> RAG is the only context; it gets tail-trimmed, not dropped.
    assert 0 < len(context_text) < 200_000
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
docker run --rm --entrypoint python -v /home/ubuntu/deepwiki-open:/app deepwiki-open-deepwiki -m pytest tests/unit/test_prompt_fit.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'api.prompt_fit'`.

- [ ] **Step 3: Implement `api/prompt_fit.py`**

```python
"""Provider-aware prompt budget fitting.

Deep-dive pages inject entire program sources into the prompt
(``ChatCompletionRequest.filePath``). Providers differ wildly in context
size (claude: 1M tokens; local vLLM/gemma: tens of k), so before assembly
the handlers call :func:`fit_to_budget` which, in order:

1. leaves everything untouched if the estimate fits the provider budget;
2. drops the RAG context (redundant when the full source is present);
3. truncates the *middle* of the file content, keeping head and tail —
   COBOL sources put IDENTIFICATION/ENVIRONMENT/DATA divisions at the top
   and the tail of PROCEDURE DIVISION carries termination logic, so the
   middle is the least-bad cut.

Token counts are estimated at 4 chars/token to avoid tokenizer costs on
huge strings; budgets carry enough slack that the estimate is safe.
"""
import logging
import os

log = logging.getLogger(__name__)

CHARS_PER_TOKEN = 4
TRUNCATION_MARKER = "\n\n*** [TRUNCATED: middle of file omitted to fit the model's context window] ***\n\n"

# Conservative defaults; override per deployment via env if needed.
# 24k (not higher) because the 4-chars/token estimate UNDERCOUNTS dense
# COBOL tokens (short keywords, numerics) — keep ~20% slack vs the model
# context rather than sail close to it.
_DEFAULT_BUDGET = int(os.getenv("DEEPWIKI_PROMPT_TOKEN_BUDGET", "24000"))
_PROVIDER_BUDGETS = {
    "claude": int(os.getenv("DEEPWIKI_CLAUDE_PROMPT_TOKEN_BUDGET", "800000")),
}


def prompt_token_budget(provider: str) -> int:
    """Return the prompt token budget for a provider."""
    return _PROVIDER_BUDGETS.get(provider, _DEFAULT_BUDGET)


def _estimate_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


def fit_to_budget(file_content: str, context_text: str, base_tokens: int, budget: int):
    """Fit (file_content, context_text) into ``budget`` tokens.

    ``base_tokens`` is the estimated size of everything else in the prompt
    (system prompt, instruction, conversation history).
    Returns the possibly-reduced ``(file_content, context_text)`` pair.
    """
    def total():
        return base_tokens + _estimate_tokens(file_content) + _estimate_tokens(context_text)

    if total() <= budget:
        return file_content, context_text

    # 1) Drop RAG context when the full source is present — it is redundant.
    if file_content and context_text:
        log.info("Prompt over budget (%d > %d tokens): dropping RAG context", total(), budget)
        context_text = ""
        if total() <= budget:
            return file_content, context_text

    # 2) Truncate the middle of whichever block remains too large.
    if file_content:
        allowed_chars = max((budget - base_tokens) * CHARS_PER_TOKEN - len(TRUNCATION_MARKER), 0)
        if allowed_chars < len(file_content):
            head = allowed_chars // 2
            tail = allowed_chars - head
            log.warning(
                "File content over budget: keeping first %d and last %d of %d chars",
                head, tail, len(file_content),
            )
            file_content = file_content[:head] + TRUNCATION_MARKER + file_content[-tail:]
    elif context_text:
        allowed_chars = max((budget - base_tokens) * CHARS_PER_TOKEN, 0)
        if allowed_chars < len(context_text):
            context_text = context_text[:allowed_chars]

    return file_content, context_text
```

- [ ] **Step 4: Run the tests; all must pass**

Same docker command. Expected: 6 passed.

- [ ] **Step 5: Wire into `api/websocket_wiki.py`**

Add import near the other `api.` imports (~line 21):
```python
from api.prompt_fit import fit_to_budget, prompt_token_budget
```
In the prompt-assembly region: `file_content` is fetched at lines 406-413, `context_text` was built at lines 197-247, `conversation_history` at lines 415-419; the prompt is assembled starting at line 422 (`prompt = f"/no_think {system_prompt}\n\n"`). Insert immediately BEFORE that assembly line (all four inputs are in scope there):
```python
        # Fit oversized inputs (full program sources) to the provider's context budget
        file_content, context_text = fit_to_budget(
            file_content=file_content,
            context_text=context_text,
            base_tokens=count_tokens(
                system_prompt + conversation_history + query,
                is_ollama_embedder=(request.provider == "ollama"),
            ),
            budget=prompt_token_budget(request.provider),
        )
```
(`is_ollama_embedder` MUST be passed as a keyword — there is no `is_ollama` variable in scope, and the second positional parameter of `count_tokens` is `embedder_type`, not a boolean. This mirrors the existing call at ~line 84.) `conversation_history` must already be built — insert the fit call AFTER the history loop (lines 415-419) and BEFORE `prompt = f"/no_think ..."` (line 422).

- [ ] **Step 6: Wire into `api/simple_chat.py` identically**

Same import; find the equivalent assembly (search for `<currentFileContent` — same structure) and insert the same call before its `prompt = ...` line.

- [ ] **Step 7: Syntax-check + run all unit tests**

```bash
python3 -c "import ast; [ast.parse(open(f).read()) for f in ['api/websocket_wiki.py','api/simple_chat.py','api/prompt_fit.py']]; print('syntax OK')"
docker run --rm --entrypoint python -v /home/ubuntu/deepwiki-open:/app deepwiki-open-deepwiki -m pytest tests/unit/ -v
```
Expected: syntax OK; all tests pass (6 new + 8 existing claude tests + any other unit tests).

- [ ] **Step 8: Commit**

```bash
git add api/prompt_fit.py tests/unit/test_prompt_fit.py api/websocket_wiki.py api/simple_chat.py
git commit -m "Fit full-source prompts to provider-aware token budgets"
```

---

### Task 2: Raise claude output budget

**Files:**
- Modify: `api/config/generator.json` (claude provider block, lines ~127-145)

- [ ] **Step 1: Change `max_tokens` from 8192 to 32768 for all three claude models**

The deep-dive page for a mid-size program runs 15-25k output tokens; 8192 would truncate mid-page. (vLLM models stay untouched — the server generates to its own limit.)

- [ ] **Step 2: Validate**

```bash
python3 -c "import json; cfg=json.load(open('api/config/generator.json')); ms=cfg['providers']['claude']['models']; assert all(m['max_tokens']==32768 for m in ms.values()), ms; print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add api/config/generator.json
git commit -m "Raise claude max_tokens to 32768 for deep-dive page generation"
```

---

### Task 3: Frontend — 4th top-level section in the structure prompt

**Files:**
- Modify: `src/app/[owner]/[repo]/page.tsx` — inside `determineWikiStructure` (~line 687), the comprehensive-view template

- [ ] **Step 1: Insert the Program Analysis section spec**

Locate the BRD block ending with `- Reference (Definition of Terminologies; Attachments)` (~line 780), which is followed by the paragraph `Each top-level section should contain its own pages/subsections. ...`. Insert BETWEEN them:

```
=== Top-level section 4: "🔬 Program Analysis" (per-program deep dive) ===
EXACTLY ONE page per program source file in the repository (a program source file is any COBOL/RPG/source member — e.g. *.cbl, *.cob, *.rpg, or *.txt files whose content is program source). Rules for these pages:
- The page id MUST follow the pattern "page-analysis-<program-name-lowercase>" (e.g. "page-analysis-bv401").
- The page title MUST be "Program Deep Dive: <PROGRAM-NAME>".
- relevant_files MUST contain EXACTLY the one source file for that program (plus its copybook files if they exist as separate files in the repository).
- importance MUST be "high".
- Do NOT create analysis pages for non-program files (READMEs, JCL listings, data files), and do NOT merge multiple programs into one page.
```

- [ ] **Step 2: Update the page-count instruction**

At the `IMPORTANT:` list near the end of the template (~line 853), change item 1 from:
```
1. Create ${isComprehensiveView ? '18-30 pages total spread across the three top-level documents (Wiki, TSD, BRD), each document having a meaningful set of pages' : '4-6 pages'} ...
```
to:
```
1. Create ${isComprehensiveView ? '18-30 pages total spread across the Wiki, TSD and BRD documents, PLUS exactly one "🔬 Program Analysis" page per program source file (these do not count toward the 18-30)' : '4-6 pages'} ...
```

- [ ] **Step 3: Type-check / build**

```bash
cd /home/ubuntu/deepwiki-open && npx next build --no-lint 2>&1 | tail -5
```
Expected: build completes (template literals are plain strings; the only failure mode is an accidentally unescaped backtick — fix if so). If `npx next build` is too slow/heavy on this host, `npx tsc --noEmit` is an acceptable substitute.

- [ ] **Step 4: Commit**

```bash
git add "src/app/[owner]/[repo]/page.tsx"
git commit -m "Add Program Analysis top-level section to wiki structure prompt"
```

---

### Task 4: Frontend — deep-dive page prompt + full-source request

**Files:**
- Modify: `src/app/[owner]/[repo]/page.tsx` — inside `generatePageContent` (~line 373)

- [ ] **Step 1: Add deep-dive detection and prompt selection**

`generatePageContent` currently builds one `promptContent` template literal starting `You are an expert technical writer and software architect.` (~line 420) and a `requestBody` (~line 532). Restructure:

```typescript
const isDeepDive = page.id.startsWith('page-analysis-');
```

Keep the existing prompt for non-deep-dive pages, byte-identical. For deep-dive pages use this template instead (reuse the SAME `${filePaths.map(...)}` details-block, the SAME language interpolation expression the existing template uses for "Generate the content in ... language", and the SAME Mermaid syntax rules block — copy that block verbatim from the existing template rather than retyping it):

```typescript
const deepDivePrompt = `You are a senior mainframe/COBOL systems analyst producing the definitive reference analysis of one program.
You are given the COMPLETE source of the program in [CURRENT_FILE_CONTENT]. Base EVERY statement strictly on that source (plus any copybook files provided). Never invent fields, paragraphs, or behavior. Cite line numbers for every claim using the format [${page.filePaths[0] ?? 'source'}:start-end]().

CRITICAL STARTING INSTRUCTION:
The very first thing on the page MUST be a <details> block listing the source file(s) analyzed:
<details>
<summary>Relevant source files</summary>

${filePaths.map(path => `- [${path}](${generateFileUrl(path)})`).join('\n')}
</details>

Immediately after, the H1 title: # ${page.title}

Then produce ALL of the following numbered sections (every one is REQUIRED; if a section is genuinely not applicable to this program, keep the heading and state in one line why it does not apply):

## 1. Program Identification
Table: program name, platform (infer from source style, e.g. IBM AS/400), version/date stamps found in source, change/work-unit references found in comments, one-paragraph business purpose.

## 2. Environment & File Definitions
For EVERY file in SELECT/ASSIGN and FD entries: logical name, physical file/member, record format name, organization, access mode, key fields, open mode used (INPUT/OUTPUT/I-O/EXTEND), and its role (primary input / primary output / update-in-place / control / reference lookup). Group into Input / Output-Update / Reference tables.

## 3. Copybooks & Record Layouts
Every COPY member and inline record layout: where used, full field table (level, field name, PIC, computed byte length, description inferred from usage). Do not skip filler fields.

## 4. Working-Storage Inventory (EXHAUSTIVE)
EVERY 01/77-level item and its subordinate fields — no exceptions, including counters, flags, constants, timestamps and work areas. Table columns: field, PIC, length, initial value, purpose, and the paragraphs that read or write it. Group logically (constants / flags / counters / timestamps / record areas / work fields).

## 5. Procedure Division — Complete Paragraph Inventory
First a table of EVERY paragraph/SECTION in source order: name, one-line purpose, performed-by (callers), performs (callees), files touched.
Then a Mermaid call-graph (graph TD) of the PERFORM structure covering EVERY paragraph.

## 6. Paragraph-by-Paragraph Analysis (THE CORE — be exhaustive)
One ### subsection PER PARAGRAPH, in source order. Do NOT group or summarize multiple paragraphs together. For each: purpose; trigger/caller; numbered step-by-step logic; every file operation (verb, file, key used, status handling); every condition/branch and what each path does; data transformations (source field → target field); a Mermaid flowchart (graph TD) for any paragraph with branching or loops.

## 7. Control & Restart Mechanisms
Any checkpoint/timestamp/incremental-processing/commit logic: which fields and files implement it, the exact sequence (Mermaid sequenceDiagram), what happens on abnormal termination, rerun/restart safety analysis.

## 8. End-to-End Data Flow
Mermaid flowchart (graph TD): every input file → the transformations/decision points → every output/updated file. Follow with a field-level mapping table (output field ← source field/derivation) for the primary output record.

## 9. Error Handling Inventory
EVERY file-status check, INVALID KEY clause, error flag set/test, error display/abend path: table of location (paragraph + lines), condition detected, and the program's response.

## 10. External Dependencies & Cross-Program Relationships
Called programs (CALL statements), callers if inferable from comments, shared files that couple this program to others, JCL/scheduling hints found in comments.

## 11. Operational Notes & Gotchas
Concrete, evidence-based warnings: rerun/duplicate-processing risks, sort-order assumptions, REWRITE-after-READ requirements, counter overflow limits (compute the actual limit from the PIC), locking/contention, hard-coded values that look like configuration.

## 12. Glossary
Business and technical terms appearing in the source (field prefixes, file names, domain abbreviations) with their meanings as evidenced by usage.

COMPLETENESS RULES (these override brevity):
- Section 6 MUST contain one subsection for EVERY paragraph listed in section 5 — a reviewer will diff the two lists.
- Section 4 MUST contain EVERY working-storage item — a reviewer will grep the source for 01/77 levels and check.
- Prefer tables over prose. Cite line numbers everywhere. This document must exceed the detail of a human-written 8-page program analysis; length is NOT a concern, completeness is.

[IMPLEMENTER NOTE — NOT LITERAL TEXT: replace this bracketed line with the existing "CRITICAL: All diagrams MUST follow strict vertical orientation" Mermaid-rules block, copied verbatim from the standard prompt in this same function. Do NOT leave this bracket in the shipped template.]

IMPORTANT: Generate the content in [IMPLEMENTER NOTE — NOT LITERAL TEXT: reuse the exact \`\${language === 'en' ? 'English' : ...}\` interpolation expression from the standard prompt] language.

[WIKI_PAGE_TOPIC]: ${page.title}
[CURRENT_FILE_CONTENT]: provided in the request context.`;

const promptContent = isDeepDive ? deepDivePrompt : /* existing template, unchanged */;
```

- [ ] **Step 2: Send the full source with the request**

After the `requestBody` construction (~line 532-540, before `addTokensToRequestBody`):
```typescript
// Deep-dive pages get the full program source injected server-side
if (isDeepDive) {
  if (page.filePaths.length > 0) {
    requestBody.filePath = page.filePaths[0];
  } else {
    // Structure model violated the one-file-per-analysis-page rule; without
    // filePath the page silently degrades to shallow RAG-only output.
    console.warn(`Deep-dive page ${page.id} has no filePaths — full-source injection skipped; page will be shallow`);
  }
}
```
(The backend reads `request.filePath` → `get_file_content` → `<currentFileContent>`; Task 1's budget logic protects small-context providers. The warn-don't-fail choice is deliberate: a shallow page plus a console warning beats a hard failure of the whole generation run; Task 6's checker will catch the shallow page.)

- [ ] **Step 3: Type-check / build**

```bash
npx tsc --noEmit 2>&1 | tail -5   # or npx next build --no-lint
```
Expected: no new errors.

- [ ] **Step 4: Commit**

```bash
git add "src/app/[owner]/[repo]/page.tsx"
git commit -m "Generate Program Analysis pages from full source with exhaustive prompt"
```

---

### Task 5: Parity/exceed checker script

**Files:**
- Create: `scripts/check_deep_dive.py`

- [ ] **Step 1: Write the checker**

```python
#!/usr/bin/env python3
"""Score a generated Program Analysis page against its COBOL source.

Usage: python3 scripts/check_deep_dive.py <wiki_cache.json> <page_id> <source_file>

Checks (exceed-the-PDF criteria):
1. Every PROCEDURE DIVISION paragraph name appears in the page.
2. Every 01/77-level working-storage name appears in the page.
3. All 12 required section headings are present.
4. Page length comfortably exceeds the 8-page reference (~8.6k chars).
Exit code 0 = all pass, 1 = any failure (prints a coverage report).
"""
import json
import re
import sys


REQUIRED_HEADINGS = [
    "Program Identification", "Environment & File Definitions",
    "Copybooks", "Working-Storage Inventory", "Paragraph Inventory",
    "Paragraph-by-Paragraph", "Control & Restart", "Data Flow",
    "Error Handling", "Cross-Program", "Gotchas", "Glossary",
]
MIN_CHARS = 25_000  # ~3x the reference PDF's text volume


def cobol_paragraphs(src: str) -> list[str]:
    """Paragraph/section labels in the PROCEDURE DIVISION (area-A labels ending '.')."""
    m = re.search(r"PROCEDURE\s+DIVISION", src, re.IGNORECASE)
    body = src[m.end():] if m else src
    # Fixed-column COBOL: cols 1-6 sequence area (spaces in this codebase),
    # col 7 indicator, Area A starts at col 8 -> labels carry ~7 leading spaces.
    names = re.findall(r"^ {6,8}([A-Z0-9][A-Z0-9-]{2,30})\s*(?:SECTION\s*)?\.\s*$",
                       body, re.MULTILINE)
    seen, out = set(), []
    for n in names:
        if n not in seen and n not in {"EXIT", "GOBACK"}:
            seen.add(n)
            out.append(n)
    return out


def ws_items(src: str) -> list[str]:
    """01/77-level names in WORKING-STORAGE."""
    m = re.search(r"WORKING-STORAGE\s+SECTION", src, re.IGNORECASE)
    n = re.search(r"PROCEDURE\s+DIVISION", src, re.IGNORECASE)
    body = src[m.end(): n.start() if n else None] if m else ""
    return sorted(set(re.findall(r"^\s*(?:01|77)\s+([A-Z0-9][A-Z0-9-]+)", body, re.MULTILINE)))


def main():
    cache_path, page_id, source_path = sys.argv[1], sys.argv[2], sys.argv[3]
    cache = json.load(open(cache_path))
    page = cache["generated_pages"].get(page_id)
    if not page:
        print(f"FAIL: page {page_id} not in cache; pages: {list(cache['generated_pages'])}")
        sys.exit(1)
    content = page["content"]
    src = open(source_path, encoding="utf-8", errors="replace").read()

    ok = True
    paras = cobol_paragraphs(src)
    missing_p = [p for p in paras if p not in content]
    print(f"paragraph coverage: {len(paras) - len(missing_p)}/{len(paras)}"
          + (f"  MISSING: {missing_p}" if missing_p else ""))
    ok &= not missing_p

    items = ws_items(src)
    missing_w = [w for w in items if w not in content]
    print(f"working-storage 01/77 coverage: {len(items) - len(missing_w)}/{len(items)}"
          + (f"  MISSING: {missing_w}" if missing_w else ""))
    ok &= not missing_w

    missing_h = [h for h in REQUIRED_HEADINGS if h.lower() not in content.lower()]
    print(f"required headings: {len(REQUIRED_HEADINGS) - len(missing_h)}/{len(REQUIRED_HEADINGS)}"
          + (f"  MISSING: {missing_h}" if missing_h else ""))
    ok &= not missing_h

    print(f"page length: {len(content)} chars (minimum {MIN_CHARS})")
    ok &= len(content) >= MIN_CHARS

    print("RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
```
Note for non-English output: headings check is the only language-sensitive one — if the user regenerates in zh-TW, run the checker with `REQUIRED_HEADINGS` reduced to the numeric prefixes ("## 1." … "## 12.") instead; paragraph/field-name checks are language-neutral (COBOL identifiers stay Latin). Implement that as a fallback: if fewer than half the headings match, check for `## 1.` … `## 12.` numeric headings instead before failing.

- [ ] **Step 2: Smoke-test the source-parsing half against the real file**

```bash
python3 -c "
import sys; sys.path.insert(0, 'scripts')
from check_deep_dive import cobol_paragraphs, ws_items
src = open('/home/ubuntu/.adalflow/repos/poc_code1_cbl_bv401/BV401.txt', encoding='utf-8', errors='replace').read()
ps, ws = cobol_paragraphs(src), ws_items(src)
print(len(ps), 'paragraphs:', ps[:10])
print(len(ws), 'ws items:', ws[:10])
assert len(ps) >= 5 and len(ws) >= 3, 'parser found implausibly little - fix the regexes against the real source format'
"
```
Expected: plausible counts (BV401 has at least A000/B000/B200/C100/C200/C300/B900-style paragraphs). **If the regexes miss the actual source layout (e.g. line-numbered columns 1-6), adjust them against the real file until counts are right — this step exists precisely to calibrate the parser.**

- [ ] **Step 3: Commit**

```bash
git add scripts/check_deep_dive.py
git commit -m "Add deep-dive parity checker (paragraph/field/heading coverage)"
```

---

### Task 6: Deploy, regenerate, verify against the PDF

**Files:** none (deployment + verification)

- [ ] **Step 1: Rebuild and restart**

```bash
docker compose up -d --build
```
Expected: container healthy.

- [ ] **Step 2: Regenerate the BV401 wiki (user action)**

Ask the user to open the deepwiki UI for `poc_code1_cbl_bv401`, pick the provider/model (recommend Claude → `claude-opus-4-8` or haiku; sonnet may still 429), and trigger a wiki refresh (force regenerate so the structure prompt re-runs). Confirm in `api/logs/application.log` that page generation ran and (for claude) `Claude usage:` lines show large prompt_tokens for the analysis page (full source injected — expect ≳8k prompt tokens vs ~2k for normal pages).

- [ ] **Step 3: Run the parity checker**

```bash
ls ~/.adalflow/wikicache/ | grep bv401   # find the fresh cache file
python3 scripts/check_deep_dive.py \
  ~/.adalflow/wikicache/deepwiki_cache_gitlab_poc_code1_cbl_bv401_en.json \
  page-analysis-bv401 \
  ~/.adalflow/repos/poc_code1_cbl_bv401/BV401.txt
```
Expected: `RESULT: PASS` (full paragraph coverage, full WS coverage, 12/12 headings, ≥25k chars). The page id comes from the structure model — if it chose a slightly different id, list pages via the checker's error output and rerun with the actual id; if the model ignored the id convention entirely, tighten the structure-prompt wording (Task 3) and regenerate.

- [ ] **Step 4: Manual spot-check against the PDF**

Open the new page in the UI side-by-side with `BV401.pdf` §4 (the per-paragraph analysis) and §9 (gotchas). Verify: every PDF paragraph section has a counterpart with equal or more detail; flowcharts render; gotchas are evidence-based with line citations; and the page contains material the PDF lacks (exhaustive WS table, field-level mapping, error-handling inventory, glossary).

- [ ] **Step 5: Iterate if needed**

If coverage fails: the usual culprits are (a) output truncation — check whether the page ends mid-sentence; raise claude max_tokens further or note vLLM's server-side cap; (b) the model summarizing paragraphs together — strengthen the COMPLETENESS RULES wording; (c) structure model not emitting analysis pages — tighten section-4 wording. Fix, rebuild, regenerate, re-run the checker.

- [ ] **Step 6: Commit any tuning + the plan checkboxes**

```bash
git add -A docs/ src/ api/ scripts/
git commit -m "Tune deep-dive generation after BV401 verification"
```

---

## Self-review checklist (run after writing, before execution)

- Spec coverage: structure section ✅ (Task 3), exhaustive per-program page ✅ (Task 4), full-source injection ✅ (Task 4 step 2 + existing backend), provider budgets ✅ (Task 1), output budget ✅ (Task 2), beats-the-PDF verification ✅ (Tasks 5-6).
- The 147k-token B5349 case: claude swallows it whole (800k budget); vllm gets head+tail truncation with marker — documented behavior, not silent.
- Existing Wiki/TSD/BRD pages: untouched prompts, untouched flow; only additive changes.
