# Citation Grounding & Inline Source Text Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Verify every wiki-page source citation against the exact source the model was given, then render verified citations as inline expandable source text (no GitLab link) and broken citations as a red "unverified" marker.

**Architecture:** A new pure backend module (`api/citation_grounding.py`) builds a per-page "source map" from the line-numbered deep-dive `file_content` and the retrieved RAG chunks, resolves each `[file:lines]()` citation against it, and stores the verdicts as a sidecar `citations` map on the page object. The markdown `content` is left unchanged. `Markdown.tsx` consumes the map: verified→expandable snippet, broken→red marker, absent→legacy blob-link fallback (backward compatible).

**Tech Stack:** Python 3 (FastAPI `api/`), pytest (`tests/unit/`). React/Next.js (`src/`), vitest (run via the project's `node:20-slim` container per the `deepwiki-frontend-js-toolchain` memory).

**Spec:** `docs/superpowers/specs/2026-06-12-citation-grounding-and-inline-source-design.md`

---

## File Structure

- `api/citation_grounding.py` — **new**, pure functions: `parse_citation_label`, `build_source_map`, `resolve_citation`, `verify_page_citations`, plus a small `FileSource` dataclass. No I/O, no network.
- `api/api.py` — add `CitationInfo` model; add `citations` field to `WikiPage`.
- `api/wiki_generator.py` — make `retrieve_for_generation` also return the retrieved documents; build the source map + verify citations at the per-page store point.
- `src/types/wiki/wikipage.tsx` — add `CitationInfo` type + `citations` field to `WikiPage`.
- `src/app/[owner]/[repo]/page.tsx` — add `citations` to the local `WikiPage` interface; pass `citations` prop to `<Markdown>`.
- `src/components/Markdown.tsx` — accept `citations` prop; render verified/broken/legacy citation states; add `CitationSnippet` + `BrokenCitation` inline components.
- `tests/unit/test_citation_grounding.py` — **new**, unit tests for the pure module.
- `tests/unit/test_wiki_generator.py` — add a test that `citations` is populated on stored pages.
- `src/components/Markdown.citation.test.tsx` — add render-state tests; thread `citations` through the test helper.
- `src/version.ts` — bump `APP_VERSION`.

**Canonical citation-label grammar (mirror of `src/utils/citationUrl.ts` `CITATION_RE`):**
`^([^:]+\.[A-Za-z0-9]+)(?::(\d+)(?:-(\d+))?)?$` — a path with a file extension, optionally `:line` or `:start-end`.

**Numbered-source prefix (from `number_source_lines`):** `f"{n:>6} | {line}"` → 6-wide right-justified number, space, pipe, space, code. The source map strips this back to raw `line`.

---

## Task 1: Citation label parsing + source map (`api/citation_grounding.py`)

**Files:**
- Create: `api/citation_grounding.py`
- Test: `tests/unit/test_citation_grounding.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_citation_grounding.py`:

```python
"""Tests for citation grounding (verify citations against provided source)."""
from types import SimpleNamespace

from api.citation_grounding import (FileSource, build_source_map,
                                    parse_citation_label)
from api.prompt_assembly import number_source_lines


def test_parse_citation_label_range():
    assert parse_citation_label("prog.cbl:12-34") == ("prog.cbl", 12, 34)


def test_parse_citation_label_single_line():
    assert parse_citation_label("prog.cbl:12") == ("prog.cbl", 12, None)


def test_parse_citation_label_whole_file():
    assert parse_citation_label("prog.cbl") == ("prog.cbl", None, None)


def test_parse_citation_label_rejects_non_citation():
    # No file extension -> not a citation (matches frontend CITATION_RE).
    assert parse_citation_label("see the docs") is None


def test_build_source_map_from_numbered_deep_dive():
    # Deep-dive injects line-numbered content; the map stores RAW text by line.
    numbered = number_source_lines("ALPHA\nBETA\nGAMMA")
    smap = build_source_map(numbered, "prog.cbl", [])
    fs = smap["prog.cbl"]
    assert fs.lines == {1: "ALPHA", 2: "BETA", 3: "GAMMA"}


def test_build_source_map_from_rag_chunk_with_span():
    doc = SimpleNamespace(
        text="READ-MASTER.\n    READ FILE",
        meta_data={"file_path": "PAY.cbl", "start_line": 120, "end_line": 121})
    smap = build_source_map("", "", [doc])
    assert smap["PAY.cbl"].lines == {120: "READ-MASTER.", 121: "    READ FILE"}


def test_build_source_map_rag_chunk_without_span_is_whole_file_only():
    # Old indexes carry no start_line: file is present but has no line text.
    doc = SimpleNamespace(text="whatever", meta_data={"file_path": "a.py"})
    smap = build_source_map("", "", [doc])
    assert "a.py" in smap
    assert smap["a.py"].lines == {}


def test_build_source_map_ignores_docs_without_file_path():
    doc = SimpleNamespace(text="x", meta_data={})
    smap = build_source_map("", "", [doc])
    assert smap == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/ubuntu/deepwiki-open && .venv/bin/python -m pytest tests/unit/test_citation_grounding.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'api.citation_grounding'`.

- [ ] **Step 3: Implement parsing + source map**

Create `api/citation_grounding.py`:

```python
"""Verify wiki-page source citations against the source the model was given.

A page's claims cite `[file.ext:start-end]()`. At generation time we hold the
exact source we showed the model — the line-numbered deep-dive file and the
retrieved RAG chunks — so we can check each citation mechanically: does the file
exist in what we provided, and do the cited lines fall within it? Verified
citations become inline source text in the UI; broken ones are flagged as
possibly fabricated. Pure module: no I/O, no network.
"""
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# Mirror of src/utils/citationUrl.ts CITATION_RE: a path with a file extension,
# optional :line or :start-end. Requires the extension so prose can't match.
_CITATION_RE = re.compile(r"^([^:]+\.[A-Za-z0-9]+)(?::(\d+)(?:-(\d+))?)?$")

# The prefix number_source_lines adds: "{n:>6} | {code}".
_NUMBERED_RE = re.compile(r"^ *(\d+) \| (.*)$")


@dataclass
class FileSource:
    """Source we provided for one file: real line number -> raw line text.

    ``lines`` is empty when the file was present in context but carried no line
    information (e.g. an old RAG chunk without a span) — the file is then known
    only at whole-file granularity.
    """
    lines: Dict[int, str] = field(default_factory=dict)


def parse_citation_label(label: str) -> Optional[Tuple[str, Optional[int], Optional[int]]]:
    """(file_path, start_line, end_line) for a citation label, or None.

    None means the label is not a citation (no file extension) and should be
    left alone.
    """
    m = _CITATION_RE.match(label.strip())
    if not m:
        return None
    path, start, end = m.group(1), m.group(2), m.group(3)
    return path, (int(start) if start else None), (int(end) if end else None)


def _ingest_numbered(file_path: str, numbered: str, smap: Dict[str, FileSource]) -> None:
    fs = smap.setdefault(file_path, FileSource())
    for line in numbered.splitlines():
        m = _NUMBERED_RE.match(line)
        if m:
            fs.lines[int(m.group(1))] = m.group(2)


def _ingest_chunk(doc, smap: Dict[str, FileSource]) -> None:
    meta = getattr(doc, "meta_data", None) or {}
    file_path = meta.get("file_path")
    if not file_path:
        return
    fs = smap.setdefault(file_path, FileSource())
    start = meta.get("start_line")
    if start is None:
        return  # whole-file presence only
    for offset, text in enumerate(doc.text.splitlines()):
        fs.lines[start + offset] = text


def build_source_map(file_content: str, file_path: str, rag_documents) -> Dict[str, FileSource]:
    """Map file_path -> FileSource of the source we GAVE the model for one page.

    ``file_content`` is the line-numbered deep-dive program source (empty for
    standard pages); ``file_path`` is its path. ``rag_documents`` are the
    retrieved chunk documents (each with ``.text`` and ``.meta_data``).
    """
    smap: Dict[str, FileSource] = {}
    if file_content and file_path:
        _ingest_numbered(file_path, file_content, smap)
    for doc in (rag_documents or []):
        _ingest_chunk(doc, smap)
    return smap
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/ubuntu/deepwiki-open && .venv/bin/python -m pytest tests/unit/test_citation_grounding.py -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
cd /home/ubuntu/deepwiki-open
git add api/citation_grounding.py tests/unit/test_citation_grounding.py
git commit -m "feat: citation label parsing + source map for grounding"
```

---

## Task 2: Resolve + verify citations (`api/citation_grounding.py`)

**Files:**
- Modify: `api/citation_grounding.py` (add `resolve_citation`, `verify_page_citations`)
- Test: `tests/unit/test_citation_grounding.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_citation_grounding.py` (extend the import line at the top to:
`from api.citation_grounding import (FileSource, build_source_map, parse_citation_label, resolve_citation, verify_page_citations)`):

```python
def _smap():
    return {"prog.cbl": FileSource(lines={12: "MOVE A TO B", 13: "ADD 1 TO C"})}


def test_resolve_verified_range_returns_snippet():
    info = resolve_citation("prog.cbl:12-13", _smap())
    assert info["status"] == "verified"
    assert info["snippet"] == "MOVE A TO B\nADD 1 TO C"
    assert info["filePath"] == "prog.cbl"
    assert info["startLine"] == 12 and info["endLine"] == 13


def test_resolve_verified_single_line():
    info = resolve_citation("prog.cbl:12", _smap())
    assert info["status"] == "verified"
    assert info["snippet"] == "MOVE A TO B"


def test_resolve_broken_file_not_provided():
    info = resolve_citation("ghost.cbl:1-3", _smap())
    assert info["status"] == "broken"
    assert info["reason"] == "file not provided"
    assert info["snippet"] is None


def test_resolve_broken_lines_out_of_range():
    info = resolve_citation("prog.cbl:12-99", _smap())
    assert info["status"] == "broken"
    assert info["reason"] == "lines not in provided source"


def test_resolve_whole_file_present_is_verified_without_snippet():
    info = resolve_citation("prog.cbl", _smap())
    assert info["status"] == "verified"
    assert info["snippet"] is None


def test_resolve_whole_file_absent_is_broken():
    info = resolve_citation("ghost.cbl", _smap())
    assert info["status"] == "broken"
    assert info["reason"] == "file not provided"


def test_resolve_line_range_when_no_line_info_is_broken():
    # File present but no line text (old RAG chunk) -> ranged cite can't verify.
    info = resolve_citation("a.py:5", {"a.py": FileSource(lines={})})
    assert info["status"] == "broken"
    assert info["reason"] == "lines not in provided source"


def test_resolve_non_citation_returns_none():
    assert resolve_citation("just prose", _smap()) is None


def test_verify_page_citations_extracts_empty_href_links_only():
    content = (
        "Intro. Sources: [prog.cbl:12-13]()\n\n"
        "More. Sources: [ghost.cbl:1-2]()\n\n"
        "A real link [docs](https://example.com/x) and prose [not a cite]()."
    )
    out = verify_page_citations(content, _smap())
    assert out["prog.cbl:12-13"]["status"] == "verified"
    assert out["ghost.cbl:1-2"]["status"] == "broken"
    # Real-href link and the non-citation empty link are not included.
    assert "docs" not in out
    assert "not a cite" not in out


def test_verify_page_citations_dedupes_repeated_label():
    content = "Sources: [prog.cbl:12](). Again Sources: [prog.cbl:12]()."
    out = verify_page_citations(content, _smap())
    assert list(out.keys()) == ["prog.cbl:12"]


def test_verify_page_citations_round_trip_with_numbered_source():
    # number_source_lines -> build_source_map -> verify recovers the real lines.
    numbered = number_source_lines("FIRST LINE\nSECOND LINE\nTHIRD LINE")
    smap = build_source_map(numbered, "x.cbl", [])
    out = verify_page_citations("Sources: [x.cbl:1-2]()", smap)
    assert out["x.cbl:1-2"]["snippet"] == "FIRST LINE\nSECOND LINE"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/ubuntu/deepwiki-open && .venv/bin/python -m pytest tests/unit/test_citation_grounding.py -k "resolve or verify" -v`
Expected: FAIL — `ImportError: cannot import name 'resolve_citation'`.

- [ ] **Step 3: Implement resolve + verify**

Append to `api/citation_grounding.py`:

```python
# Markdown citations are empty-href links: [label](). Real links have an href
# and are skipped. Mirrors Markdown.tsx, which only treats empty-href links as
# citation candidates.
_EMPTY_LINK_RE = re.compile(r"\[([^\]]+)\]\(\)")


def resolve_citation(label: str, source_map: Dict[str, FileSource]) -> Optional[dict]:
    """Resolve one citation label against the provided source.

    Returns a dict {status, filePath, startLine, endLine, snippet, reason}, or
    None if ``label`` is not a citation at all.
    """
    parsed = parse_citation_label(label)
    if parsed is None:
        return None
    file_path, start, end = parsed
    info = {"status": "broken", "filePath": file_path, "startLine": start,
            "endLine": end, "snippet": None, "reason": None}

    fs = source_map.get(file_path)
    if fs is None:
        info["reason"] = "file not provided"
        return info

    if start is None:  # whole-file citation: presence is enough
        info["status"] = "verified"
        return info

    needed = list(range(start, (end or start) + 1))
    if not all(n in fs.lines for n in needed):
        info["reason"] = "lines not in provided source"
        return info

    info["status"] = "verified"
    info["snippet"] = "\n".join(fs.lines[n] for n in needed)
    return info


def verify_page_citations(content: str, source_map: Dict[str, FileSource]) -> Dict[str, dict]:
    """Resolve every `[label]()` citation in the page markdown.

    Returns {label: resolved-info}. Non-citation empty links are skipped;
    repeated labels collapse to one entry.
    """
    out: Dict[str, dict] = {}
    for label in _EMPTY_LINK_RE.findall(content or ""):
        label = label.strip()
        if label in out:
            continue
        info = resolve_citation(label, source_map)
        if info is not None:
            out[label] = info
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/ubuntu/deepwiki-open && .venv/bin/python -m pytest tests/unit/test_citation_grounding.py -v`
Expected: PASS (all Task 1 + Task 2 tests).

- [ ] **Step 5: Commit**

```bash
cd /home/ubuntu/deepwiki-open
git add api/citation_grounding.py tests/unit/test_citation_grounding.py
git commit -m "feat: resolve and verify page citations against provided source"
```

---

## Task 3: Backend models — `CitationInfo` + `WikiPage.citations`

**Files:**
- Modify: `api/api.py:50-60` (`WikiPage` model; add `CitationInfo` above it)

- [ ] **Step 1: Add the `CitationInfo` model and the `citations` field**

In `api/api.py`, immediately BEFORE `class WikiPage(BaseModel):` (line 50), add:

```python
class CitationInfo(BaseModel):
    """Verification verdict for one source citation on a wiki page."""
    status: str  # "verified" | "broken"
    filePath: str
    startLine: Optional[int] = None
    endLine: Optional[int] = None
    snippet: Optional[str] = None
    reason: Optional[str] = None
```

Then add one field to `WikiPage` (after `relatedPages: List[str]`, line 59):

```python
    citations: Dict[str, CitationInfo] = {}
```

(`Dict`, `Optional`, and `BaseModel` are already imported at `api/api.py:9,12`.)

- [ ] **Step 2: Verify the module imports cleanly**

Run: `cd /home/ubuntu/deepwiki-open && .venv/bin/python -c "from api.api import WikiPage, CitationInfo; print(WikiPage(id='x', title='t', content='', filePaths=[], importance='high', relatedPages=[]).citations)"`
Expected: prints `{}` (old-shape pages still construct; `citations` defaults to empty).

- [ ] **Step 3: Commit**

```bash
cd /home/ubuntu/deepwiki-open
git add api/api.py
git commit -m "feat: WikiPage.citations sidecar field + CitationInfo model"
```

---

## Task 4: Wire grounding into the generator

**Files:**
- Modify: `api/wiki_generator.py` (import; `retrieve_for_generation` return; per-page store point)
- Test: `tests/unit/test_wiki_generator.py`

Context: `retrieve_for_generation` (`api/wiki_generator.py:207-230`) currently returns only the formatted context string. We also need the retrieved documents to build the source map. The per-page store happens at `api/wiki_generator.py:430` (`generated[page["id"]] = {**page, "content": content}`).

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_wiki_generator.py` (uses the existing `STRUCTURE_XML`, `make_job`, `FakeDispatch`, `read_cache`, `run`, and the autouse `engine_env`/`FakeRAG` which returns a doc with `file_path="a.py"` and NO line span):

```python
def test_page_citations_verified_and_broken(tmp_path):
    # FakeRAG provides file a.py (whole-file, no line span). A whole-file cite to
    # a.py verifies; a cite to a file never provided is broken.
    body = ("# P\n\nClaim one. Sources: [a.py]()\n\n"
            "Claim two. Sources: [ghost.py:1-2]()")
    job = make_job(self_review=False)
    dispatch = FakeDispatch([STRUCTURE_XML, body, body, body])

    run(run_generation(job, dispatch))

    pages = read_cache(tmp_path, job)["generated_pages"]
    cites = next(iter(pages.values()))["citations"]
    assert cites["a.py"]["status"] == "verified"
    assert cites["ghost.py:1-2"]["status"] == "broken"
    assert cites["ghost.py:1-2"]["reason"] == "file not provided"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /home/ubuntu/deepwiki-open && .venv/bin/python -m pytest tests/unit/test_wiki_generator.py::test_page_citations_verified_and_broken -v`
Expected: FAIL — `KeyError: 'citations'` (the stored page has no `citations` yet).

- [ ] **Step 3: Add the import**

In `api/wiki_generator.py`, after the existing `from api.prompt_assembly import (...)` block (lines 29-31), add:

```python
from api.citation_grounding import build_source_map, verify_page_citations
```

- [ ] **Step 4: Make `retrieve_for_generation` return the documents too**

In `api/wiki_generator.py`, replace the body of `retrieve_for_generation` (lines 207-230) so all three return paths yield `(context_text, documents)`:

```python
    async def retrieve_for_generation(inner_prompt: str,
                                      file_path: str = "") -> tuple:
        """The websocket's retrieval gate, replicated for generation calls.

        Returns (context_text, documents): the formatted context string AND the
        raw retrieved documents (used to build the citation source map). The
        websocket retrieves whenever the message is <= 8000 tokens (so standard
        page prompts DID get RAG context in the browser flow — only oversized
        messages like big structure prompts went without). The retrieval query
        mirrors its fallback chain: a filePath-focused query when filePath is set
        (deep-dive pages), else the message itself.
        """
        tokens = count_tokens(inner_prompt,
                              is_ollama_embedder=(job.provider == "ollama"))
        logger.info(f"Request size: {tokens} tokens")
        if tokens > 8000:
            logger.warning(f"Request exceeds recommended token limit ({tokens} > 7500)")
            return "", []
        rag_query = f"Contexts related to {file_path}" if file_path else inner_prompt
        try:
            retrieved = await asyncio.to_thread(rag, rag_query, language=job.language)
            documents = (retrieved[0].documents
                         if retrieved and retrieved[0].documents else [])
            return format_context_text(retrieved), documents
        except Exception as e:
            # Continue without RAG if there's an error (websocket behavior)
            logger.error(f"Error in RAG retrieval: {str(e)}")
            return "", []
```

- [ ] **Step 5: Update the caller + initialise per-page docs**

In `api/wiki_generator.py`, the per-page loop initialises `file_content, file_path = "", ""` at line 347. Right after that line, add an init for the documents so it exists even when generation raises:

```python
        page_documents = []
```

Then change the generation-retrieval call (currently line 381-382):

```python
            page_context = await retrieve_for_generation(
                page_inner, file_path=requested_file_path)
```

to:

```python
            page_context, page_documents = await retrieve_for_generation(
                page_inner, file_path=requested_file_path)
```

- [ ] **Step 6: Build the map + verify at the store point**

In `api/wiki_generator.py`, replace the store line (currently line 430):

```python
        generated[page["id"]] = {**page, "content": content}
```

with:

```python
        # Verify citations against the exact source we showed the model, so the
        # UI can show real source text for grounded claims and flag the rest.
        if content.startswith("Error generating content:"):
            citations = {}
        else:
            source_map = build_source_map(file_content, file_path, page_documents)
            citations = verify_page_citations(content, source_map)
        generated[page["id"]] = {**page, "content": content, "citations": citations}
```

- [ ] **Step 7: Run the new test + the generator suite**

Run: `cd /home/ubuntu/deepwiki-open && .venv/bin/python -m pytest tests/unit/test_wiki_generator.py -v`
Expected: PASS — the new `test_page_citations_verified_and_broken` plus all existing generator tests (the extra `citations` key does not affect their assertions; `WikiPage(**p)` accepts it via Task 3).

- [ ] **Step 8: Commit**

```bash
cd /home/ubuntu/deepwiki-open
git add api/wiki_generator.py tests/unit/test_wiki_generator.py
git commit -m "feat: verify page citations against provided source at generation time"
```

---

## Task 5: Frontend types

**Files:**
- Modify: `src/types/wiki/wikipage.tsx` (add `CitationInfo` + `citations`)
- Modify: `src/app/[owner]/[repo]/page.tsx:26-36` (local `WikiPage` interface)

- [ ] **Step 1: Add the type to the shared `WikiPage`**

In `src/types/wiki/wikipage.tsx`, add the `CitationInfo` interface above `WikiPage`, and a `citations` field to `WikiPage` (after `relatedPages: string[];`):

```ts
export interface CitationInfo {
  status: 'verified' | 'broken';
  filePath: string;
  startLine?: number;
  endLine?: number;
  snippet?: string;
  reason?: string;
}
```

```ts
  citations?: Record<string, CitationInfo>;
```

- [ ] **Step 2: Add `citations` to the page-component's local `WikiPage`**

In `src/app/[owner]/[repo]/page.tsx`, the local `interface WikiPage` (lines 26-36) duplicates the shape. Add after `relatedPages: string[];`:

```ts
  citations?: Record<string, {
    status: 'verified' | 'broken';
    filePath: string;
    startLine?: number;
    endLine?: number;
    snippet?: string;
    reason?: string;
  }>;
```

- [ ] **Step 3: Type-check (compile only; no behavior change yet)**

Run (containerized toolchain per `deepwiki-frontend-js-toolchain` memory):
`cd /home/ubuntu/deepwiki-open && docker run --rm -v "$PWD":/app -w /app node:20-slim npx tsc --noEmit`
Expected: PASS (no new type errors from these additions).

- [ ] **Step 4: Commit**

```bash
cd /home/ubuntu/deepwiki-open
git add src/types/wiki/wikipage.tsx "src/app/[owner]/[repo]/page.tsx"
git commit -m "feat: citations type on WikiPage (frontend)"
```

---

## Task 6: Render citation states in `Markdown.tsx`

**Files:**
- Modify: `src/components/Markdown.tsx` (props, `a()` renderer, two new components)
- Test: `src/components/Markdown.citation.test.tsx`

Context: `Markdown.tsx:84-125` is the `a()` renderer. Today, for an empty-href citation it builds a blob URL (`:101-110`) or a plain span for local repos (`:112-116`). We insert a citations-map lookup BEFORE that blob-link logic so verified/broken win, and fall through to the existing behavior when the label is absent from the map.

- [ ] **Step 1: Write the failing tests**

In `src/components/Markdown.citation.test.tsx`, change the `render` helper to accept a third `citations` arg, then add four tests. Replace the existing `render` definition:

```tsx
const render = (content: string, repoInfo?: RepoInfo, citations?: Record<string, unknown>) =>
  renderToStaticMarkup(React.createElement(Markdown, { content, repoInfo, citations }));
```

Add a new `describe` block at the end of the file:

```tsx
describe('Markdown citation grounding', () => {
  it('verified citation with snippet shows the source text, no link', () => {
    const citations = {
      'CAL101.txt:51-54': {
        status: 'verified', filePath: 'CAL101.txt',
        startLine: 51, endLine: 54, snippet: 'MOVE A TO B',
      },
    };
    const html = render('Sources: [CAL101.txt:51-54]()', gitlab, citations);
    expect(html).toContain('MOVE A TO B');          // real source text inlined
    expect(html).not.toContain('/-/blob/');         // no gitlab link
  });

  it('verified whole-file citation shows a badge, no link', () => {
    const citations = {
      'CAL101.txt': { status: 'verified', filePath: 'CAL101.txt' },
    };
    const html = render('Sources: [CAL101.txt]()', gitlab, citations);
    expect(html).toContain('CAL101.txt');
    expect(html).not.toContain('/-/blob/');
  });

  it('broken citation shows a red unverified marker, no link', () => {
    const citations = {
      'GHOST.txt:1-5': {
        status: 'broken', filePath: 'GHOST.txt',
        startLine: 1, endLine: 5, reason: 'file not provided',
      },
    };
    const html = render('Sources: [GHOST.txt:1-5]()', gitlab, citations);
    expect(html).toContain('unverified');
    expect(html).not.toContain('/-/blob/');
  });

  it('citation absent from the map falls back to a blob link (regression)', () => {
    const html = render('Sources: [CAL101.txt:51-54]()', gitlab);
    expect(html).toContain(
      'href="https://gitlab.reslv.one/poc/code2_sqlcbl_cal101/-/blob/main/CAL101.txt#L51-54"',
    );
  });
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run (containerized vitest per `deepwiki-frontend-js-toolchain` memory):
`cd /home/ubuntu/deepwiki-open && docker run --rm -v "$PWD":/app -w /app node:20-slim npx vitest run src/components/Markdown.citation.test.tsx`
Expected: FAIL — the verified/broken tests fail (snippet/marker not rendered; the citation still renders as a blob link); the regression test passes.

- [ ] **Step 3: Add the prop + the two presentational components**

In `src/components/Markdown.tsx`, import the type and extend props. Change the import at line 10 area to also pull the type:

```tsx
import RepoInfo from '@/types/repoinfo';
import { CitationInfo } from '@/types/wiki/wikipage';
```

Change `MarkdownProps` (lines 32-35) to:

```tsx
interface MarkdownProps {
  content: string;
  repoInfo?: RepoInfo;
  citations?: Record<string, CitationInfo>;
}
```

Change the component signature (line 36) to destructure `citations`:

```tsx
const Markdown: React.FC<MarkdownProps> = ({ content, repoInfo, citations }) => {
```

Add these two components just ABOVE the `nodeToPlainText` function (around line 13), so they are module-level:

```tsx
// A verified citation: shows the cited filename, expandable to the real source
// text we provided the model. No external link — the text IS the evidence.
const CitationSnippet: React.FC<{ label: string; snippet?: string }> = ({ label, snippet }) => {
  const [open, setOpen] = React.useState(false);
  const badge = "text-green-700 dark:text-green-400 font-medium hover:underline";
  if (!snippet) {
    return <span className={badge}>✓ {label}</span>;
  }
  return (
    <span className="citation-verified">
      <button type="button" onClick={() => setOpen((o) => !o)} className={badge}>
        ✓ {label}
      </button>
      <span
        className={`block font-mono text-xs whitespace-pre overflow-x-auto my-1 p-2 rounded bg-gray-100 dark:bg-gray-800 ${open ? '' : 'hidden'}`}
      >
        {snippet}
      </span>
    </span>
  );
};

// A broken citation: the cited file/lines were not in the source we gave the
// model, so the claim may be fabricated.
const BrokenCitation: React.FC<{ label: string; reason?: string }> = ({ label, reason }) => (
  <span title={reason} className="text-red-600 dark:text-red-400 font-medium">
    ⚠ {label} — unverified
  </span>
);
```

(`React` is already imported at the top of the file, so `React.useState` is available.)

- [ ] **Step 4: Insert the lookup in the `a()` renderer**

In `src/components/Markdown.tsx`, inside the `a()` renderer, find the line (≈99-100):

```tsx
      const text = nodeToPlainText(children);
      const cite = text ? parseCitation(text) : null;
```

Immediately AFTER those two lines, insert the citations-map branch (this runs before the existing `if (text && cite && repoInfo)` block, so verified/broken take precedence; absence falls through to today's behavior):

```tsx
      const info = text ? citations?.[text] : undefined;
      if (text && info) {
        if (info.status === 'verified') {
          return <CitationSnippet label={text} snippet={info.snippet} />;
        }
        return <BrokenCitation label={text} reason={info.reason} />;
      }
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd /home/ubuntu/deepwiki-open && docker run --rm -v "$PWD":/app -w /app node:20-slim npx vitest run src/components/Markdown.citation.test.tsx`
Expected: PASS — all four new tests plus the three pre-existing citation tests (the regression test confirms absent-from-map still renders the blob link).

- [ ] **Step 6: Commit**

```bash
cd /home/ubuntu/deepwiki-open
git add src/components/Markdown.tsx src/components/Markdown.citation.test.tsx
git commit -m "feat: render verified citations as inline source text, broken as unverified"
```

---

## Task 7: Pass `citations` at the Markdown call site

**Files:**
- Modify: `src/app/[owner]/[repo]/page.tsx:1162-1166` (the `<Markdown>` usage)

- [ ] **Step 1: Thread the prop**

In `src/app/[owner]/[repo]/page.tsx`, the page-content `<Markdown>` (lines 1162-1165) currently reads:

```tsx
                    <Markdown
                      content={generatedPages[currentPageId].content}
                      repoInfo={effectiveRepoInfo}
                    />
```

Change it to also pass the citations map:

```tsx
                    <Markdown
                      content={generatedPages[currentPageId].content}
                      repoInfo={effectiveRepoInfo}
                      citations={generatedPages[currentPageId].citations}
                    />
```

- [ ] **Step 2: Type-check**

Run: `cd /home/ubuntu/deepwiki-open && docker run --rm -v "$PWD":/app -w /app node:20-slim npx tsc --noEmit`
Expected: PASS (the local `WikiPage.citations` from Task 5 matches `MarkdownProps.citations`).

- [ ] **Step 3: Commit**

```bash
cd /home/ubuntu/deepwiki-open
git add "src/app/[owner]/[repo]/page.tsx"
git commit -m "feat: pass citation grounding map to the page Markdown renderer"
```

---

## Task 8: Full sweep + version bump

**Files:**
- Modify: `src/version.ts:7`

- [ ] **Step 1: Run the backend unit suite**

Run: `cd /home/ubuntu/deepwiki-open && .venv/bin/python -m pytest tests/unit/test_citation_grounding.py tests/unit/test_wiki_generator.py tests/unit/test_prompt_assembly.py tests/unit/test_wiki_prompts.py -q`
Expected: PASS. (Any pre-existing `test_all_embedders.py` failures are environment-specific and unrelated — do not block on them.)

- [ ] **Step 2: Run the frontend citation tests**

Run: `cd /home/ubuntu/deepwiki-open && docker run --rm -v "$PWD":/app -w /app node:20-slim npx vitest run src/components/Markdown.citation.test.tsx src/utils/citationUrl.test.ts`
Expected: PASS.

- [ ] **Step 3: Bump the app version**

In `src/version.ts`, change line 7:

```ts
export const APP_VERSION = '0.3.7';
```

to:

```ts
export const APP_VERSION = '0.3.8';
```

- [ ] **Step 4: Commit**

```bash
cd /home/ubuntu/deepwiki-open
git add src/version.ts
git commit -m "chore: bump APP_VERSION to 0.3.8 (citation grounding + inline source)"
```

---

## Out of scope / deploy notes

- **No flagging of uncited prose** — only citations are verified (spec decision). A fabricated paragraph with no citation is not flagged.
- **Re-generate to populate:** `citations` only lands on pages generated after this change. Old cached pages have no map and render via the legacy blob-link fallback. Force-regenerate affected wikis (per the `deepwiki-rag-index-fix` memory) to get grounded citations.
- **Standard-page line-ranged citations** need a line-numbered RAG index (the RAG-line-numbers work in `docs/superpowers/plans/2026-06-10-rag-line-numbers-and-empty-source-guard.md`). Until a repo is re-indexed, standard-page line-ranged citations resolve as broken (whole-file citations still verify). Deep-dive pages verify immediately from the injected full source.
- Per project convention (and memory), the `APP_VERSION` bump is required before the next image build.

## Self-Review notes (done)

- **Spec coverage:** verification semantics → Tasks 1-2; sidecar storage/model → Task 3; generation-time wiring → Task 4; frontend types → Task 5; verified/broken/legacy rendering (no GitLab link for verified) → Task 6; prop threading → Task 7; version bump → Task 8. Resolution table rows are each tested in Task 2.
- **Type consistency:** `build_source_map(file_content, file_path, rag_documents)`, `resolve_citation(label, source_map)`, `verify_page_citations(content, source_map)`, and `FileSource(lines=...)` are used identically across module, tests, and generator wiring. `CitationInfo` fields (`status/filePath/startLine/endLine/snippet/reason`) match between `api/api.py`, the TS types, and `Markdown.tsx` consumption.
- **Placeholder scan:** no TBD/TODO; every code step shows complete code.
- **Backward compatibility:** `citations` defaults to `{}` (backend) / optional (frontend); absent-from-map citations fall back to the existing blob-link rendering (Task 6 regression test).
