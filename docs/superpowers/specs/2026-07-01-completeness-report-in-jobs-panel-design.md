# Completeness Report in the Jobs Panel — Design

**Date:** 2026-07-01
**Status:** Approved (design), pending implementation plan

## Problem

The TSD/BRD completeness check writes `<cache>.completeness.json` and
`<cache>.completeness.md` next to the wikicache and logs a one-line summary,
but the result is invisible in the UI. After a generation job finishes, a user
has no way to see — without shell access to the server — how many required
template fields came through with content, how many are legitimately empty
(`None`), and how many the model dropped. This surfaces that summary in the
Jobs panel, right where generation completion is already shown.

## Goals

- After a job finishes, show a one-line completeness summary in its Jobs-panel
  row: `Completeness: <content> ok / <empty_none> None / <missing> ✗`.
- Provide a "view full report" link that opens the generated Markdown report.
- Degrade gracefully: wikis with no report (older runs, non-template wikis)
  show nothing extra; nothing about this can break the panel.

## Non-goals (YAGNI)

- No inline per-page ✓/○/✗ table in the panel (that lives in the `.md` report,
  reachable via the link).
- No summary for `failed`/`cancelled`/partial jobs — `done` only.
- No new client state store; component-local state keyed by job id.
- No in-app Markdown rendering of the report — the link opens the raw `.md`.

## Architecture

### Backend: `GET /api/wiki_completeness` (`api/api.py`)

Query params mirror `/api/wiki_cache` plus a format selector:
`owner`, `repo`, `repo_type`, `language`, `provider`, `model`,
`format` ∈ {`json` (default), `md`}.

Resolution: `get_wiki_cache_path(owner, repo, repo_type, language, provider,
model)` → `completeness_report_paths(cache_path)` (from
`api.tsd_brd_completeness`) → the sibling `.completeness.json` / `.md` paths.
`get_wiki_cache_path` already enforces the path-traversal guards, so no new
validation is needed. Note: unlike `/api/wiki_cache`, this endpoint does not
fall back to the "newest" cached version when `provider`/`model` are omitted —
it resolves the exact (versioned) filename for the given params (or the legacy
un-versioned one when both are absent). The Jobs panel always supplies
`provider`+`model`, so this is not a concern for the caller in this spec.

- `format=json`: if the `.completeness.json` file exists, read and return the
  parsed object; otherwise return **200 with a null body** (matching
  `get_cached_wiki`'s not-found convention so the frontend degrades quietly).
- `format=md`: if the `.completeness.md` file exists, return its text as a
  `PlainTextResponse(media_type="text/markdown; charset=utf-8")`; otherwise
  return 404.
- Any read error is caught and treated as "not found" (null / 404) — this
  endpoint never 500s on a missing or unreadable sibling file.

### Frontend proxy: `src/app/api/wiki_completeness/route.ts`

A thin Next.js route forwarding the query string to
`${SERVER_BASE_URL}/wiki_completeness`, mirroring the existing
`src/app/api/repo_file/route.ts` proxy. It must pass through BOTH formats:
for `format=md` it returns the upstream body as text with the upstream
`Content-Type`; for `json` (and default) it returns JSON with the upstream
status. On upstream/network error it returns a non-fatal empty/JSON-null body
so the client treats it as "no report".

### JobsPanel (`src/components/JobsPanel.tsx`)

For each job whose `status === 'done'`:

- Lazily fetch `/api/wiki_completeness?owner=…&repo=…&repo_type=…&language=…&provider=…&model=…&format=json` once and cache the result in a component-local map keyed by `job.id` (avoid refetching on every poll/render).
- The fetch is fire-and-forget with respect to errors: a rejected fetch or a
  null body simply means "no summary for this job".
- When a report object is present, render below the existing job status line:
  - Summary text: `Completeness: {summary.headings.content} ok / {summary.headings.empty_none} None / {summary.headings.missing} ✗`, with the `✗` count styled red when `missing > 0` (and neutral when `0`).
  - A "view full report ↗" link (anchor with `target="_blank" rel="noopener noreferrer"`) to the same endpoint with `format=md`.
- When no report is present, render nothing extra for that job.

The query is built from fields already on the `WikiJob` object
(`repo.owner`, `repo.repo`, `repo.type`, `language`, `provider`, `model`).

## Data flow

Generation writes `X.completeness.json`/`.md` (existing behavior) → user opens
the app → JobsPanel polls `/api/wiki_jobs` (existing) → for each `done` job it
fetches `/api/wiki_completeness?…format=json` via the Next proxy → backend reads
the sibling `.json` → panel renders the summary line + `format=md` link → the
link fetches the sibling `.md` through the proxy and shows it in a new tab.

## Error handling

- Missing/unreadable `.json` → backend returns null → panel shows no summary.
- Missing `.md` → backend returns 404 → the link opens a plain "not found" body
  (acceptable; the summary line already told the user counts).
- Proxy/network failure → treated as "no report"; panel unaffected.
- Malformed report JSON is not expected (we write it), but a fetch/parse error
  in the panel is caught and shows no summary rather than throwing.

## Testing

Backend (`tests/unit/`, an API-level test using FastAPI `TestClient`):
1. `format=json` with a fixture `.completeness.json` on disk → returns the
   parsed object with the expected `summary`.
2. `format=md` with a fixture `.completeness.md` → returns its text and a
   `text/markdown` content type.
3. Absent report → `format=json` returns null body (200); `format=md` returns
   404.
4. Path-traversal attempt in `owner`/`repo` → rejected (400) by the reused
   `get_wiki_cache_path` guard.

Frontend (`src/components/`, vitest + testing-library):
5. A `done` job with a mocked `/api/wiki_completeness` JSON response renders the
   summary line (`45 ok / 6 None / 1 ✗`) and a `view full report` link whose
   href carries `format=md`.
6. A `done` job whose completeness fetch resolves to null renders no summary
   line and no link.
7. A non-`done` (e.g. `running`) job never triggers the completeness fetch / row.
