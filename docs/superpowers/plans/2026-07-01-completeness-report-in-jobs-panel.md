# Completeness Report in the Jobs Panel — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface the TSD/BRD completeness report in the Jobs panel — a one-line summary per finished job plus a "view full report" link to the generated Markdown.

**Architecture:** A log-safe I/O helper reads the sibling `.completeness.json`/`.md` files; a thin `GET /api/wiki_completeness` endpoint and a Next.js proxy serve them; a prop-driven `CompletenessSummary` component renders the summary + link, wired into `JobsPanel` which lazily fetches the report for each `done` job.

**Tech Stack:** Python 3.12 / FastAPI (backend), Next.js 15 / React / TypeScript (frontend), pytest + vitest. Backend tests: `.venv/bin/python -m pytest`. Frontend tests: vitest in an ephemeral `node:20-slim` container.

## Global Constraints

- The endpoint mirrors `/api/wiki_cache` params: `owner`, `repo`, `repo_type`, `language`, `provider`, `model`, plus `format` ∈ {`json` (default), `md`}.
- `format=json` returns the parsed report, or **200 with null body** when the file is absent (matches `get_cached_wiki`). `format=md` returns the Markdown as `text/markdown; charset=utf-8`, or **404** when absent. The endpoint never 500s on a missing/unreadable file.
- Report file paths derive from the wikicache path via `completeness_report_paths(cache_path)` (in `api/tsd_brd_completeness.py`): `X.json` → `X.completeness.json` / `X.completeness.md`.
- Summary line format: `Completeness: {content} ok / {empty_none} None / {missing} ✗`, with the `✗` count red when `missing > 0`.
- The completeness summary is shown for `done` jobs only.
- Backend tests run via `.venv/bin/python -m pytest` (system `python`/`pytest` absent). Frontend tests run via vitest in a `node:20-slim` container (host has no node): `docker run --rm -v "$PWD":/app -w /app node:20-slim sh -c "npx vitest run <file>"`.
- Do NOT import `api.api` in a unit test (it configures a file logger that is unwritable in this sandbox). Keep new testable logic in modules that import only `json`/`os`/`re`/`api.tsd_brd_completeness`/`api.wiki_prompts`.

---

### Task 1: Log-safe report reader (`api/completeness_io.py`)

**Files:**
- Create: `api/completeness_io.py`
- Test: `tests/unit/test_completeness_io.py`

**Interfaces:**
- Consumes: `completeness_report_paths(cache_path: str) -> tuple[str, str]` from `api/tsd_brd_completeness.py`.
- Produces: `read_completeness_report(cache_path: str, fmt: str) -> tuple[object, str | None] | None`. Returns `(parsed_json_dict, None)` for `fmt="json"`, `(markdown_text, "text/markdown; charset=utf-8")` for `fmt="md"`, or `None` when the sibling file is absent or unreadable.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_completeness_io.py
import json

from api.completeness_io import read_completeness_report


def _write(tmp_path, suffix, text):
    (tmp_path / f"deepwiki_cache_x{suffix}").write_text(text, encoding="utf-8")
    return str(tmp_path / "deepwiki_cache_x.json")


def test_reads_json_report(tmp_path):
    cache = _write(tmp_path, ".completeness.json",
                   json.dumps({"summary": {"headings": {"content": 1}}}))
    res = read_completeness_report(cache, "json")
    assert res is not None
    data, media = res
    assert data["summary"]["headings"]["content"] == 1
    assert media is None


def test_reads_md_report(tmp_path):
    cache = _write(tmp_path, ".completeness.md", "# Report\n\nbody\n")
    res = read_completeness_report(cache, "md")
    assert res is not None
    text, media = res
    assert text.startswith("# Report")
    assert media == "text/markdown; charset=utf-8"


def test_absent_report_returns_none(tmp_path):
    cache = str(tmp_path / "deepwiki_cache_x.json")  # no sibling files written
    assert read_completeness_report(cache, "json") is None
    assert read_completeness_report(cache, "md") is None


