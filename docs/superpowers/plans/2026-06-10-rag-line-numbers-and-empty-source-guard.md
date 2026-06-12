# RAG Chunk Line Numbers + Empty-Source Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Two deferred follow-ups to the deep-dive citation fix, on branch `fix/deep-dive-citation-accuracy`:
- **A (RAG line numbers):** retrieved RAG chunks should carry real source line numbers so the model cites accurately from RAG context too (not just the primary deep-dive file).
- **B (empty-source guard):** a deep-dive page whose source file can't be loaded should be recorded as an error instead of generating a fabricated page from nothing.

**Architecture:**
- **A:** At index time, compute each chunk's `start_line`/`end_line` from its splitter `order` (deterministic: word-split with `step = chunk_size − chunk_overlap`) and store them in the chunk's metadata. At format time, render each chunk with per-line absolute numbers (reusing a generalized `number_source_lines`) and a `(lines a-b)` header. Both the generation path and the websocket chat path go through one shared `format_context_text`.
- **B:** After the deep-dive source fetch, if `file_content` is empty AND the repo is a remote type (github/gitlab/bitbucket), raise inside the existing per-page try so the page is recorded as `Error generating content: …` and counts toward the consecutive-failure threshold. Local repos (which never inject source via the provider API) proceed unchanged.

**Tech Stack:** Python 3 (FastAPI `api/`), adalflow (`TextSplitter`, `LocalDB`), pytest (`tests/unit/`). No network in tests.

**User decisions (locked):**
- RAG chunks: **per-line numbers + range header** (not header-range-only).
- Empty deep-dive source: hard-fail **remote repo types only**; local repos proceed as today.

**Prerequisite already done:** The indexing fix (`MAX_INDEX_FILE_TOKENS`) that puts COBOL `.txt` programs into the index is committed (`a864062`). Without it there are no program chunks to number.

**Key facts verified in the codebase:**
- Splitter config (`api/config/embedder.json`): `split_by="word"`, `chunk_size=350`, `chunk_overlap=100` → `step=250`. Chunk with `order=i` spans words `[i·250 : i·250+350]`; chunk text is an exact substring of the parent (word-split with `" "` separator preserves interior tabs/newlines and consecutive spaces).
- adalflow's splitter (`.venv/.../text_splitter.py:284`) shares **one** `meta_data` dict across all chunks of a parent (`meta_data = deepcopy(doc.meta_data)` then `meta_data=meta_data` for every chunk). **Therefore line spans MUST be written to a fresh per-chunk dict** (`chunk.meta_data = {**chunk.meta_data, ...}`), never mutated in place — otherwise every chunk of a file gets the last chunk's numbers.
- `LocalDB.get_transformed_data(key=...)` (`.venv/.../core/db.py:130`) returns the same chunk objects held in `transformed_items[key]`; mutating them before `save_state` persists the change.
- `format_context_text` exists in `api/prompt_assembly.py:73`; `api/websocket_wiki.py:222-246` is a byte-equivalent **inline duplicate**. There is no unit test over the websocket copy.
- `number_source_lines(content)` (from the prior fix) already numbers from 1; generalizing with a `start` param keeps existing callers unchanged.

---

## File Structure

- `api/prompt_assembly.py` — generalize `number_source_lines(content, start=1)`; teach `format_context_text` to render per-chunk numbered bodies + range headers when `start_line` is present, else the existing grouped plain format.
- `api/data_pipeline.py` — add `compute_line_span(...)` (pure) + `attach_chunk_line_spans(chunks, documents)` (config/glue); call the latter in `transform_documents_and_save_to_db` between `db.transform` and `db.save_state`.
- `api/websocket_wiki.py` — replace the inline RAG-grouping block with a call to the shared `format_context_text` (DRY; gives the chat path numbered chunks too).
- `api/wiki_generator.py` — empty-source guard in the deep-dive branch.
- `tests/unit/test_prompt_assembly.py` — tests for `number_source_lines(start=…)` and the numbered `format_context_text`.
- `tests/unit/test_chunk_line_spans.py` (new) — tests for `compute_line_span` + `attach_chunk_line_spans`.
- `tests/unit/test_wiki_generator.py` — replace `test_deep_dive_file_fetch_failure_proceeds`; add empty-string + local-repo cases.
- `src/version.ts` — bump to `0.3.6`.

