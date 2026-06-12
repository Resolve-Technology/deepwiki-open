# Deep-Dive Citation Accuracy (A + C) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Make deep-dive program-analysis pages cite *real* line numbers and stop fabricating filenames (e.g. `CAL101.txt`) for COBOL `CALL` targets.

**Architecture:** Two independent fixes to the deep-dive generation path. **(A)** Prefix every line of the injected program source with its 1-based line number *before* it goes into the prompt, and tell the model to cite those numbers — the model currently receives unnumbered source and guesses. **(B/“C”)** Tighten the deep-dive prompt so the model only emits file citations for files actually provided, and writes `CALL` targets as bare program identifiers rather than inventing `[name.txt:…]()` links.

**Tech Stack:** Python 3 (FastAPI backend, `api/`), pytest unit tests (`tests/unit/`). No network in tests — generation uses a fake dispatch.

**Why this is correct (root cause):**
- The deep-dive prompt (`api/wiki_prompts.py:313`) orders the model to *“Cite line numbers for every claim”*, but the source is injected raw and unnumbered via `assemble_envelope` (`api/prompt_assembly.py:136`). LLMs cannot reliably count lines → wrong numbers.
- Section 10 of the same prompt (`api/wiki_prompts.py:355-356`) tells the model to document *“Called programs (CALL statements)”*. In COBOL `CALL 'CAL101'` names a **program**, not a file; the model turns it into a `[CAL101.txt:…]()` citation for a file that does not exist.

**Design constraints (do not violate):**
- `assemble_envelope` is byte-parity-locked to the websocket flow (`tests/unit/test_prompt_assembly.py:24-46`). **Do NOT add numbering inside `assemble_envelope`** — it would change the websocket chat path too and break parity. Number the source in `wiki_generator.py`, before the call.
- Numbering must happen **before** `fit_to_budget` truncation (which lives inside `assemble_envelope`). Because we number the true source first, the surviving head/tail keep their real line numbers even when the middle is dropped — exactly what we want.
- Standard (non-deep-dive) pages never inject `file_content` (only the `is_deep_dive` branch sets it — `api/wiki_generator.py:355`), so these changes affect deep-dive pages only.

---

## File Structure

- `api/prompt_assembly.py` — add `number_source_lines(content)` helper (pure string function, no side effects). Lives here because it’s prompt-assembly concern; imported by `wiki_generator.py`.
- `api/wiki_generator.py` — call `number_source_lines` on fetched deep-dive `file_content` before assembling the envelope.
- `api/wiki_prompts.py` — reword the deep-dive prompt: (a) state that the source is line-numbered and to cite those numbers; (b) forbid fabricated file citations and define how to write `CALL` targets.
- `tests/unit/test_prompt_assembly.py` — unit tests for `number_source_lines`.
- `tests/unit/test_wiki_generator.py` — update the existing `test_deep_dive_file_injection` (content is now numbered) + add a fetch-failure-still-no-injection guard already exists.
- `tests/unit/test_wiki_prompts.py` — anchor tests for the new deep-dive prompt wording.
- `src/version.ts` — bump `APP_VERSION` (required before any image build, per project convention).

**Canonical line-number format (used everywhere in this plan):** `f"{n:>6} | {line}"` → a 6-wide right-justified number, space, pipe, space, then the original line. Example: source line 1 `IDENTIFICATION DIVISION.` becomes `     1 | IDENTIFICATION DIVISION.`.

---

## Task 1: `number_source_lines` helper

**Files:**
- Modify: `api/prompt_assembly.py` (add function near the top-level helpers, after `format_context_text` ~line 105)
- Test: `tests/unit/test_prompt_assembly.py`

- [x] **Step 1: Write the failing tests**

Add to `tests/unit/test_prompt_assembly.py` (import line at top becomes
`from api.prompt_assembly import (assemble_envelope, format_context_text, number_source_lines, select_generation_system_prompt)`):