def test_malformed_json_returns_none(tmp_path):
    cache = _write(tmp_path, ".completeness.json", "{ not valid json ")
    assert read_completeness_report(cache, "json") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_completeness_io.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'api.completeness_io'`

- [ ] **Step 3: Write minimal implementation**

```python
# api/completeness_io.py
"""Read the sibling completeness report files for a wikicache path.

Kept separate from api.api so it imports no FastAPI/app state (and no file
logger), which makes it unit-testable in isolation. Pure file I/O over the
paths derived by api.tsd_brd_completeness.completeness_report_paths.
"""
import json
import os
from typing import Optional, Tuple

from api.tsd_brd_completeness import completeness_report_paths

_MD_MEDIA = "text/markdown; charset=utf-8"


def read_completeness_report(cache_path: str,
                             fmt: str) -> Optional[Tuple[object, Optional[str]]]:
    """Return (content, media_type) for the sibling report, or None if absent.

    fmt="md" -> (markdown_text, "text/markdown; charset=utf-8").
    Any other fmt (incl. "json") -> (parsed_json, None).
    Missing or unreadable file -> None (never raises).
    """
    json_path, md_path = completeness_report_paths(cache_path)
    try:
        if fmt == "md":
            if not os.path.isfile(md_path):
                return None
            with open(md_path, encoding="utf-8") as f:
                return f.read(), _MD_MEDIA
        if not os.path.isfile(json_path):
            return None
        with open(json_path, encoding="utf-8") as f:
            return json.load(f), None
    except Exception:
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_completeness_io.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add api/completeness_io.py tests/unit/test_completeness_io.py
git commit -m "feat: log-safe reader for completeness report files"
```

---

### Task 2: Backend endpoint `GET /api/wiki_completeness` (`api/api.py`)

**Files:**
- Modify: `api/api.py` (add the endpoint near the other `wiki_cache` routes, e.g. after `get_cached_wiki` ~line 743)

**Interfaces:**
- Consumes: `get_wiki_cache_path(owner, repo, repo_type, language, provider, model)` (already defined in `api/api.py:562`, enforces path-traversal guards and raises `HTTPException(400)` on bad segments); `read_completeness_report` (Task 1); `Response`/`JSONResponse` (already imported at `api/api.py:8`).
- Produces: `GET /api/wiki_completeness` route.

- [ ] **Step 1: Add the endpoint**

Insert after the `get_cached_wiki` function (the `GET /api/wiki_cache` handler) in `api/api.py`:

```python
@app.get("/api/wiki_completeness")
async def get_completeness_report(
    owner: str = Query(..., description="Repository owner"),
    repo: str = Query(..., description="Repository name"),
    repo_type: str = Query(..., description="Repository type (e.g., github, gitlab)"),
    language: str = Query(..., description="Language of the wiki content"),
    provider: Optional[str] = Query(None, description="LLM provider of the cached version"),
    model: Optional[str] = Query(None, description="Model of the cached version"),
    fmt: str = Query("json", alias="format", description="json (default) or md"),
):
    """Serve the TSD/BRD completeness report written next to the wikicache.

    format=json -> parsed report, or 200 null when absent (like get_cached_wiki).
    format=md   -> the Markdown report as text/markdown, or 404 when absent.
    """
    from api.completeness_io import read_completeness_report
    # Reuses get_wiki_cache_path's path-traversal guards (raises 400 on bad ids).
    cache_path = get_wiki_cache_path(owner, repo, repo_type, language, provider, model)
    result = read_completeness_report(cache_path, fmt)
    if result is None:
        if fmt == "md":
            return JSONResponse(status_code=404, content={"error": "No completeness report."})
        return None  # 200 with null body
    content, media = result
    if media:  # markdown
        return Response(content=content, media_type=media)
    return content  # dict -> FastAPI serializes as JSON