**Canonical numbering format (unchanged):** `f"{n:>6} | {line}"`.

---

## Task 1: Generalize `number_source_lines` with a start offset

**Files:**
- Modify: `api/prompt_assembly.py` (the `number_source_lines` function added by the prior fix)
- Test: `tests/unit/test_prompt_assembly.py`

- [x] **Step 1: Write the failing tests**

Add to `tests/unit/test_prompt_assembly.py`:

```python
def test_number_source_lines_custom_start():
    out = number_source_lines("DELTA\nEPSILON", start=2)
    assert out == "     2 | DELTA\n     3 | EPSILON"


def test_number_source_lines_default_start_unchanged():
    # Existing callers pass no start; must still number from 1.
    assert number_source_lines("A\nB") == "     1 | A\n     2 | B"
```

- [x] **Step 2: Run to verify the new one fails**

Run: `cd /home/ubuntu/deepwiki-open && .venv/bin/python -m pytest tests/unit/test_prompt_assembly.py -k number_source_lines_custom_start -v`
Expected: FAIL — `number_source_lines() got an unexpected keyword argument 'start'`.

- [x] **Step 3: Add the `start` parameter**

In `api/prompt_assembly.py`, change the signature and the `enumerate` start:

```python
def number_source_lines(content: str, start: int = 1) -> str:
    """Prefix each source line with its 1-based line number.

    ``start`` lets callers number a fragment by its absolute position in the
    original file (e.g. a RAG chunk that begins at line 120). Defaults to 1 so
    existing callers that pass whole files are unaffected.

    Deep-dive pages and RAG context both order the model to cite exact line
    numbers, but raw source/chunks carry no line markers — numbering gives it
    ground truth to cite.
    """
    if not content:
        return ""
    return "\n".join(
        f"{n:>6} | {line}"
        for n, line in enumerate(content.splitlines(), start=start)
    )
```

- [x] **Step 4: Run tests to verify pass**

Run: `cd /home/ubuntu/deepwiki-open && .venv/bin/python -m pytest tests/unit/test_prompt_assembly.py -v`
Expected: PASS (all prior `number_source_lines` tests + the 2 new ones).

- [x] **Step 5: Commit**

```bash
cd /home/ubuntu/deepwiki-open
git add api/prompt_assembly.py tests/unit/test_prompt_assembly.py
git commit -m "feat: number_source_lines accepts a start offset for RAG chunks"
```

---

## Task 2: Compute and attach chunk line spans at index time

**Files:**
- Modify: `api/data_pipeline.py` (add two functions; call one in `transform_documents_and_save_to_db` ~line 483)
- Test: `tests/unit/test_chunk_line_spans.py` (new — do NOT touch `test_data_pipeline_indexing.py`)

Note: `configs` is a module global in `data_pipeline.py` (used at line 441 as `configs["text_splitter"]`). The splitter Documents expose `.parent_doc_id` (str), `.order` (int), `.text`, `.meta_data`. Parent `documents` expose `.id` and `.text`.

- [x] **Step 1: Write the failing tests**

Create `tests/unit/test_chunk_line_spans.py`:

