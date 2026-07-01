# TSD Content Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop TSD chapters from repeating a general project intro (consolidate it in the Introduction chapter) and make Program Inventory lead with a `Program | Function | Key Logic` table.

**Architecture:** Two prompt-text changes in `api/wiki_prompts.py` — a new `omit_general_intro` flag on `build_page_prompt` that swaps the per-page "Introduction" instruction, and a stricter Program Inventory outline — plus a one-line wiring in `run_generation` to set the flag for `page-tsd-*` pages. Takes effect on regeneration.

**Tech Stack:** Python 3.12 / FastAPI, pytest. Tests: `.venv/bin/python -m pytest`.

## Global Constraints

- Scope: TSD template pages only (`page-tsd-*`). BRD (`page-brd-*`), developer Wiki pages, and deep-dive `page-analysis-*` pages keep current behaviour.
- `omit_general_intro` defaults to `False` so all existing callers/tests are unaffected.
- No change to the completeness checker / header guard (the `## Program Inventory` heading text is unchanged; no headings added/removed).
- Prompt-only change; effect is on future generations — requires a regen to observe.
- Backend tests run via `.venv/bin/python -m pytest tests/unit/test_wiki_prompts.py -q` (system python/pytest absent). Do NOT import `api.api` in a unit test (log-hostile in this sandbox).

---

### Task 1: Prompt changes in `api/wiki_prompts.py` (intro flag + Program Inventory table)

**Files:**
- Modify: `api/wiki_prompts.py` (`build_page_prompt` signature ~407-410; the standard-branch "Introduction" instruction at line 617; `TSD_BRD_OUTLINES["page-tsd-program-inventory"]`)
- Test: `tests/unit/test_wiki_prompts.py`

**Interfaces:**
- Produces: `build_page_prompt(page_title, file_paths, language, deep_dive, repo_url, repo_type, default_branch, required_outline=None, omit_general_intro=False) -> str`. When `omit_general_intro=True`, the standard page prompt replaces the general "Introduction" instruction with a "do not write a general introduction" instruction.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/unit/test_wiki_prompts.py
from api.wiki_prompts import build_page_prompt, TSD_BRD_OUTLINES


def _page_prompt(**kw):
    base = dict(page_title="系統概觀 (System Overview)", file_paths=["BV401.txt"],
                language="zh-tw", deep_dive=False, repo_url="https://x/repo",
                repo_type="gitlab", default_branch="main",
                required_outline=TSD_BRD_OUTLINES["page-tsd-system-overview"])
    base.update(kw)
    return build_page_prompt(**base)


def test_omit_general_intro_replaces_the_intro_instruction():
    p = _page_prompt(omit_general_intro=True)
    assert "concise introduction (1-2 paragraphs)" not in p
    assert "Do NOT open with a general introduction" in p


def test_default_keeps_the_general_intro_instruction():
    p = _page_prompt()  # omit_general_intro defaults to False (BRD/Wiki behaviour)
    assert "concise introduction (1-2 paragraphs)" in p
    assert "Do NOT open with a general introduction" not in p


def test_program_inventory_requires_a_table():
    outline = TSD_BRD_OUTLINES["page-tsd-program-inventory"]
    assert "Program | Function | Key Logic" in outline
    assert "REQUIRED" in outline
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_wiki_prompts.py -k "omit_general_intro or program_inventory_requires" -q`
Expected: FAIL — `omit_general_intro` is not a valid keyword argument yet; and the Program Inventory outline lacks `"Program | Function | Key Logic"`/`"REQUIRED"`.

- [ ] **Step 3a: Add the `omit_general_intro` parameter**

In `api/wiki_prompts.py`, change the `build_page_prompt` signature (currently lines 407-410) to add the new keyword parameter:

```python
def build_page_prompt(page_title: str, file_paths: List[str], language: str,
                      deep_dive: bool, repo_url: str, repo_type: str,
                      default_branch: str,
                      required_outline: Optional[str] = None,
                      omit_general_intro: bool = False) -> str:
```

- [ ] **Step 3b: Compute the intro instruction and use it**

In `build_page_prompt`, immediately AFTER the `outline_clause = ...` block ends (the block that starts `outline_clause = ""` and the `if required_outline:` that follows it) and BEFORE the `if deep_dive:` return, add:

```python
    # TSD chapters (omit_general_intro) must not each repeat a general project
    # overview — that lives once in the Introduction chapter. Other pages keep
    # the standard 1-2 paragraph introduction.
    if omit_general_intro:
        intro_instruction = (
            "1.  **No general introduction:** Do NOT open with a general "
            "introduction or a project/overview paragraph — the dedicated "
            "Introduction chapter already covers the overall purpose, scope, and "
            "high-level overview. Begin directly with this page's required "
            "template sections below.\n"
        )
    else:
        intro_instruction = (
            f"1.  **Introduction:** Start with a concise introduction (1-2 paragraphs) explaining the purpose, scope, and high-level overview of \"{page_title}\" within the context of the overall project. If relevant, and if information is available in the provided files, link to other potential wiki pages using the format `[Link Text](#page-anchor-or-id)`.\n"
        )
```

Then, in the standard (non-deep-dive) `return (...)` concatenation, REPLACE the existing line 617:

```python
            f"1.  **Introduction:** Start with a concise introduction (1-2 paragraphs) explaining the purpose, scope, and high-level overview of \"{page_title}\" within the context of the overall project. If relevant, and if information is available in the provided files, link to other potential wiki pages using the format `[Link Text](#page-anchor-or-id)`.\n"
```

with:

```python
            + intro_instruction +
```

(Keep the surrounding lines — the `f"...H1 heading: # {page_title}"` line, the `outline_clause`, and the `"Based ONLY on the content..."` line — exactly as they are; only the one Introduction line is replaced by the `+ intro_instruction +` splice.)

- [ ] **Step 3c: Make Program Inventory require a table**

In `TSD_BRD_OUTLINES`, replace the `page-tsd-program-inventory` entry:

```python
    "page-tsd-program-inventory": (
        "## Program Inventory\n"
        "(One ### subsection per program/module: business function and key logic. "
        "May also summarise as a table: Program | Business Function | Key Logic.)\n"
    ),
```

with:

```python
    "page-tsd-program-inventory": (
        "## Program Inventory\n"
        "(REQUIRED: FIRST a summary table with columns `Program | Function | Key Logic` "
        "— one row per program/module (the source program members). THEN, below the "
        "table, one `###` subsection per program with its brief key points. Do not "
        "omit the table.)\n"
    ),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_wiki_prompts.py -q`
Expected: PASS (all — the 3 new tests plus the pre-existing wiki_prompts tests, including the Task-4 bilingual-row test from the earlier feature).

- [ ] **Step 5: Commit**

```bash
git add api/wiki_prompts.py tests/unit/test_wiki_prompts.py
git commit -m "feat: consolidate TSD intros (omit_general_intro) + require Program Inventory table"
```

---

### Task 2: Wire the flag in run_generation + version bump + e2e regen

**Files:**
- Modify: `api/wiki_generator.py` (the `build_page_prompt(...)` call at ~line 464-467)
- Modify: `src/version.ts`

**Interfaces:**
- Consumes: `build_page_prompt(..., omit_general_intro=...)` (Task 1).

- [ ] **Step 1: Pass the flag for TSD pages**

In `api/wiki_generator.py`, the call currently reads (≈lines 464-467):

```python
            page_inner = build_page_prompt(
                page["title"], page["filePaths"], job.language, is_deep_dive,
                repo_url, repo.type, default_branch,
                required_outline=TSD_BRD_OUTLINES.get(page["id"]))
```

Change it to also pass the flag (TSD template pages only):

```python
            page_inner = build_page_prompt(
                page["title"], page["filePaths"], job.language, is_deep_dive,
                repo_url, repo.type, default_branch,
                required_outline=TSD_BRD_OUTLINES.get(page["id"]),
                omit_general_intro=page["id"].startswith("page-tsd-"))