```

- [ ] **Step 2: Verify syntax**

Run: `.venv/bin/python -c "import ast; ast.parse(open('api/api.py').read()); print('syntax OK')"`
Expected: `syntax OK`

- [ ] **Step 3: Confirm the checker unit tests still pass (no regression to imported module)**

Run: `.venv/bin/python -m pytest tests/unit/test_completeness_io.py tests/unit/test_tsd_brd_completeness.py -q`
Expected: PASS (all)

- [ ] **Step 4: Commit**

```bash
git add api/api.py
git commit -m "feat: GET /api/wiki_completeness serves the completeness report"
```

---

### Task 3: Next.js proxy route (`src/app/api/wiki_completeness/route.ts`)

**Files:**
- Create: `src/app/api/wiki_completeness/route.ts`

**Interfaces:**
- Produces: a `GET` handler proxying to `${SERVER_BASE_URL}/wiki_completeness`, passing the query string through and preserving the upstream content type (so `format=md` returns text and `format=json` returns JSON).

- [ ] **Step 1: Create the proxy route**

Model it on `src/app/api/repo_file/route.ts`, but pass the body through with the upstream content type (the md format is not JSON):

```typescript
import { NextRequest, NextResponse } from 'next/server';

const TARGET_SERVER_BASE_URL = process.env.SERVER_BASE_URL || 'http://localhost:8001';