```python
"""Tests for chunk line-span computation (RAG citation accuracy).

compute_line_span derives a chunk's absolute start/end line from its splitter
`order`, using the same word-window stepping adalflow's TextSplitter uses.
"""
from types import SimpleNamespace

from api.data_pipeline import compute_line_span, attach_chunk_line_spans


# A 4-word parent spanning 3 lines. Word split on " ":
#   words = ["alpha", "beta\ngamma", "delta\nepsilon", "zeta"]
#   char offsets: alpha@0, beta\ngamma@6, delta\nepsilon@17, zeta@31
PARENT = "alpha beta\ngamma delta\nepsilon zeta"


def test_compute_line_span_first_chunk():
    # order 0 always starts at line 1; chunk text spans 2 lines.
    assert compute_line_span(PARENT, order=0, chunk_text="alpha beta\ngamma ", step=2) == (1, 2)


def test_compute_line_span_second_chunk():
    # order 1, step 2 -> starts at word index 2 ("delta\nepsilon", char 17, line 2).
    assert compute_line_span(PARENT, order=1, chunk_text="delta\nepsilon zeta", step=2) == (2, 3)


def test_compute_line_span_order_past_end_returns_none():
    assert compute_line_span(PARENT, order=99, chunk_text="x", step=2) is None


def test_attach_chunk_line_spans_writes_fresh_per_chunk_dict():
    # Two chunks of one parent. adalflow shares one meta_data dict across a
    # parent's chunks; attach must give each chunk its OWN dict so they don't
    # clobber each other.
    shared_meta = {"file_path": "p.cbl"}
    parent = SimpleNamespace(id="P1", text=PARENT)
    c0 = SimpleNamespace(parent_doc_id="P1", order=0,
                         text="alpha beta\ngamma ", meta_data=shared_meta)
    c1 = SimpleNamespace(parent_doc_id="P1", order=1,
                         text="delta\nepsilon zeta", meta_data=shared_meta)

    attach_chunk_line_spans([c0, c1], [parent], step=2)

    assert c0.meta_data["start_line"] == 1 and c0.meta_data["end_line"] == 2
    assert c1.meta_data["start_line"] == 2 and c1.meta_data["end_line"] == 3
    # distinct dicts — c1 did not overwrite c0
    assert c0.meta_data is not c1.meta_data
    assert c0.meta_data["file_path"] == "p.cbl"  # original key preserved


def test_attach_chunk_line_spans_skips_unknown_parent():
    c = SimpleNamespace(parent_doc_id="MISSING", order=0, text="x",
                        meta_data={"file_path": "p"})
    attach_chunk_line_spans([c], [], step=2)
    assert "start_line" not in c.meta_data
```

- [x] **Step 2: Run to verify they fail**

Run: `cd /home/ubuntu/deepwiki-open && .venv/bin/python -m pytest tests/unit/test_chunk_line_spans.py -v`
Expected: FAIL — `ImportError: cannot import name 'compute_line_span'`.

- [x] **Step 3: Implement the two functions**

In `api/data_pipeline.py`, add near `transform_documents_and_save_to_db` (e.g. just above it, after `prepare_data_pipeline`):

```python
def compute_line_span(parent_text: str, order: int, chunk_text: str, step: int):
    """Absolute (start_line, end_line) of a word-split chunk within its parent.

    adalflow's word splitter windows the parent's space-separated words with a
    stride of ``step = chunk_size - chunk_overlap``; chunk ``order`` therefore
    begins at word index ``order * step``. The chunk text is an exact substring
    of the parent, so the start char (and thus start line) is derivable without
    fragile substring matching. Returns ``None`` if ``order`` runs past the end.
    """
    words = parent_text.split(" ")
    start_word = order * step
    if start_word >= len(words):
        return None
    start_char = 0 if start_word == 0 else len(" ".join(words[:start_word])) + 1
    start_line = parent_text[:start_char].count("\n") + 1
    end_line = start_line + chunk_text.count("\n")
    return start_line, end_line


def attach_chunk_line_spans(chunks, documents, step: int = None):
    """Write start_line/end_line into each chunk's metadata (fresh dict).

    Only valid for word splitting; a no-op for other split_by modes. ``step``
    defaults to the configured ``chunk_size - chunk_overlap``. A FRESH meta_data
    dict is assigned per chunk because adalflow shares one dict across all of a
    parent's chunks — in-place mutation would make them clobber each other.
    """
    cfg = configs["text_splitter"]
    if cfg.get("split_by") != "word":
        return chunks
    if step is None:
        step = cfg["chunk_size"] - cfg["chunk_overlap"]
    parent_text_by_id = {str(d.id): d.text for d in documents}
    for chunk in chunks:
        parent_text = parent_text_by_id.get(chunk.parent_doc_id)
        if parent_text is None:
            continue
        span = compute_line_span(parent_text, chunk.order or 0, chunk.text, step)
        if span is None:
            continue
        start_line, end_line = span
        chunk.meta_data = {**chunk.meta_data,
                           "start_line": start_line, "end_line": end_line}
    return chunks
```