```python
def test_number_source_lines_prefixes_each_line():
    out = number_source_lines("ALPHA\nBETA\nGAMMA")
    assert out == (
        "     1 | ALPHA\n"
        "     2 | BETA\n"
        "     3 | GAMMA"
    )


def test_number_source_lines_empty_returns_empty():
    # Empty stays empty so the envelope's "no file content" path is untouched.
    assert number_source_lines("") == ""


def test_number_source_lines_drops_trailing_blank_line():
    # splitlines() means a single trailing newline does not create a phantom
    # numbered blank line.
    assert number_source_lines("X\n") == "     1 | X"


def test_number_source_lines_preserves_blank_interior_lines():
    out = number_source_lines("A\n\nB")
    assert out == "     1 | A\n     2 | \n     3 | B"


def test_number_source_lines_normalizes_crlf():
    # COBOL .txt sources are often CRLF; numbering normalizes to LF rows.
    out = number_source_lines("A\r\nB")
    assert out == "     1 | A\n     2 | B"
```

- [x] **Step 2: Run tests to verify they fail**

Run: `cd /home/ubuntu/deepwiki-open && python -m pytest tests/unit/test_prompt_assembly.py -k number_source_lines -v`
Expected: FAIL — `ImportError: cannot import name 'number_source_lines'`.

- [x] **Step 3: Implement the helper**

In `api/prompt_assembly.py`, immediately after the `format_context_text` function (after line 104), add:

```python
def number_source_lines(content: str) -> str:
    """Prefix each source line with its 1-based line number.

    Deep-dive pages order the model to cite exact line numbers, but the raw
    program source carries no line markers — the model would have to count
    lines, which it does unreliably. Numbering the source first gives it ground
    truth to cite. Done in the generator (not in ``assemble_envelope``) so the
    websocket chat path stays byte-identical, and BEFORE budget-fit truncation
    so surviving lines keep their true numbers.
    """
    if not content:
        return ""
    return "\n".join(
        f"{n:>6} | {line}"
        for n, line in enumerate(content.splitlines(), start=1)
    )
```

- [x] **Step 4: Run tests to verify they pass**

Run: `cd /home/ubuntu/deepwiki-open && python -m pytest tests/unit/test_prompt_assembly.py -v`
Expected: PASS (all existing parity tests + the 5 new ones).

- [x] **Step 5: Commit**

```bash
cd /home/ubuntu/deepwiki-open
git add api/prompt_assembly.py tests/unit/test_prompt_assembly.py
git commit -m "feat: number_source_lines helper for deep-dive citation accuracy"
```

---

## Task 2: Inject numbered source in the deep-dive generation path

**Files:**
- Modify: `api/wiki_generator.py:29` (import) and `api/wiki_generator.py:358-363` (numbering after fetch)
- Test: `tests/unit/test_wiki_generator.py:303-320` (update existing `test_deep_dive_file_injection`)

- [x] **Step 1: Update the existing test to expect numbered content (this is the failing test)**

In `tests/unit/test_wiki_generator.py`, the current `test_deep_dive_file_injection` stubs `get_file_content` to return `"COBOL SOURCE LINES"` and asserts the raw string is injected. Change the fake to a multi-line source and assert the **numbered** form. Replace the body of `test_deep_dive_file_injection` (lines 303-320) with:

```python
def test_deep_dive_file_injection(monkeypatch):
    xml = """<wiki_structure><title>T</title><description>D</description><pages>
      <page id="page-analysis-prog"><title>Prog Analysis</title>
        <relevant_files><file_path>prog.cbl</file_path></relevant_files></page>
    </pages></wiki_structure>"""
    monkeypatch.setattr(wiki_generator, "get_file_content",
                        lambda *a, **k: "IDENTIFICATION DIVISION.\nPROGRAM-ID. PROG.")
    job = make_job(self_review=True)
    dispatch = FakeDispatch([xml, PAGE_BODY, "NO_CHANGES"])

    run(run_generation(job, dispatch))

    gen_prompt, review_prompt = dispatch.prompts[1], dispatch.prompts[2]
    expected_block = (
        '<currentFileContent path="prog.cbl">\n'
        "     1 | IDENTIFICATION DIVISION.\n"
        "     2 | PROGRAM-ID. PROG.\n"
        "</currentFileContent>"
    )
    for p in (gen_prompt, review_prompt):  # numbered content carries into review too
        assert expected_block in p
    assert "senior mainframe/COBOL systems analyst" in gen_prompt
    # Deep-dive generation retrieval uses the filePath-focused query
    assert ("Contexts related to prog.cbl", "en") in FakeRAG.instances[0].queries
```

