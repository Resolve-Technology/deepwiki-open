# Citation-Grounded Page Regeneration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Save a wiki page only after its citations verify — correct ungrounded claims with a targeted fix loop, strip the ones that remain, and never mutate when verification is untrustworthy (embedder outage).

**Architecture:** A new pure module strips fully-unverified markdown blocks; a new prompt asks the model to fix/remove only the broken-cited claims; a `ground_page_citations` helper in `wiki_generator.py` runs verify → correct loop (max 2) → strip, guarded by a non-empty `repo_map`. Reuses the existing `citation_grounding`, `assemble_envelope`, and `timed_dispatch` machinery.

**Tech Stack:** Python 3.11, pytest (run via `.venv/bin/python -m pytest`), FastAPI server-side generation engine.

**Spec:** `docs/superpowers/specs/2026-06-13-citation-grounded-page-regeneration-design.md`

---

## File Structure

- **Create** `api/citation_stripping.py` — `strip_unverified_claims(content, citations)`. Pure, no I/O. Mirrors the style of `api/citation_grounding.py`.
- **Modify** `api/wiki_prompts.py` — add `build_citation_fix_prompt(...)` next to `build_self_review_prompt`.
- **Modify** `api/wiki_generator.py` — add `MAX_CITATION_FIX_ATTEMPTS`, `GroundingContext`, `_verify_citations`, `ground_page_citations`; wire the helper into `run_generation` (replaces the inline citation tail ~lines 451–468).
- **Create** `tests/unit/test_citation_stripping.py`.
- **Modify** `tests/unit/test_wiki_prompts.py` — add the fix-prompt test.
- **Modify** `tests/unit/test_wiki_generator.py` — add 3 integration tests (correct-clean, strip-after-exhaust, outage-guard).

**Test command convention (host has no global pytest):**
`.venv/bin/python -m pytest tests/unit/<file>.py -v`

---

## Task 0: Branch

- [ ] **Step 1: Create a working branch** (repo is on `main`)

Run:
```bash
cd /home/ubuntu/deepwiki-open && git checkout -b citation-grounded-regen
```
Expected: `Switched to a new branch 'citation-grounded-regen'`

---

## Task 1: `strip_unverified_claims` pure module

**Files:**
- Create: `api/citation_stripping.py`
- Test: `tests/unit/test_citation_stripping.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_citation_stripping.py`:
```python
"""Tests for api/citation_stripping.strip_unverified_claims."""
from api.citation_stripping import strip_unverified_claims


def test_drops_block_whose_citations_are_all_broken():
    content = (
        "## Heading\n\n"
        "Real claim. Sources: [a.py:1-2]()\n\n"
        "Fabricated claim. Sources: [ghost.py:9-9]()"
    )
    citations = {
        "a.py:1-2": {"status": "verified"},
        "ghost.py:9-9": {"status": "broken"},
    }
    out = strip_unverified_claims(content, citations)
    assert "Fabricated claim" not in out
    assert "ghost.py" not in out
    assert "Real claim" in out
    assert "## Heading" in out


def test_keeps_block_with_one_verified_citation():
    content = "Mixed claim. Sources: [a.py:1-2]() [ghost.py:9-9]()"
    citations = {
        "a.py:1-2": {"status": "verified"},
        "ghost.py:9-9": {"status": "broken"},
    }
    out = strip_unverified_claims(content, citations)
    assert "Mixed claim" in out
    assert "ghost.py:9-9" in out


def test_keeps_block_with_no_citations():
    content = "Just prose, no citations here."
    assert strip_unverified_claims(content, {}) == "Just prose, no citations here."


def test_merges_standalone_sources_block_into_claim():
    # Claim and its Sources line separated by a blank line: must drop together.
    content = "Fabricated claim on its own line.\n\nSources: [ghost.py:9-9]()"
    citations = {"ghost.py:9-9": {"status": "broken"}}
    out = strip_unverified_claims(content, citations)
    assert out.strip() == ""


def test_ignores_non_citation_empty_links():
    # An empty link without a file extension is not a citation -> not a trigger.
    content = "See [the docs](). Sources: [a.py:1-2]()"
    citations = {"a.py:1-2": {"status": "verified"}}
    out = strip_unverified_claims(content, citations)
    assert "See" in out
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_citation_stripping.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'api.citation_stripping'`

