# Server-Side Generation Queue — Design

**Date:** 2026-06-06
**Status:** Approved (plan written; not yet executed)
**Builds on:** per-model wiki cache (2026-06-05), self-review pass (2026-06-05)

## Problem

Wiki generation is orchestrated by the browser tab: the wiki page component opens the
websockets, schedules page after page, runs the self-review pass, and saves the cache in
a single POST at the very end. Consequences:

1. Navigating away / refreshing / closing the tab kills the run — completed pages are
   lost (tokens wasted), only the clone/embeddings survive.
2. One repo at a time, and only while the user babysits the tab.
3. No way to queue several repos and let the server work through them.

## Approved decisions

1. **Full replacement** — generation moves entirely server-side; the wiki page becomes a
   viewer + progress display. One pipeline, no duplicated prompt code in two languages.
2. **Page-granularity polling** — each completed page is saved to the wiki cache
   immediately; the UI polls job status (~3s) and re-fetches the cache as pages land.
   The token-by-token live text effect is dropped (v1 trade-off).
3. **2 parallel jobs by default** (`WIKI_JOBS_CONCURRENCY`, env-configurable) — two
   repos generate simultaneously. Known caveat: two claude jobs share the Max-plan
   quota; vLLM handles parallel batches well.

## Architecture

```
ConfigurationModal / Refresh modal           Home page                Wiki page
        │ POST /api/wiki_jobs                  │ GET /api/wiki_jobs     │ GET /api/wiki_jobs/{id}  (poll ~3s)
        ▼                                      ▼ (JobsPanel)           │ GET /api/wiki_cache       (on page-count change)
┌─────────────────────────── backend ────────────────────────────────────────────┐
│ api/wiki_jobs.py    JobManager: registry + asyncio.Queue + N worker tasks      │
│       │  per job                                                               │
│       ▼                                                                        │
│ api/wiki_generator.py  run_job():                                              │
│   1. (force?) delete target cache version                                      │
│   2. prepare_retriever (clone + embeddings — already persistent)               │
│   3. structure prompt → LLM → parse <wiki_structure> XML (3 retries)           │
│   4. for each page: RAG retrieve → page prompt → LLM                           │
│        └ self_review? → review prompt (+ rag) → corrected page                 │
│        └ save_wiki_cache(partial pages + stats)   ← INCREMENTAL                │
│   5. final save (self_reviewed flag, stats, generated_at, repo_commit)         │
│                                                                                │
│ api/wiki_prompts.py    structure / standard-page / deep-dive prompts           │
│                        (ported verbatim from page.tsx) + self-review prompts   │
│                        (ported from wikiRevision.ts)                           │
│ api/llm_dispatch.py    provider dispatch (claude native, vllm w/ discovery     │
│                        route, others) → (text, usage); context formatting      │
│                        helpers shared with the websocket chat path             │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### Job model & API

```
WikiJob {
  id, repo: RepoInfo, language, provider, model, comprehensive, self_review,
  force_regenerate, status: queued|running|done|failed|cancelled|interrupted,
  phase: cloning|embedding|structure|pages|review|saving,
  progress: { pages_total, pages_done, current_page_title },
  stats: { generation: {...}, review: {...} },   # same shape as cache stats
  error, created_at, started_at, finished_at
}
```

- `POST /api/wiki_jobs` — enqueue (WIKI_AUTH_MODE-gated like cache DELETE). Dedupe:
  409 when the same (repo, language, provider, model) is already queued/running.
  Queue cap 20 → 429. Repo access `token` is held in memory only.
- `GET /api/wiki_jobs` — queued + running + last 50 finished.
- `GET /api/wiki_jobs/{id}` — full status for polling.
- `POST /api/wiki_jobs/{id}/cancel` — cooperative cancel (checked between LLM calls);
  pages already saved remain in the cache.

### Persistence & restarts

Jobs journal to `~/.adalflow/wikicache/wiki_jobs.json` (**without tokens**) on every
status change. On startup, journaled `queued`/`running` jobs become `interrupted` —
the user re-enqueues with one click; incremental saves mean the partially generated
wiki is already in the cache and visible. (True resume-from-page-N is a noted
follow-up, not v1.)

### Token/usage accounting

Server-side the engine reads usage **directly from the model clients** (no
`<<<USAGE_JSON>>>` markers needed on this path — the markers remain for the
interactive Ask/review flows). Stats accumulate per phase exactly as today.

### Frontend changes

- **Enqueue**: ConfigurationModal's submit and the wiki page's Refresh modal POST a job
  and navigate to the wiki page; Regenerate sets `force_regenerate`.
- **Wiki page**: when a job is active for the loaded (repo, version), show a progress
  panel (phase, page x/y, current title, elapsed) polling `GET /api/wiki_jobs/{id}`;
  re-fetch the wiki cache whenever `pages_done` changes so finished pages render
  incrementally. When no job is active: today's viewer behavior.
- **Home page**: `JobsPanel` listing queued/running jobs with progress bars and cancel
  buttons — this is where multi-repo parallelism is visible and managed.
- **Removed from page.tsx**: `determineWikiStructure`, `generatePageContent`, the
  in-browser self-review block, `runStatsRef`, the end-of-run saveCache effect (~800
  lines). **Kept**: Ask, Model Review modal incl. apply-review (interactive flows),
  mermaid retry machinery, export, version switching.

### Concurrency & resource notes

- 2 workers; each job holds its repo's FAISS index in memory (existing in-memory DB
  cache is keyed per repo — already safe for concurrent jobs).
- The embedder vLLM serves both jobs' retrieval embedding calls — fine at this scale.
- Provider rate limits are shared across parallel jobs (claude Max quota especially);
  the JobsPanel makes it the user's informed choice.

### Risks & mitigations

| Risk | Mitigation |
|---|---|
| Prompt-port drift (TS → Py) | Port verbatim from quoted line ranges; parity fixture test asserts key anchors (role block, formatting rules, language clause, page-count clause) appear identically; delete the TS copies in the same change so there is only one source |
| Engine bug burns tokens unattended | Per-job page cap (structure page count already bounded); cancel endpoint; engine aborts after 3 consecutive page failures |
| Server restart mid-job | Journal → `interrupted` + incremental saves |
| Lost live-text UX | Accepted v1 trade-off (page-granularity); live streaming is a noted follow-up |

### Testing

Engine and manager are designed for fake-LLM injection: `llm_dispatch` is passed in, so
tests drive whole jobs with canned XML/pages in milliseconds — covering incremental
saves, stats, self-review guards, cancel, dedupe, parallel=2 ordering, journal
round-trip, and the 3-consecutive-failure abort. Frontend: build + manual checklist
(navigate away mid-run, parallel two repos, cancel, container restart mid-job).