Then wire it into `transform_documents_and_save_to_db` — between `db.transform(...)` and `db.save_state(...)` (currently lines 483-485):

```python
    db.transform(key="split_and_embed")
    # Tag each chunk with its source line span so retrieved RAG context can be
    # cited with real line numbers (see format_context_text).
    attach_chunk_line_spans(db.get_transformed_data(key="split_and_embed"), documents)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    db.save_state(filepath=db_path)
```

- [x] **Step 4: Run tests to verify pass**

Run: `cd /home/ubuntu/deepwiki-open && .venv/bin/python -m pytest tests/unit/test_chunk_line_spans.py tests/unit/test_data_pipeline_indexing.py -v`
Expected: PASS (new span tests + the pre-existing indexing tests still green).

- [x] **Step 5: Commit**

```bash
cd /home/ubuntu/deepwiki-open
git add api/data_pipeline.py tests/unit/test_chunk_line_spans.py
git commit -m "feat: tag RAG chunks with source line spans at index time"
```

---

## Task 3: Render numbered RAG chunks in `format_context_text`

**Files:**
- Modify: `api/prompt_assembly.py` (`format_context_text`)
- Test: `tests/unit/test_prompt_assembly.py`

The existing spanless behavior (group by file, join chunk texts under one `## File Path:` header) MUST be preserved exactly — the existing `test_format_context_text_groups_by_file_path` asserts it and uses docs without `start_line`. Only when a file's chunks all carry `start_line` do we switch to per-chunk numbered rendering.

- [x] **Step 1: Write the failing tests**

The existing `FakeDoc` in this test file is `class FakeDoc: def __init__(self, file_path, text): self.meta_data = {"file_path": file_path}; self.text = text`. Add a spanned variant and tests:

```python
class SpannedDoc:
    def __init__(self, file_path, text, start_line, end_line):
        self.meta_data = {"file_path": file_path,
                          "start_line": start_line, "end_line": end_line}
        self.text = text


def test_format_context_text_numbers_spanned_chunks():
    docs = [SpannedDoc("PAY.cbl", "READ-MASTER.\n    READ FILE", 120, 121)]
    out = format_context_text([FakeRetrieverOutput(docs)])
    expected = (
        "\n\n" + "-" * 10 +
        "## File Path: PAY.cbl (lines 120-121)\n\n"
        "   120 | READ-MASTER.\n"
        "   121 |     READ FILE"
    )
    assert out == expected


def test_format_context_text_multiple_spanned_chunks_same_file():
    docs = [SpannedDoc("PAY.cbl", "AAA", 10, 10),
            SpannedDoc("PAY.cbl", "BBB", 50, 50)]
    out = format_context_text([FakeRetrieverOutput(docs)])
    # Each chunk gets its own ranged header (gaps are visible via line numbers).
    assert "## File Path: PAY.cbl (lines 10-10)\n\n    10 | AAA" in out
    assert "## File Path: PAY.cbl (lines 50-50)\n\n    50 | BBB" in out


def test_format_context_text_spanless_unchanged():
    # Regression guard: docs without start_line keep the grouped plain format.
    docs = [FakeDoc("a.py", "first chunk"), FakeDoc("a.py", "second chunk")]
    out = format_context_text([FakeRetrieverOutput(docs)])
    assert out == ("\n\n" + "-" * 10 +
                   "## File Path: a.py\n\nfirst chunk\n\nsecond chunk")
```

(Note: `    10 | AAA` is `{10:>6}` → 4 spaces + "10", i.e. `"    10 | AAA"`.)

- [x] **Step 2: Run to verify they fail**

Run: `cd /home/ubuntu/deepwiki-open && .venv/bin/python -m pytest tests/unit/test_prompt_assembly.py -k "spanned or spanless" -v`
Expected: FAIL on the spanned tests (numbering not implemented).