- [ ] **Step 3: Write the implementation**

Create `api/citation_stripping.py`:
```python
"""Remove wiki-page claims whose citations all failed verification.

The grounding pass (citation_grounding.py) marks each `[file:lines]()` citation
verified or broken. After the correction loop has done its best, this module
deletes the markdown blocks that cite ONLY broken citations, so the saved page
carries no claim we could not ground. A block with at least one verified
citation, or no citations at all, is kept untouched. Pure module: no I/O.
"""
import re
from typing import Dict, List

from api.citation_grounding import parse_citation_label

# Same empty-href citation pattern as citation_grounding._EMPTY_LINK_RE.
_EMPTY_LINK_RE = re.compile(r"\[([^\]]+)\]\(\)")


def _citation_labels(block: str) -> List[str]:
    """Labels in a block that parse as real file:line citations."""
    return [m.strip() for m in _EMPTY_LINK_RE.findall(block)
            if parse_citation_label(m.strip()) is not None]


def _is_sources_only(block: str) -> bool:
    """True when a block is just a trailing `Sources: ...` line (no prose)."""
    return block.strip().lower().startswith("sources:")


def strip_unverified_claims(content: str, citations: Dict[str, dict]) -> str:
    """Drop every markdown block whose citations are all broken.

    ``citations`` is the {label: {status, ...}} map from verify_page_citations.
    A block is dropped iff it contains at least one citation present in
    ``citations`` and EVERY such citation has status "broken". Blocks with a
    verified citation, or with no citations, are kept. A standalone `Sources:`
    block is first merged into the preceding block so a claim and its citations
    are kept or dropped together (never orphaned).
    """
    raw_blocks = re.split(r"\n\s*\n", content)

    blocks: List[str] = []
    for b in raw_blocks:
        if blocks and _is_sources_only(b):
            blocks[-1] = blocks[-1].rstrip() + "\n" + b.strip()
        else:
            blocks.append(b)

    kept: List[str] = []
    for b in blocks:
        labels = _citation_labels(b)
        statuses = [citations[label]["status"] for label in labels
                    if label in citations]
        drop = bool(statuses) and all(s == "broken" for s in statuses)
        if not drop:
            kept.append(b.strip())

    return "\n\n".join(k for k in kept if k)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_citation_stripping.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
cd /home/ubuntu/deepwiki-open
git add api/citation_stripping.py tests/unit/test_citation_stripping.py
git commit -m "feat: strip_unverified_claims drops fully-unverified markdown blocks"
```

---

## Task 2: `build_citation_fix_prompt`

**Files:**
- Modify: `api/wiki_prompts.py` (add after `build_self_review_prompt`, ~line 542)
- Test: `tests/unit/test_wiki_prompts.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_wiki_prompts.py`:
```python
def test_build_citation_fix_prompt_lists_every_broken_citation():
    from api.wiki_prompts import build_citation_fix_prompt
    content = "# Page\n\nClaim. Sources: [ghost.py:9-9]()"
    broken = [
        ("ghost.py:9-9", "file not provided"),
        ("x.py:5-6", "lines not in provided source"),
    ]
    prompt = build_citation_fix_prompt(
        "My Page", ["a.py"], content, broken, "https://github.com/o/r")
    assert "ghost.py:9-9" in prompt
    assert "file not provided" in prompt
    assert "x.py:5-6" in prompt
    assert "lines not in provided source" in prompt
    assert content in prompt
    assert "My Page" in prompt
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_wiki_prompts.py::test_build_citation_fix_prompt_lists_every_broken_citation -v`
Expected: FAIL — `ImportError: cannot import name 'build_citation_fix_prompt'`

- [ ] **Step 3: Write the implementation**