- [x] **Step 2: Run the test to verify it fails**

Run: `cd /home/ubuntu/deepwiki-open && python -m pytest tests/unit/test_wiki_generator.py::test_deep_dive_file_injection -v`
Expected: FAIL — the prompt still contains the un-numbered `IDENTIFICATION DIVISION.` without the `     1 | ` prefix.

- [x] **Step 3: Wire numbering into the generator**

In `api/wiki_generator.py`, line 29, add `number_source_lines` to the import:

```python
from api.prompt_assembly import (assemble_envelope, format_context_text,
                                 number_source_lines,
                                 select_generation_system_prompt)
```

(Keep whatever other names are already on that import line — append `number_source_lines` to the existing tuple.)

Then in the deep-dive fetch block (currently lines 358-363), number the content right after a successful fetch:

```python
                try:
                    file_content = await asyncio.to_thread(
                        get_file_content, repo_url, file_path, repo.type, repo.token)
                    # Line-number the source so the model cites real line numbers
                    # instead of guessing; must precede the budget-fit truncation
                    # inside assemble_envelope so kept lines keep their true numbers.
                    file_content = number_source_lines(file_content)
                except Exception as e:
                    logger.error(f"Error retrieving file content: {str(e)}")
                    file_content, file_path = "", ""
```

- [x] **Step 4: Run the test to verify it passes**

Run: `cd /home/ubuntu/deepwiki-open && python -m pytest tests/unit/test_wiki_generator.py -v`
Expected: PASS — including the unchanged `test_deep_dive_file_fetch_failure_proceeds` (still no `<currentFileContent>` when the fetch raises, because `file_content` stays `""`).

- [x] **Step 5: Commit**

```bash
cd /home/ubuntu/deepwiki-open
git add api/wiki_generator.py tests/unit/test_wiki_generator.py
git commit -m "fix: line-number deep-dive source before injection so citations are real"
```

---

## Task 3: Reword the deep-dive prompt (cite numbered lines; no fabricated files)

**Files:**
- Modify: `api/wiki_prompts.py:313` (intro/citation sentence) and `api/wiki_prompts.py:355-356` (Section 10 `CALL` targets)
- Test: `tests/unit/test_wiki_prompts.py` (add anchor tests)

This task only edits the `if deep_dive:` branch string of `build_page_prompt`. Make two edits.

- [x] **Step 1: Write the failing anchor tests**

Add to `tests/unit/test_wiki_prompts.py` (after `test_deep_dive_prompt_anchors`, ~line 133):

```python
def test_deep_dive_prompt_describes_line_numbering():
    p = build_page_prompt("My Program", ["prog.cbl"], "en", True, *REPO)
    # The model is told the source is pre-numbered and to cite those numbers.
    assert "Each line in [CURRENT_FILE_CONTENT] is prefixed with its line number" in p
    assert "Cite those exact line numbers" in p


def test_deep_dive_prompt_forbids_fabricated_file_citations():
    p = build_page_prompt("My Program", ["prog.cbl"], "en", True, *REPO)
    assert "Only cite files that were actually provided" in p
    # CALL targets must be written as program identifiers, not invented files.
    assert "is a program name, not a file" in p
    assert "Never fabricate a filename" in p
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `cd /home/ubuntu/deepwiki-open && python -m pytest tests/unit/test_wiki_prompts.py -k "deep_dive_prompt_describes_line_numbering or deep_dive_prompt_forbids_fabricated" -v`
Expected: FAIL — assertion errors (strings not present yet).

- [x] **Step 3a: Edit the intro/citation sentence (line 313)**

In `api/wiki_prompts.py`, replace the existing line 313 string:

```python
            f"You are given the COMPLETE source of the program in [CURRENT_FILE_CONTENT]. Base EVERY statement strictly on that source (plus any copybook files provided). Never invent fields, paragraphs, or behavior. Cite line numbers for every claim using the format [{first_file}:start-end]().\n"