```

- [ ] **Step 2: Verify syntax + no regression**

Run: `.venv/bin/python -c "import ast; ast.parse(open('api/wiki_generator.py').read()); print('syntax OK')"`
Expected: `syntax OK`

Run: `.venv/bin/python -m pytest tests/unit/test_wiki_prompts.py tests/unit/test_tsd_brd_completeness.py -q`
Expected: PASS (no regression to the imported modules).

- [ ] **Step 3: Bump APP_VERSION + commit code**

Edit `src/version.ts`, increment the patch (`0.3.26` → `0.3.27`). Then:

```bash
git add api/wiki_generator.py src/version.ts
git commit -m "feat: apply omit_general_intro to TSD pages; bump APP_VERSION"
```

- [ ] **Step 4: Rebuild + redeploy**

```bash
docker compose build deepwiki
docker compose up -d --no-deps deepwiki
```
Expected: builds (exit 0); healthy; `curl -s http://localhost:3000 | grep -oE '0\.3\.[0-9]+'` shows `0.3.27`.

- [ ] **Step 5: Regenerate 401 (haiku) and verify the content changes**

Submit a `force_regenerate` job for `code1_cbl_bv401` (provider `claude`, model `claude-haiku-4-5-20251001`, language `zh-tw`, comprehensive, self_review) via `POST /api/wiki_jobs` with the repo token; wait for `done`. Then inspect the regenerated cache:

```bash
docker exec -i deepwiki-open-deepwiki-1 python3 - <<'PY'
import json
d=json.load(open("/root/.adalflow/wikicache/deepwiki_cache_gitlab_poc_code1_cbl_bv401_zh-tw~claude~claude-haiku-4-5-20251001.json"))
gp=d.get("generated_pages") or {}
inv=(gp.get("page-tsd-program-inventory") or {}).get("content","")
print("program-inventory has a table:", "|" in inv and "---" in inv)
# spot-check a non-intro TSD chapter no longer opens with a generic overview heading
so=(gp.get("page-tsd-system-overview") or {}).get("content","")
print("system-overview first 200 chars after H1:\n", so[:400])
PY
```
Expected: `program-inventory has a table: True`, and the system-overview body begins with its template sections (System Platform / Impact Analysis …) rather than a general project-overview paragraph.

- [ ] **Step 6: Commit (version already committed in Step 3)**

No code change in this step; the version bump was committed in Step 3. If any follow-up tweak was needed, commit it here.

---

## Self-Review

**Spec coverage:**
- Change A (suppress general intro on TSD pages) → Task 1 Step 3a/3b (param + conditional instruction) + Task 2 Step 1 (wiring for `page-tsd-*`). ✓
- Change B (Program Inventory mandatory table) → Task 1 Step 3c. ✓
- TSD-only scope → Task 2 wiring keys on `page["id"].startswith("page-tsd-")`; default `False` leaves BRD/Wiki/deep-dive unchanged. ✓
- Introduction chapter still carries the consolidated overview → its `required_outline` (`## Purpose`/`## Document Overview`) is injected via the unchanged `outline_clause`, independent of `omit_general_intro`. ✓
- No completeness/guard impact → only the parenthetical prose of `page-tsd-program-inventory` changed; `## Program Inventory` heading unchanged. ✓
- Tests (intro on/off, table required) → Task 1 Step 1. ✓
- e2e regen → Task 2 Step 5. ✓

**Placeholder scan:** none — every code/test step has complete code.

**Type consistency:** `omit_general_intro: bool = False` added to `build_page_prompt`'s signature (Task 1) and passed by keyword in `run_generation` (Task 2); `intro_instruction` is a local `str` spliced into the existing concatenation. Program Inventory test strings (`"Program | Function | Key Logic"`, `"REQUIRED"`) match the outline text written in Step 3c exactly.