- [x] **Step 3: Implement**

In `api/prompt_assembly.py`, replace the body of `format_context_text` (the grouping/formatting after the early-return guard) with:

```python
    documents = retrieved_documents[0].documents
    logger.info(f"Retrieved {len(documents)} documents")

    # Group documents by file path (preserves grouped order)
    docs_by_file = {}
    for doc in documents:
        file_path = doc.meta_data.get('file_path', 'unknown')
        docs_by_file.setdefault(file_path, []).append(doc)

    context_parts = []
    for file_path, docs in docs_by_file.items():
        if all(d.meta_data.get('start_line') for d in docs):
            # New index: render each chunk with absolute line numbers + range.
            for doc in docs:
                s = doc.meta_data['start_line']
                e = doc.meta_data.get('end_line', s)
                body = number_source_lines(doc.text, start=s)
                context_parts.append(f"## File Path: {file_path} (lines {s}-{e})\n\n{body}")
        else:
            # Old index / no line info: original grouped, plain format.
            content = "\n\n".join(doc.text for doc in docs)
            context_parts.append(f"## File Path: {file_path}\n\n{content}")

    return "\n\n" + "-" * 10 + "\n\n".join(context_parts)
```

Keep the existing early-return guard (`if not (retrieved_documents and retrieved_documents[0].documents): return ""`) unchanged above this.

- [x] **Step 4: Run tests to verify pass**

Run: `cd /home/ubuntu/deepwiki-open && .venv/bin/python -m pytest tests/unit/test_prompt_assembly.py -v`
Expected: PASS, including the unchanged `test_format_context_text_groups_by_file_path` and `test_format_context_text_empty_inputs`.

- [x] **Step 5: Commit**

```bash
cd /home/ubuntu/deepwiki-open
git add api/prompt_assembly.py tests/unit/test_prompt_assembly.py
git commit -m "feat: number retrieved RAG chunks with real source line numbers"
```

---

## Task 4: Route the websocket chat path through the shared formatter

**Files:**
- Modify: `api/websocket_wiki.py` (imports + the inline RAG-format block, ~lines 222-248)

This removes the duplicate grouping logic so the chat path also gets numbered chunks, and keeps a single source of truth. Output for spanless docs is byte-identical to today.

- [x] **Step 1: Confirm the shared function import**

Read the top imports of `api/websocket_wiki.py`. If `format_context_text` is not already imported from `api.prompt_assembly`, add:

```python
from api.prompt_assembly import format_context_text
```

(Verify no circular import: `api.prompt_assembly` imports only from `api.prompt_fit` and stdlib — safe.)

- [x] **Step 2: Replace the inline block**

Replace the inline grouping/formatting (currently lines 222-248, from `if retrieved_documents and retrieved_documents[0].documents:` through the `else: logger.warning("No documents retrieved from RAG")`) with:

```python
                    if retrieved_documents and retrieved_documents[0].documents:
                        context_text = format_context_text(retrieved_documents)
                    else:
                        logger.warning("No documents retrieved from RAG")
```

Leave the surrounding `try/except` (retrieval call, error logging) intact.

- [x] **Step 3: Verify nothing else references the removed locals**

Grep the function for `docs_by_file`, `context_parts`, and the local `documents` to ensure no later code depends on them:
Run: `cd /home/ubuntu/deepwiki-open && grep -n "docs_by_file\|context_parts" api/websocket_wiki.py`
Expected: no matches remain.

- [x] **Step 4: Sanity-check import + byte-parity behavior**

Run: `cd /home/ubuntu/deepwiki-open && .venv/bin/python -c "import api.websocket_wiki"` (must import cleanly).
Then run the prompt-assembly suite (covers the shared formatter): `cd /home/ubuntu/deepwiki-open && .venv/bin/python -m pytest tests/unit/test_prompt_assembly.py -q` → PASS.

- [x] **Step 5: Commit**

```bash
cd /home/ubuntu/deepwiki-open
git add api/websocket_wiki.py
git commit -m "refactor: websocket chat uses shared format_context_text (numbered chunks)"
```