In `api/wiki_prompts.py`, add immediately after `build_self_review_prompt` (after ~line 542):
```python
# ---------------------------------------------------------------------------
# Citation-fix prompt (correct or remove claims whose citations didn't verify)
# ---------------------------------------------------------------------------

def build_citation_fix_prompt(page_title: str, file_paths: List[str],
                             content: str, broken: List[tuple],
                             repo_url: str) -> str:
    """Prompt the model to fix or remove ONLY the claims whose citations could
    not be found in the repository's actual source.

    ``broken`` is a list of ``(citation_label, reason)`` pairs, e.g.
    ``("ghost.py:9-9", "file not provided")``.
    """
    files_joined = ", ".join(file_paths)
    listed = "\n".join(f"- {label} ({reason})" for label, reason in broken)
    return (
        f"You are correcting a documentation page generated for the repository {repo_url}. "
        "You have access to the repository's actual source code through the provided context.\n"
        "\n"
        "The citations listed below could NOT be found in the repository's actual source — "
        "the file is missing or the cited lines do not exist:\n"
        f"{listed}\n"
        "\n"
        "For EACH listed citation, either correct the claim so it matches the real code (and cite "
        "the correct file and line numbers), or remove the claim entirely. Do NOT add any new claim "
        "that is not directly supported by the provided source. Keep the page's structure, level of "
        "detail, and language.\n"
        "\n"
        "Reply with the COMPLETE corrected page in markdown — no preamble, no explanation, no code "
        "fence around the whole page.\n"
        "\n"
        f"<page title=\"{page_title}\" files=\"{files_joined}\">\n"
        f"{content}\n"
        "</page>"
    )
```

Note: `List` is already imported at the top of `wiki_prompts.py` (used by `build_page_rag_query`). Confirm with `grep -n "from typing import" api/wiki_prompts.py` before running.

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_wiki_prompts.py::test_build_citation_fix_prompt_lists_every_broken_citation -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /home/ubuntu/deepwiki-open
git add api/wiki_prompts.py tests/unit/test_wiki_prompts.py
git commit -m "feat: build_citation_fix_prompt for targeted citation correction"
```

---

## Task 3: `ground_page_citations` helper + wiring

**Files:**
- Modify: `api/wiki_generator.py` (imports; new constant ~line 43; new dataclass + helpers ~after line 56; replace inline citation tail ~lines 451–468)
- Test: `tests/unit/test_wiki_generator.py` (append integration tests)

- [ ] **Step 1: Write the failing integration tests**

Append to `tests/unit/test_wiki_generator.py`:
```python
SINGLE_PAGE_XML = """<wiki_structure>
  <title>T</title><description>d</description>
  <sections><section id="s1"><title>S</title><page_ref>p1</page_ref></section></sections>
  <pages><page id="p1"><title>Page One</title><importance>high</importance>
    <relevant_files><file_path>a.py</file_path></relevant_files></page></pages>
</wiki_structure>"""


def test_citation_fix_corrects_broken_page(tmp_path, monkeypatch):
    # Non-empty repo_map so correction runs; a.py lines 1-3 exist, ghost.py absent.
    monkeypatch.setattr(wiki_generator, "read_repo_files",
                        lambda *a, **k: {"a.py": "alpha\nbeta\ngamma"})
    job = make_job(self_review=False)
    broken_page = "# Page\n\nClaim. Sources: [ghost.py:9-9]()"
    fixed_page = "# Page\n\nClaim. Sources: [a.py:1-2]()"
    dispatch = FakeDispatch([SINGLE_PAGE_XML, broken_page, fixed_page])

    run(run_generation(job, dispatch))

    content = read_cache(tmp_path, job)["generated_pages"]["p1"]["content"]
    assert "ghost.py" not in content
    assert "a.py:1-2" in content
    # structure + page + one fix dispatch
    assert len(dispatch.prompts) == 3


def test_unfixable_claims_are_stripped(tmp_path, monkeypatch):
    monkeypatch.setattr(wiki_generator, "read_repo_files",
                        lambda *a, **k: {"a.py": "alpha\nbeta\ngamma"})
    job = make_job(self_review=False)
    page = ("# Page\n\n"
            "Real claim. Sources: [a.py:1-2]()\n\n"
            "Fabricated claim. Sources: [ghost.py:9-9]()")
    # Model fails to fix on both attempts (returns the same broken page).
    dispatch = FakeDispatch([SINGLE_PAGE_XML, page, page, page])

    run(run_generation(job, dispatch))

    content = read_cache(tmp_path, job)["generated_pages"]["p1"]["content"]
    assert "Real claim" in content
    assert "Fabricated claim" not in content
    assert "ghost.py" not in content
    # structure + page + 2 fix attempts
    assert len(dispatch.prompts) == 4