// Proxy for the TSD/BRD completeness report. Passes format=json|md straight
// through and preserves the upstream content type so Markdown stays text.
export async function GET(request: NextRequest) {
  try {
    const qs = request.nextUrl.searchParams.toString();
    const backendResponse = await fetch(`${TARGET_SERVER_BASE_URL}/wiki_completeness?${qs}`);
    const contentType = backendResponse.headers.get('content-type') || 'application/json';
    const body = await backendResponse.text();
    return new NextResponse(body, {
      status: backendResponse.status,
      headers: { 'Content-Type': contentType },
    });
  } catch (error) {
    console.error('Error fetching completeness report:', error);
    // Non-fatal: return JSON null so the client treats it as "no report".
    return new NextResponse('null', {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  }
}
```

- [ ] **Step 2: Type-check the new route compiles**

Run: `docker run --rm -v "$PWD":/app -w /app node:20-slim sh -c "npx tsc --noEmit -p tsconfig.json 2>&1 | grep -E 'wiki_completeness|error TS' | head"`
Expected: no errors referencing `wiki_completeness/route.ts` (pre-existing unrelated TS errors, if any in the repo, are out of scope — confirm none mention this new file).

- [ ] **Step 3: Commit**

```bash
git add src/app/api/wiki_completeness/route.ts
git commit -m "feat: Next proxy for /api/wiki_completeness (json + md)"
```

---

### Task 4: `CompletenessSummary` component + query helper (`src/components/CompletenessSummary.tsx`)

**Files:**
- Create: `src/components/CompletenessSummary.tsx`
- Test: `src/components/CompletenessSummary.test.tsx`

**Interfaces:**
- Produces:
  - `interface CompletenessReport { summary: { headings: { content: number; empty_none: number; missing: number }; rows: { present: number; missing: number }; pages_missing: string[] } }`
  - `completenessQuery(job: { repo: { owner: string; repo: string; type: string }; language: string; provider: string; model: string }): string` — the URL query string (owner/repo/repo_type/language/provider/model).
  - `CompletenessSummary: React.FC<{ report: CompletenessReport | null; mdHref: string }>` — renders the summary line + link, or nothing when `report` is null.

This is a prop-driven presentational unit (no data fetching, no `next/link`), so it renders with `renderToStaticMarkup` in the repo's node test env.

- [ ] **Step 1: Write the failing test**

```tsx
// src/components/CompletenessSummary.test.tsx
import { describe, it, expect } from 'vitest';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { CompletenessSummary, completenessQuery, CompletenessReport } from './CompletenessSummary';

const report: CompletenessReport = {
  summary: { headings: { content: 45, empty_none: 6, missing: 1 },
             rows: { present: 21, missing: 1 }, pages_missing: [] },
};

describe('CompletenessSummary', () => {
  it('renders the summary counts and a view-full-report link', () => {
    const html = renderToStaticMarkup(
      React.createElement(CompletenessSummary, { report, mdHref: '/api/wiki_completeness?x=1&format=md' }));
    expect(html).toContain('45 ok / 6 None /');
    expect(html).toContain('1 ✗');
    expect(html).toContain('view full report');
    expect(html).toContain('format=md');
    expect(html).toMatch(/text-red-600/);          // missing>0 styled red
  });

  it('renders nothing when there is no report', () => {
    const html = renderToStaticMarkup(
      React.createElement(CompletenessSummary, { report: null, mdHref: '/x' }));
    expect(html).toBe('');
  });

  it('does not style the missing count red when zero', () => {
    const clean: CompletenessReport = {
      summary: { headings: { content: 50, empty_none: 0, missing: 0 },
                 rows: { present: 22, missing: 0 }, pages_missing: [] } };
    const html = renderToStaticMarkup(
      React.createElement(CompletenessSummary, { report: clean, mdHref: '/x?format=md' }));
    expect(html).toContain('0 ✗');
    expect(html).not.toMatch(/text-red-600/);
  });

  it('completenessQuery builds the expected params', () => {
    const q = completenessQuery({
      repo: { owner: 'poc', repo: 'code1_cbl_bv401', type: 'gitlab' },
      language: 'zh-tw', provider: 'claude', model: 'claude-haiku-4-5-20251001' });
    expect(q).toContain('owner=poc');
    expect(q).toContain('repo=code1_cbl_bv401');
    expect(q).toContain('repo_type=gitlab');
    expect(q).toContain('language=zh-tw');
    expect(q).toContain('provider=claude');
    expect(q).toContain('model=claude-haiku-4-5-20251001');
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker run --rm -v "$PWD":/app -w /app node:20-slim sh -c "npx vitest run src/components/CompletenessSummary.test.tsx"`
Expected: FAIL — cannot resolve `./CompletenessSummary`.

- [ ] **Step 3: Write minimal implementation**

```tsx
// src/components/CompletenessSummary.tsx
import React from 'react';

export interface CompletenessReport {
  summary: {
    headings: { content: number; empty_none: number; missing: number };
    rows: { present: number; missing: number };
    pages_missing: string[];
  };
}

export function completenessQuery(job: {
  repo: { owner: string; repo: string; type: string };
  language: string;
  provider: string;
  model: string;
}): string {
  return new URLSearchParams({
    owner: job.repo.owner,
    repo: job.repo.repo,
    repo_type: job.repo.type,
    language: job.language,
    provider: job.provider,
    model: job.model,
  }).toString();
}

// Prop-driven: renders the completeness summary + link, or nothing when the
// report is absent. The parent fetches the report and builds mdHref.
export const CompletenessSummary: React.FC<{
  report: CompletenessReport | null;
  mdHref: string;
}> = ({ report, mdHref }) => {
  if (!report) return null;
  const h = report.summary.headings;
  return (
    <div className="mt-1 text-[11px] text-[var(--muted)]">
      Completeness: {h.content} ok / {h.empty_none} None{' / '}
      <span className={h.missing > 0 ? 'text-red-600 font-medium' : ''}>{h.missing} ✗</span>
      {' · '}
      <a
        href={mdHref}
        target="_blank"
        rel="noopener noreferrer"
        className="text-[var(--accent-primary)] hover:underline"
      >
        view full report ↗
      </a>
    </div>
  );
};
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker run --rm -v "$PWD":/app -w /app node:20-slim sh -c "npx vitest run src/components/CompletenessSummary.test.tsx"`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/components/CompletenessSummary.tsx src/components/CompletenessSummary.test.tsx
git commit -m "feat: CompletenessSummary component + completenessQuery helper"
```

---

### Task 5: Wire completeness into `JobsPanel` (`src/components/JobsPanel.tsx`)

**Files:**
- Modify: `src/components/JobsPanel.tsx` (imports near line 3-4; new state + effect after the `jobs` state ~line 55; render inside the `recentFinished` `<li>` ~line 150-166)

**Interfaces:**
- Consumes: `CompletenessSummary`, `completenessQuery`, `CompletenessReport` (Task 4); existing `jobs` state (array of `WikiJob`).

- [ ] **Step 1: Add the import**

After the existing imports at the top of `src/components/JobsPanel.tsx`:

```tsx
import { CompletenessSummary, completenessQuery, CompletenessReport } from './CompletenessSummary';
```

- [ ] **Step 2: Add report state + lazy fetch for done jobs**

Immediately after `const [jobs, setJobs] = useState<WikiJob[]>([]);` (line 55), add:

```tsx
  // Completeness report per finished job, fetched once and cached by job id.
  const [reports, setReports] = useState<Record<string, CompletenessReport | null>>({});

  useEffect(() => {
    jobs
      .filter(j => j.status === 'done' && !(j.id in reports))
      .forEach(async job => {
        try {
          const res = await fetch(`/api/wiki_completeness?${completenessQuery(job)}&format=json`);
          const data = res.ok ? await res.json() : null;
          setReports(prev => ({ ...prev, [job.id]: data as CompletenessReport | null }));
        } catch {
          setReports(prev => ({ ...prev, [job.id]: null }));
        }
      });
  }, [jobs, reports]);
```

- [ ] **Step 3: Render the summary under each finished done job**

In the `recentFinished.map(job => ( ... ))` block, replace the single-row `<li>` with a column that keeps the existing row and appends the summary. Change:

```tsx
            <li key={job.id} className="flex items-center justify-between gap-3 text-xs">
              <Link href={jobWikiUrl(job)} className="text-[var(--foreground)] hover:text-[var(--accent-primary)] truncate">
                {job.repo.owner}/{job.repo.repo}
                <span className="text-[var(--muted)]"> · {job.provider}/{job.model}</span>
              </Link>
              <span className="flex items-center gap-1.5 flex-shrink-0">
                <span
                  className={`px-2 py-0.5 rounded-full border ${statusBadgeClasses[job.status] || statusBadgeClasses.queued}`}
                  title={job.error || undefined}
                >
                  {job.status}
                </span>
                <button
                  onClick={() => removeJob(job.id)}
                  title="Remove from this list"
                  aria-label="Remove job"
                  className="px-1.5 py-0.5 rounded border border-[var(--border-color)] text-[var(--muted)] hover:text-[var(--foreground)] hover:bg-[var(--background)] transition-colors leading-none"
                >
                  ×
                </button>
              </span>
            </li>
```

to:

```tsx
            <li key={job.id} className="text-xs">
              <div className="flex items-center justify-between gap-3">
                <Link href={jobWikiUrl(job)} className="text-[var(--foreground)] hover:text-[var(--accent-primary)] truncate">
                  {job.repo.owner}/{job.repo.repo}
                  <span className="text-[var(--muted)]"> · {job.provider}/{job.model}</span>
                </Link>
                <span className="flex items-center gap-1.5 flex-shrink-0">
                  <span
                    className={`px-2 py-0.5 rounded-full border ${statusBadgeClasses[job.status] || statusBadgeClasses.queued}`}
                    title={job.error || undefined}
                  >
                    {job.status}
                  </span>
                  <button
                    onClick={() => removeJob(job.id)}
                    title="Remove from this list"
                    aria-label="Remove job"
                    className="px-1.5 py-0.5 rounded border border-[var(--border-color)] text-[var(--muted)] hover:text-[var(--foreground)] hover:bg-[var(--background)] transition-colors leading-none"
                  >
                    ×
                  </button>
                </span>
              </div>
              {job.status === 'done' && (
                <CompletenessSummary
                  report={reports[job.id] ?? null}
                  mdHref={`/api/wiki_completeness?${completenessQuery(job)}&format=md`}
                />
              )}
            </li>
```

- [ ] **Step 4: Type-check + run the existing frontend tests (no regression)**

Run: `docker run --rm -v "$PWD":/app -w /app node:20-slim sh -c "npx tsc --noEmit -p tsconfig.json 2>&1 | grep -E 'JobsPanel|CompletenessSummary|error TS' | head"`
Expected: no errors referencing `JobsPanel.tsx` or `CompletenessSummary.tsx`.

Run: `docker run --rm -v "$PWD":/app -w /app node:20-slim sh -c "npx vitest run src/components/CompletenessSummary.test.tsx src/components/Markdown.citation.test.tsx"`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add src/components/JobsPanel.tsx
git commit -m "feat: show completeness summary + report link in JobsPanel"
```

---

### Task 6: Version bump + end-to-end verification

**Files:**
- Modify: `src/version.ts`

- [ ] **Step 1: Bump APP_VERSION**

Edit `src/version.ts`, increment the patch (`0.3.23` → `0.3.24`).

- [ ] **Step 2: Rebuild + redeploy**

```bash
docker compose build deepwiki
docker compose up -d --no-deps deepwiki
```
Expected: image builds (exit 0); container recreated and healthy; `curl -s http://localhost:3000 | grep -oE '0\.3\.[0-9]+'` shows `0.3.24`.

- [ ] **Step 3: Verify the endpoint serves the existing bv401 report**

The bv401 comprehensive/haiku wiki already has a completeness report on disk. Query the endpoint through the running backend:

```bash
curl -s "http://localhost:8001/wiki_completeness?owner=poc&repo=code1_cbl_bv401&repo_type=gitlab&language=zh-tw&provider=claude&model=claude-haiku-4-5-20251001&format=json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['summary'])"
```
Expected: prints the `summary` dict (e.g. `{'headings': {'content': 45, 'empty_none': 6, 'missing': 1}, ...}`).

```bash
curl -s -o /dev/null -w "%{http_code} %{content_type}\n" "http://localhost:8001/wiki_completeness?owner=poc&repo=code1_cbl_bv401&repo_type=gitlab&language=zh-tw&provider=claude&model=claude-haiku-4-5-20251001&format=md"
```
Expected: `200 text/markdown; charset=utf-8`.

```bash
curl -s "http://localhost:8001/wiki_completeness?owner=poc&repo=does_not_exist&repo_type=gitlab&language=zh-tw&provider=claude&model=claude-haiku-4-5-20251001&format=json"
```
Expected: `null` (200 with null body).

- [ ] **Step 4: Verify through the Next proxy**

```bash
curl -s -o /dev/null -w "%{http_code}\n" "http://localhost:3000/api/wiki_completeness?owner=poc&repo=code1_cbl_bv401&repo_type=gitlab&language=zh-tw&provider=claude&model=claude-haiku-4-5-20251001&format=json"
```
Expected: `200`.

- [ ] **Step 5: Commit**

```bash
git add src/version.ts
git commit -m "chore: bump APP_VERSION for completeness report in Jobs panel"
```

---

## Self-Review

**Spec coverage:**
- Summary line in the Jobs panel for done jobs → Tasks 4, 5. ✓
- "view full report" link opening the `.md` → Tasks 4 (link), 3 (md proxy), 2 (md endpoint). ✓
- Graceful degradation (no report → nothing; errors non-fatal) → Task 1 (returns None), Task 2 (null/404, never 500s), Task 3 (error → JSON null), Task 4 (null report → renders nothing), Task 5 (fetch catch → null). ✓
- Backend `GET /api/wiki_completeness` with json/md + null/404 conventions → Task 2. ✓
- Frontend proxy passing both formats → Task 3. ✓
- Path-traversal safety → inherited from reused `get_wiki_cache_path` (Task 2); its guard is covered by its own existing suite, not re-tested here (a fresh endpoint test would require importing the log-hostile `api.api`).
- Tests → Task 1 (reader: json/md/absent/malformed), Task 4 (component + query helper). ✓

**Deviation from spec test items (justified):** The spec listed a `JobsPanel` vitest test rendering the summary line. The repo's vitest env is **node with no jsdom/testing-library** and uses `renderToStaticMarkup` (no effects), so JobsPanel's fetch-driven behavior isn't unit-testable without new deps. The testable seam is therefore the prop-driven `CompletenessSummary` component + `completenessQuery` helper (Task 4), which cover the spec's intent (summary rendered from a report; nothing rendered from null). The JobsPanel wiring (Task 5) is verified by type-check + the Task 6 e2e, consistent with the codebase (JobsPanel's existing job-fetch effect is likewise not unit-tested). Similarly, the backend endpoint's file-read logic is tested via the log-safe `api/completeness_io.py` (Task 1) rather than a heavy TestClient test.

**Placeholder scan:** none — every code/test step contains complete code.

**Type consistency:** `read_completeness_report(cache_path, fmt) -> (content, media|None) | None` used identically in Tasks 1-2. `CompletenessReport`, `completenessQuery(job)`, `CompletenessSummary({report, mdHref})` consistent across Tasks 4-5. Query param names (`owner/repo/repo_type/language/provider/model/format`) match between the endpoint (Task 2), proxy (Task 3), and `completenessQuery` (Task 4).