```

with:

```python
            f"You are given the COMPLETE source of the program in [CURRENT_FILE_CONTENT]. Base EVERY statement strictly on that source (plus any copybook files provided). Never invent fields, paragraphs, or behavior. Each line in [CURRENT_FILE_CONTENT] is prefixed with its line number in the form `<number> | <code>`. Cite those exact line numbers for every claim using the format [{first_file}:start-end](); do NOT guess or renumber. Only cite files that were actually provided to you (the file(s) in the 'Relevant source files' list and any copybooks in context). Never fabricate a filename: a CALL target such as `CAL101` is a program name, not a file — write it as `CAL101`, never as a citation link like [CAL101.txt:1-10]().\n"
```

- [x] **Step 3b: Edit Section 10 (lines 355-356) to reinforce the `CALL`-target rule**

In `api/wiki_prompts.py`, replace the Section 10 body string (line 356):

```python
            "Called programs (CALL statements), callers if inferable from comments, shared files that couple this program to others, JCL/scheduling hints found in comments.\n"
```

with:

```python
            "Called programs (CALL statements) — list each as its bare program identifier in `backticks` (e.g. `CAL101`); do NOT turn a called program into a file citation unless that program's own source file was actually provided. Callers if inferable from comments, shared files that couple this program to others, JCL/scheduling hints found in comments.\n"
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `cd /home/ubuntu/deepwiki-open && python -m pytest tests/unit/test_wiki_prompts.py -v`
Expected: PASS — new anchor tests pass; all existing deep-dive anchor tests (`test_deep_dive_prompt_anchors`, `test_page_prompt_deep_dive_wiki_page_topic`, etc.) still pass (their asserted substrings were not removed).

- [x] **Step 5: Commit**

```bash
cd /home/ubuntu/deepwiki-open
git add api/wiki_prompts.py tests/unit/test_wiki_prompts.py
git commit -m "fix: deep-dive prompt cites numbered lines and forbids fabricated file citations"
```

---

## Task 4: Full backend test sweep + version bump

**Files:**
- Modify: `src/version.ts:9`

- [x] **Step 1: Run the full unit suite**

Run: `cd /home/ubuntu/deepwiki-open && python -m pytest tests/unit -q`
Expected: PASS (no regressions across `test_prompt_assembly.py`, `test_wiki_generator.py`, `test_wiki_prompts.py`, and the rest).

- [x] **Step 2: Bump the app version**

Project convention (and memory) require bumping `APP_VERSION` before any image build. In `src/version.ts`, change:

```ts
export const APP_VERSION = '0.3.4';
```

to:

```ts
export const APP_VERSION = '0.3.5';
```

- [x] **Step 3: Commit**

```bash
cd /home/ubuntu/deepwiki-open
git add src/version.ts
git commit -m "chore: bump APP_VERSION to 0.3.5 (deep-dive citation accuracy)"
```

---

## Out of scope (deferred — see prior analysis)

- **RAG-context line numbers (“D”):** chunks in `<START_OF_CONTEXT>` still carry no line numbers (`Document.meta_data` has no line range — `api/data_pipeline.py:367`). Citations the model derives from RAG chunks rather than the primary `[CURRENT_FILE_CONTENT]` remain ungrounded. Fixing needs line-range metadata at chunk time + surfacing it in `format_context_text`.
- **Empty-`file_content` hard-fail (“B” from the analysis):** when the GitLab fetch fails / token is missing, the deep-dive page is still generated from nothing. Worth a separate guard, but it’s a behavior change, not a citation-accuracy fix.

## Verification after deploy (manual)

Regenerate one known deep-dive page (a COBOL program with a `CALL`) and confirm:
1. Cited line numbers land on the claimed code when you open the file at that line.
2. `CALL` targets render as `CAL101` (plain code), not as a dead `[CAL101.txt:…]()` link.
Per the `deepwiki-rag-index-fix` memory, affected wikis need a force-regenerate to pick up the new prompt.