def test_empty_repo_map_skips_correction_and_strip(tmp_path, monkeypatch):
    # Outage guard: nothing to verify against -> leave the page untouched.
    monkeypatch.setattr(wiki_generator, "read_repo_files", lambda *a, **k: {})
    job = make_job(self_review=False)
    page = "# Page\n\nClaim. Sources: [ghost.py:9-9]()"
    dispatch = FakeDispatch([SINGLE_PAGE_XML, page])

    run(run_generation(job, dispatch))

    data = read_cache(tmp_path, job)["generated_pages"]["p1"]
    assert data["content"] == page
    assert "ghost.py" in data["content"]
    assert len(dispatch.prompts) == 2  # no fix dispatch
    assert data["citations"]["ghost.py:9-9"]["status"] == "broken"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_wiki_generator.py -k "citation_fix or unfixable or empty_repo_map" -v`
Expected: FAIL — `test_citation_fix_corrects_broken_page` asserts 3 prompts but gets 2 (no correction loop exists yet); strip test still contains "Fabricated claim".

- [ ] **Step 3a: Add imports**

In `api/wiki_generator.py`, change the `wiki_prompts` import (currently lines 36–38) to include `build_citation_fix_prompt`:
```python
from api.wiki_prompts import (build_citation_fix_prompt, build_page_prompt,
                              build_page_rag_query, build_self_review_prompt,
                              build_structure_prompt, get_clone_default_branch,
                              parse_revised_content)
```
And add a new import after the `citation_grounding` import (line 33):
```python
from api.citation_stripping import strip_unverified_claims
```

- [ ] **Step 3b: Add the attempts constant**

After `MAX_CONSECUTIVE_PAGE_FAILURES = 3` (line 43), add:
```python
# How many targeted fix passes to attempt before stripping what's still broken.
MAX_CITATION_FIX_ATTEMPTS = int(os.environ.get("WIKI_CITATION_FIX_ATTEMPTS", "2"))
```

- [ ] **Step 3c: Add the dataclass and helpers**

After the `GenerationError` class (after line 56, before `@dataclass class PhaseStats`), add:
```python
@dataclass
class GroundingContext:
    """Everything needed to (re)verify one page's citations against the source
    the model was shown (fitted file + RAG chunks) plus the full repo files."""
    system_prompt: str
    page_inner: str
    file_content: str
    file_path: str
    page_context: str
    page_documents: list
    repo_map: dict
    provider: str
    repo_url: str
    page_title: str
    file_paths: list


def _verify_citations(content: str, ctx: "GroundingContext") -> Dict[str, dict]:
    """Verify citations against the post-budget-fit source + the repo files."""
    fitted_fc, fitted_ctx = fit_envelope_inputs(
        ctx.system_prompt, ctx.page_inner, file_content=ctx.file_content,
        context_text=ctx.page_context, provider=ctx.provider)
    rag_for_map = (ctx.page_documents
                   if fitted_ctx and fitted_ctx == ctx.page_context else [])
    source_map = build_source_map(fitted_fc, ctx.file_path, rag_for_map)
    return verify_page_citations(content, source_map, ctx.repo_map)


async def ground_page_citations(content: str, ctx: "GroundingContext",
                               dispatch, stats) -> tuple:
    """Verify citations, correct ungrounded claims, then strip any that remain.

    Returns ``(content, citations)``. When ``ctx.repo_map`` is empty we cannot
    trust brokenness — an embedder outage leaves nothing to verify against, so
    EVERY citation would resolve broken — and we return the page unchanged
    (today's behavior) rather than delete correct content.
    """
    citations = _verify_citations(content, ctx)
    if not ctx.repo_map:
        return content, citations

    for _ in range(MAX_CITATION_FIX_ATTEMPTS):
        broken = [(label, info.get("reason") or "")
                  for label, info in citations.items()
                  if info["status"] == "broken"]
        if not broken:
            return content, citations
        fix_inner = build_citation_fix_prompt(
            ctx.page_title, ctx.file_paths, content, broken, ctx.repo_url)
        fix_prompt = assemble_envelope(
            ctx.system_prompt, fix_inner, file_content=ctx.file_content,
            file_path=ctx.file_path, context_text=ctx.page_context,
            provider=ctx.provider)
        try:
            revised = await dispatch(fix_prompt, stats)
        except Exception as e:
            logger.warning(f"Citation fix dispatch failed: {e}")
            break
        revised = re.sub(r"^```markdown\s*", "", revised, flags=re.IGNORECASE)
        revised = re.sub(r"```\s*$", "", revised, flags=re.IGNORECASE)
        revised = revised.strip()
        if not revised or revised.startswith("Error"):
            break
        content = revised
        citations = _verify_citations(content, ctx)

    if any(info["status"] == "broken" for info in citations.values()):
        content = strip_unverified_claims(content, citations)
        citations = _verify_citations(content, ctx)
        logger.info("Stripped unverified claims after citation-fix attempts")
    return content, citations
