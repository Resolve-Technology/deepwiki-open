# Completeness Report Durability — Design

**Date:** 2026-07-01
**Status:** Approved (design), pending implementation plan

## Problem

The completeness report (`<cache>.completeness.json` / `.md`) is written with a
plain non-atomic `open(..., "w")`, and one report's `.json` was observed to go
missing (only the `.md` survived), so `/api/wiki_completeness` returned null and
the report was effectively lost until manually rebuilt. The operator wants the
**latest** report to persist reliably — never silently disappear (history is
NOT required).

## Goal

Guarantee the latest completeness report for a generated wiki is always
retrievable as long as the wiki's cache exists:
1. Writes are atomic/durable (no truncated/partial/corrupt files).
2. If the report is missing at read time but the wikicache exists, it is
   rebuilt on the fly from the cached pages (it is a pure function of them).

## Non-goals (YAGNI)

- No history/versioning of past reports (operator chose "latest only").
- No change to what the report contains or how it's computed
  (`check_tsd_brd_completeness` / `render_markdown_report` unchanged).
- No new endpoint (extend the existing `/api/wiki_completeness` read path).

## Architecture

### 1. Atomic report writes (`api/wiki_generator.py`)

Replace the two plain writes in the best-effort completeness block with an
atomic helper: write to a temp file in the same directory, `flush`, then
`os.replace(tmp, dest)` (atomic rename on the same filesystem). A crash or
partial write can no longer leave a truncated/empty/corrupt `.json`/`.md` — the
destination is only ever the old file or the fully-written new one. Add a small
helper (e.g. `_atomic_write_text(path, text)`) used for both files. This stays
inside the existing best-effort `try/except` (never fails the job).

### 2. Self-heal on read (`api/completeness_io.py` + `api/api.py`)

The report is a pure function of the wikicache's `generated_pages`. Extend the
read path so a missing `.completeness.json` is transparently rebuilt from the
sibling wikicache:

- In `read_completeness_report(cache_path, fmt)` (or a thin wrapper the endpoint
  calls): if the requested report file is absent BUT the wikicache `cache_path`
  (`<...>.json`) exists, load it, run `check_tsd_brd_completeness` over
  `list(generated_pages.values())`, wrap with the same metadata
  (`repo`/`provider`/`model`/`generated_at`) the generator writes, **atomically
  write** the `.json` and `.md` (so it's healed on disk for next time), and
  return the requested format.
- If neither the report nor the wikicache exists → behave as today
  (json → null / md → 404).
- Rebuild is pure/cheap (no LLM, no network); any error during rebuild is caught
  and falls back to "not found" (never 500s).

This keeps `api/completeness_io.py` importing only stdlib + the pure checker
module (no `api.api`, log-safe), so it stays unit-testable. The metadata-wrapping
+ pages extraction is shared with the generator's write (factor a small helper
`build_report_payload(wikicache_dict)` in the pure `api/tsd_brd_completeness.py`
or `completeness_io.py` so the generator and the self-heal path produce identical
JSON).

### 3. Deletion paths (audit, no code change expected)

`force_regenerate` and the cache-delete path only `os.remove(cache_path)` (the
wikicache `X.json`), not the `.completeness.*` sidecars — confirmed. When
`X.json` is removed for a regen, the old report is briefly stale until the new
run rewrites it; that's acceptable. No deletion path needs changing; the spec
records this so a reviewer re-checks it.

## Data flow

Generation → atomic write of `.json`/`.md`. Read via `/api/wiki_completeness`:
if the `.json` is present, serve it; if missing but the wikicache exists, rebuild
(atomic write) and serve. So the latest report is bound to the wiki's existence —
it cannot be permanently lost while the wiki is cached.

## Error handling

- Atomic write failure (disk/perм) → caught by the existing best-effort wrapper;
  logged, job unaffected.
- Self-heal: malformed/absent wikicache → caught → "not found" (null/404), never
  raises.

## Testing

`tests/unit/test_completeness_io.py`:
1. Atomic write helper: writing produces a complete, parseable file; a temp file
   is not left behind on success.
2. Self-heal: given a temp dir with a wikicache `X.json` (containing
   `generated_pages`) but NO `X.completeness.json`, `read_completeness_report(X, "json")`
   returns the rebuilt report (with `summary.by_document`) AND writes
   `X.completeness.json`/`.md` to disk.
3. Self-heal md: same, `fmt="md"` returns the rendered markdown.
4. Neither report nor wikicache present → returns None (json) as today.
5. `build_report_payload(wikicache_dict)` produces the same shape the generator
   writes (metadata keys + `summary`/`pages`).

`tests/unit/test_tsd_brd_completeness.py`: if `build_report_payload` lands there,
a test that it wraps pages + metadata correctly.

E2e: delete a wiki's `.completeness.json` on the running server, GET
`/api/wiki_completeness?...&format=json`, confirm it returns `by_document` (healed)
and the file is recreated on disk.