---

## Task 5: Empty-source guard for deep-dive pages (remote repos only)

**Files:**
- Modify: `api/wiki_generator.py` (deep-dive branch, ~lines 356-369)
- Test: `tests/unit/test_wiki_generator.py`

Current behavior: on fetch failure `file_content`/`file_path` are zeroed and the page is generated anyway. New behavior: if the deep-dive source is empty and the repo is a remote type, raise inside the per-page `try` so the existing handler records `Error generating content: …` and increments `consecutive_failures`. Local repos proceed unchanged.

- [x] **Step 1: Replace/rewrite the affected tests (these are the failing tests)**

In `tests/unit/test_wiki_generator.py`, REPLACE the existing `test_deep_dive_file_fetch_failure_proceeds` with the three tests below. (`make_job` defaults to a github repo with token; `RepoInfo` is imported at the top of the file; `read_cache(tmp_path, job)` and the `engine_env` autouse fixture already exist.)

```python
def test_deep_dive_remote_fetch_failure_fails_page(tmp_path, monkeypatch):
    xml = """<wiki_structure><title>T</title><description>D</description><pages>
      <page id="page-analysis-prog"><title>Prog Analysis</title>
        <relevant_files><file_path>prog.cbl</file_path></relevant_files></page>
    </pages></wiki_structure>"""
    def boom(*a, **k):
        raise ValueError("fetch failed")
    monkeypatch.setattr(wiki_generator, "get_file_content", boom)
    job = make_job(self_review=False)  # github
    dispatch = FakeDispatch([xml])     # page must NOT reach the model
    run(run_generation(job, dispatch))

    content = read_cache(tmp_path, job)["generated_pages"]["page-analysis-prog"]["content"]
    assert content.startswith("Error generating content:")
    assert "could not be loaded" in content
    assert len(dispatch.prompts) == 1  # only the structure prompt was dispatched
    assert job.progress.phase == "done"


def test_deep_dive_remote_empty_string_fails_page(tmp_path, monkeypatch):
    xml = """<wiki_structure><title>T</title><description>D</description><pages>
      <page id="page-analysis-prog"><title>Prog Analysis</title>
        <relevant_files><file_path>prog.cbl</file_path></relevant_files></page>
    </pages></wiki_structure>"""
    monkeypatch.setattr(wiki_generator, "get_file_content", lambda *a, **k: "")
    job = make_job(self_review=False)  # github
    dispatch = FakeDispatch([xml])
    run(run_generation(job, dispatch))

    content = read_cache(tmp_path, job)["generated_pages"]["page-analysis-prog"]["content"]
    assert content.startswith("Error generating content:")
    assert len(dispatch.prompts) == 1


def test_deep_dive_local_repo_proceeds_without_source(tmp_path, monkeypatch):
    xml = """<wiki_structure><title>T</title><description>D</description><pages>
      <page id="page-analysis-prog"><title>Prog Analysis</title>
        <relevant_files><file_path>prog.cbl</file_path></relevant_files></page>
    </pages></wiki_structure>"""
    def boom(*a, **k):
        raise ValueError("local repos unsupported")
    monkeypatch.setattr(wiki_generator, "get_file_content", boom)
    local_repo = RepoInfo(owner="o", repo="r", type="local",
                          localPath="/tmp/x", repoUrl="https://example/o/r")
    job = make_job(repo=local_repo, self_review=False)
    dispatch = FakeDispatch([xml, PAGE_BODY])
    run(run_generation(job, dispatch))

    # Local deep-dive proceeds with no injected source (unchanged behavior).
    assert "<currentFileContent" not in dispatch.prompts[1]
    assert read_cache(tmp_path, job)["generated_pages"]["page-analysis-prog"]["content"] == PAGE_BODY
    assert job.progress.phase == "done"
```

If `get_repo_url`/RAG handling for a `local` RepoInfo causes an unexpected failure in `test_deep_dive_local_repo_proceeds_without_source`, STOP and report it (the local path may need a different RepoInfo shape) rather than weakening the assertion.

- [x] **Step 2: Run to verify they fail**