```

- [ ] **Step 3d: Wire the helper into `run_generation`**

Replace the inline citation block (current lines 451–468, from the `# 7. Incremental save after every page` comment through the `generated[page["id"]] = ...` line) with:
```python
        # 7. Citation grounding: verify, correct ungrounded claims, strip the
        # rest. The outage guard inside ground_page_citations leaves the page
        # untouched when there are no repo files to verify against.
        if content.startswith(_ERROR_CONTENT_PREFIX):
            citations = {}
        else:
            ground_ctx = GroundingContext(
                system_prompt=system_prompt, page_inner=page_inner,
                file_content=file_content, file_path=file_path,
                page_context=page_context, page_documents=page_documents,
                repo_map=repo_map, provider=job.provider, repo_url=repo_url,
                page_title=page["title"], file_paths=page["filePaths"])
            content, citations = await ground_page_citations(
                content, ground_ctx, timed_dispatch, stats_review)
        generated[page["id"]] = {**page, "content": content, "citations": citations}
```

(Leave the following `progress.pages_done += 1` / `notify()` / `await save_partial(...)` lines unchanged.)

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_wiki_generator.py -k "citation_fix or unfixable or empty_repo_map" -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Run the full wiki_generator suite to confirm no regressions**

Run: `.venv/bin/python -m pytest tests/unit/test_wiki_generator.py -v`
Expected: PASS (all existing tests + 3 new)

- [ ] **Step 6: Commit**

```bash
cd /home/ubuntu/deepwiki-open
git add api/wiki_generator.py tests/unit/test_wiki_generator.py
git commit -m "feat: ground wiki pages — fix-loop then strip ungrounded claims"
```

---

## Task 4: Full suite, version bump, final commit

**Files:**
- Modify: `src/version.ts` (bump `APP_VERSION` — project convention before any image build; see project memory)

- [ ] **Step 1: Run the full unit suite**

Run: `.venv/bin/python -m pytest tests/unit -v`
Expected: PASS (no regressions across the suite)

- [ ] **Step 2: Bump the app version**

Read `src/version.ts`, find the `APP_VERSION` constant, and increment the patch version (e.g. `0.3.x` → `0.3.(x+1)`). Show the bumped line:

Run: `grep -n "APP_VERSION" src/version.ts`
Expected: the new version string is printed.

- [ ] **Step 3: Commit**

```bash
cd /home/ubuntu/deepwiki-open
git add src/version.ts
git commit -m "chore: bump APP_VERSION for citation-grounded regeneration"
```

---

## Self-Review (completed by plan author)

- **Spec coverage:** regenerate-via-correction-loop (Task 3 loop), targeted fix prompt (Task 2), strip fallback per-block all-broken (Task 1 + Task 3), max 2 attempts (`MAX_CITATION_FIX_ATTEMPTS`), outage guard on empty `repo_map` (Task 3 + outage test), `Sources:`-line/claim merge (Task 1 `_is_sources_only`), out-of-scope items untouched (no structure/mermaid/temperature changes). All covered.
- **Placeholder scan:** none — every code/test step is complete.
- **Type consistency:** `strip_unverified_claims(content, citations)`, `build_citation_fix_prompt(page_title, file_paths, content, broken, repo_url)`, `GroundingContext` fields, and `ground_page_citations(content, ctx, dispatch, stats)` are used identically wherever referenced. `citations` is the `{label: {status, reason, ...}}` map produced by `verify_page_citations` throughout.
- **Known assumption to verify during execution:** real `read_repo_files(None, set())` returns `{}` (empty `repo_map`) so existing tests keep their current dispatch counts — confirmed by existing passing tests that already call this path at the per-page tail.
