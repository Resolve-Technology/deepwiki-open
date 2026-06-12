# Citation Grounding & Inline Source Text — Design

**Date:** 2026-06-12
**Status:** Approved (brainstorming), ready for implementation plan

## Problem

Generated wiki pages mix claims that are genuinely grounded in the analysed
source with claims the model may have fabricated, and the reader cannot tell
which is which. Today every `[file.cbl:12-34]()` citation renders as a clickable
**GitLab/GitHub blob link** (`src/components/Markdown.tsx:101`,
`src/utils/citationUrl.ts`). The user wants two things:

1. **A clear indication of whether content is "made up" by the AI or based on
   fact.**
2. **For fact-based content, drop the GitLab link and just show the source
   text.**

## Key insight

At generation time the backend already holds the *exact* source it gave the
model:

- **Deep-dive pages** (`page-analysis-*`): the full, line-numbered program
  source in `file_content` (`api/wiki_generator.py:360-365`), numbered as
  `"   12 | <code>"` by `number_source_lines`.
- **Standard pages**: the retrieved RAG chunks, each carrying `file_path` and
  (after the RAG-line-numbers work) `start_line` / `end_line` metadata.

That gives us **ground truth** to verify every citation mechanically — no extra
LLM call. A citation that resolves to real provided lines is "fact-based"; one
that points at a file we never gave the model, or at lines outside what we
supplied, is treated as possibly fabricated.

## Decisions (locked during brainstorming)

- **Fact test:** mechanical — a citation is *verified* iff it resolves to a real
  file we provided AND (when line numbers are given) those lines fall within the
  source we supplied. No text-similarity or LLM self-grading.
- **Verified rendering:** an **inline expandable snippet** — the citation shows
  as a small `✓ file.cbl:12-34` disclosure; expanding it reveals the real source
  lines. **No GitLab link.**
- **Broken rendering:** a visible red `⚠ file.cbl:12-34 — unverified` marker.
- **Flag scope:** **citations only.** Uncited prose stays neutral (no prose
  segmentation, no false alarms).
- **Resolution point:** **at generation time, baked in.** Verification reflects
  exactly what the model was shown; refresh by re-generating.
- **Storage:** **sidecar map** on the stored page object; markdown `content` is
  left byte-for-byte unchanged. Backward compatible.

## Architecture

Two independent layers joined by one new field (`citations`):

```
generation time                     view time
───────────────                     ─────────
build_source_map(...)               Markdown.tsx a() renderer
verify_page_citations(content) ──►  looks up each [label]() in `citations`:
  → citations: {label: {...}}         verified → <CitationSnippet> (expandable text)
stored on the page object             broken   → <BrokenCitation> (red marker)
                                      absent   → legacy blob-link fallback
```

### Backend: `api/citation_grounding.py` (new, pure, unit-tested)

```python
# A provided line range of real source text for one file.
# (start_line, end_line, text) with text indexed to start_line.

def build_source_map(file_content: str, rag_documents) -> dict:
    """file_path -> list[(start_line, end_line, text)] of source we GAVE the model.

    - Deep-dive `file_content` is line-numbered ("   12 | code"); strip the
      "{n:>6} | " prefix back to raw text and record the covered line span. The
      file_path is the deep-dive page's first filePath.
    - Each RAG document with `start_line`/`end_line` in meta_data contributes a
      span under its `file_path`. Documents without line spans (old indexes)
      contribute a span with no line numbers → whole-file presence only.
    """

def resolve_citation(label: str, source_map: dict) -> dict:
    """Resolve one citation label ("file.cbl:12-34" / "file.cbl").

    Returns {status, filePath, startLine?, endLine?, snippet?, reason?}:
      - file not in source_map            -> broken, reason "file not provided"
      - lines outside any provided span   -> broken, reason "lines not in provided source"
      - whole-file citation, file present -> verified, no snippet
      - line range within a span          -> verified, snippet = exact source text
    """

def verify_page_citations(content: str, source_map: dict) -> dict:
    """Extract every `[label]()` citation from the finished markdown and resolve
    each. Returns {label: resolve_citation(...)}. Reuses the citation label
    grammar already encoded in `src/utils/citationUrl.ts` CITATION_RE — mirror it
    in Python: `^([^:]+\\.[A-Za-z0-9]+)(?::(\\d+)(?:-(\\d+))?)?$` applied to the
    label text inside `[...]()`.
    """
```

`snippet` text is the **raw** source (prefix stripped) so the frontend can show
clean code. The label is the dictionary key so it matches `parseCitation`'s
input on the frontend exactly.

### Backend wiring: `api/wiki_generator.py`

1. `retrieve_for_generation` (`api/wiki_generator.py:207-230`) currently returns
   only the formatted `context_text` string. It must **also surface the
   retrieved documents** (e.g. return `(context_text, retrieved_documents)`, or
   set them on a small dataclass) so the generation path can build the source
   map from the *same* chunks it fed the model. Update the one caller at
   `:381`. (The self-review retrieval at `:408-411` does not need to change the
   stored map.)
2. After a page's `content` is finalised — right before
   `generated[page["id"]] = {**page, "content": content}` at `:430` — build the
   map and verify:
   ```python
   source_map = build_source_map(file_content, page_documents)
   citations = verify_page_citations(content, source_map)
   generated[page["id"]] = {**page, "content": content, "citations": citations}
   ```
   For error pages (`content` starts with `"Error generating content:"`),
   `citations` is an empty dict (no work, frontend falls back).