Run: `cd /home/ubuntu/deepwiki-open && .venv/bin/python -m pytest tests/unit/test_wiki_generator.py -k "fails_page or local_repo_proceeds" -v`
Expected: FAIL (guard not implemented — currently the page would generate, or the dispatch index/assertions won't match).

- [x] **Step 3: Implement the guard**

In `api/wiki_generator.py`, the deep-dive fetch block currently ends (after the prior fix) like:

```python
                except Exception as e:
                    logger.error(f"Error retrieving file content: {str(e)}")
                    file_content, file_path = "", ""
```

Immediately AFTER that `except` (still inside the `if is_deep_dive and page["filePaths"]:` block, and inside the outer per-page `try`), add:

```python
                # A deep-dive page is the definitive analysis of ONE program;
                # without its source the model fabricates line numbers and
                # filenames. Fail the page rather than generate from nothing —
                # but only for remote repo types where the source was fetchable
                # (local repos never inject and proceed as before).
                if not file_content and repo.type in ("github", "gitlab", "bitbucket"):
                    raise RuntimeError(
                        f"deep-dive source {requested_file_path} could not be loaded "
                        "(empty content); refusing to generate an ungrounded page")
```

This `RuntimeError` is caught by the existing outer `except Exception as e:` which sets `content = f"Error generating content: {e}"`, increments `consecutive_failures`, and raises `GenerationError` only after `MAX_CONSECUTIVE_PAGE_FAILURES`. No new handler needed.

- [x] **Step 4: Run tests to verify pass**

Run: `cd /home/ubuntu/deepwiki-open && .venv/bin/python -m pytest tests/unit/test_wiki_generator.py -v`
Expected: PASS — the three new tests, and the existing `test_deep_dive_file_injection` (github with a real two-line source still injects and generates) unaffected.

- [x] **Step 5: Commit**

```bash
cd /home/ubuntu/deepwiki-open
git add api/wiki_generator.py tests/unit/test_wiki_generator.py
git commit -m "fix: deep-dive page fails instead of fabricating when remote source is empty"
```

---

## Task 6: Full sweep + version bump

**Files:**
- Modify: `src/version.ts`

- [x] **Step 1: Run the changed-area suite**

Run: `cd /home/ubuntu/deepwiki-open && .venv/bin/python -m pytest tests/unit/test_prompt_assembly.py tests/unit/test_chunk_line_spans.py tests/unit/test_wiki_generator.py tests/unit/test_wiki_prompts.py tests/unit/test_data_pipeline_indexing.py -q`
Expected: PASS. (The 3 pre-existing `test_all_embedders.py` failures are environment-specific and unrelated — do not block on them.)

- [x] **Step 2: Bump the version**

In `src/version.ts`, change `export const APP_VERSION = '0.3.5';` to:

```ts
export const APP_VERSION = '0.3.6';
```

- [x] **Step 3: Commit**

```bash
cd /home/ubuntu/deepwiki-open
git add src/version.ts
git commit -m "chore: bump APP_VERSION to 0.3.6 (RAG line numbers + empty-source guard)"
```

---

## Out of scope / deploy notes

- **Re-index required for A:** `start_line`/`end_line` only land on indexes built after this change. Existing `.pkl` indexes lack them and fall back to the old plain (unnumbered) RAG format until the repo is re-indexed. Force a re-index of affected repos after deploy.
- The `embedder.json` `top_k` change, `test_prompt_fit.py`, and `docker-compose.yml` working-tree edits are unrelated WIP and are intentionally left uncommitted.

## Self-Review notes (done)
- Spec coverage: A = Tasks 1–4; B = Task 5. Re-index caveat documented.
- Type consistency: `number_source_lines(content, start=1)` signature used consistently in Tasks 1 and 3; `compute_line_span(parent_text, order, chunk_text, step)` and `attach_chunk_line_spans(chunks, documents, step=None)` consistent between Task 2 impl and tests.
- Shared-`meta_data` hazard explicitly handled (fresh per-chunk dict) and tested.
- Existing `format_context_text` grouped behavior preserved + regression-tested.