### Storage / models

- `api/api.py:50` `WikiPage` (BaseModel): add
  `citations: Dict[str, CitationInfo] = {}` with a new `CitationInfo` model
  (`status: str`, `filePath: str`, `startLine: Optional[int]`,
  `endLine: Optional[int]`, `snippet: Optional[str]`, `reason: Optional[str]`).
  Default `{}` keeps old caches loadable.
- Wiki cache read/write carries the field automatically (it is part of the page
  dict). No migration: pages without `citations` deserialize to `{}`.

### Frontend: `src/types/wiki/wikipage.tsx` + `src/app/[owner]/[repo]/page.tsx:26`

Add to both `WikiPage` interfaces:
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

### Frontend: `src/components/Markdown.tsx`

- Extend `MarkdownProps` with `citations?: Record<string, CitationInfo>`.
- In the `a()` renderer (`:84-125`), after computing `cite = parseCitation(text)`
  for an empty-href link, look the label up in `citations`:
  - **verified** → render `<CitationSnippet label={text} info={info} />`: a
    disclosure (`<details>`/button) showing `✓ {label}`; expanded body is
    `info.snippet` in a `<pre>`/code block. Whole-file verified (no snippet) →
    a `✓ {label}` badge with no expander. **No `<a href>`.**
  - **broken** → render `<BrokenCitation label={text} reason={info.reason} />`:
    a red `⚠ {label} — unverified` span (title=reason).
  - **absent** (old page / no map) → keep today's behavior exactly: build the
    blob URL and render the link (`:101-117`).
- New small presentational components `CitationSnippet` and `BrokenCitation`
  (same file or a sibling `src/components/CitationSnippet.tsx`).
- Pass `citations={generatedPages[currentPageId].citations}` at the Markdown
  call site (`src/app/[owner]/[repo]/page.tsx:1162`).

The existing top-of-page `<details>` "Relevant source files" blob links have
**real hrefs**, so they hit the `if (href)` branch (`:90`) and are untouched —
only empty-href `Sources:` citations change.

## Resolution semantics (reference table)

| Citation | Source map state | Result |
|---|---|---|
| `file.cbl:12-34` | file present, 12-34 within a provided span | **verified** + snippet |
| `file.cbl:12-34` | file present, 12-34 outside provided spans | **broken** ("lines not in provided source") |
| `file.cbl:12-34` | file absent | **broken** ("file not provided") |
| `file.cbl` | file present (any span) | **verified**, no snippet |
| `file.cbl` | file absent | **broken** ("file not provided") |

For standard pages "provided span" = the union of retrieved RAG chunk ranges,
so a citation to a real line the model was *not* shown is correctly broken.

## Edge cases

- **Numbered-prefix stripping:** `build_source_map` must strip exactly the
  `f"{n:>6} | "` prefix `number_source_lines` adds, and key text by the real
  line number. A round-trip test (`number_source_lines` → `build_source_map`)
  guards this.
- **CRLF:** `number_source_lines` already normalises to `\n`; the map indexes by
  the normalised lines.
- **Duplicate citations:** same label appears twice → one map entry, both
  render identically. Fine.
- **Whole-file deep-dive citation** when `file_content` is present → verified,
  no snippet (the whole program is the source; showing it inline is too large).
- **RAG chunk spans absent** (pre-line-number indexes): documents contribute
  whole-file presence only, so line-ranged citations to those files resolve
  **broken**. Acceptable and honest until re-index; note it in deploy.
- **Local repos:** deep-dive injects no `file_content` and standard pages still
  have RAG chunks, so verification works for standard pages; deep-dive on local
  repos yields an empty map → citations fall back to neutral/legacy. No GitLab
  link exists for local repos anyway.

## Testing

**Backend (pytest, `tests/unit/test_citation_grounding.py`):**
- `build_source_map`: deep-dive numbered content → correct spans + raw text;
  RAG docs with spans; RAG docs without spans (whole-file only); CRLF.
- `resolve_citation`: each row of the semantics table.
- `verify_page_citations`: extracts labels from realistic markdown
  (`Sources: [a.cbl:1-3](), [b.cbl]()`), skips real-href links, returns the map.
- Round-trip: `number_source_lines(src)` → `build_source_map` →
  `resolve_citation` returns the original lines as the snippet.

**Frontend (vitest, extend `src/components/Markdown.citation.test.tsx`):**
- verified-with-snippet → renders an expander, no `<a href>`, shows snippet text
  on expand.
- verified-whole-file → badge, no expander, no link.
- broken → red marker with reason, no link.
- citation absent from map → legacy blob link still renders (regression guard).

## Out of scope

- Detecting fabrication in **uncited** prose (no prose segmentation).
- Text-similarity / semantic checking that a snippet *supports* the claim.
- LLM self-grading of claims.
- Re-verifying old cached pages: they render via the legacy link fallback until
  re-generated.

## Deploy notes

- Per project convention, bump `src/version.ts` `APP_VERSION` before the image
  build.
- Citations only populate on pages generated after this change; re-generate (or
  the standard force-regenerate per the `deepwiki-rag-index-fix` memory) to get
  grounded citations on existing wikis. Standard-page line-ranged citations also
  need a line-numbered RAG index (the RAG-line-numbers work).
