# Per-Model Wiki Cache Versions — Design

**Date:** 2026-06-05
**Status:** Approved

## Problem

DeepWiki saves each generated wiki to a single server-side cache file keyed only by
`{repo_type}_{owner}_{repo}_{language}` (`api/api.py:get_wiki_cache_path`). Regenerating
a wiki with a different LLM provider/model **overwrites** the previous output, so it is
impossible to keep and compare the outputs of different LLMs.

## Goal

Keep each provider/model's generated wiki saved separately, so the user can switch
between them via the existing model selector and compare outputs. No new comparison UI —
"presentation" stays the existing single-wiki view plus per-version rows on the
processed-projects page.

## Approved decisions

1. **Scope:** full wiki generations (not chat answers).
2. **Presentation:** save separately; switch via existing model selector; no side-by-side UI.
3. **Default load:** opening `/owner/repo` with no provider/model in the URL loads the
   **most recently generated** version. Explicit `?provider=&model=` loads that exact version.
4. **On model switch:** if a saved wiki exists for the newly selected provider/model, load
   it instantly; only generate on a miss. A separate **Regenerate** action forces regeneration.

## Design

### Cache file naming (backend, `api/api.py`)

New filename format, using `~` as separator. `~` is not in `_SAFE_CACHE_SEGMENT`
(`[A-Za-z0-9._-]`), so parsing is unambiguous and legacy files (no `~`) are distinguishable:

```
deepwiki_cache_{repo_type}_{owner}_{repo}_{language}~{provider}~{model}.json
e.g. deepwiki_cache_github_AsyncFuncAI_deepwiki-open_en~claude~claude-sonnet-4-6.json
```

- Provider/model are sanitized for filenames: any char outside `[A-Za-z0-9.-]` becomes `-`
  (e.g. `Qwen/Qwen3-32B` → `Qwen-Qwen3-32B`). The sanitized value round-trips: sanitizing
  it again yields the same string, so values parsed from filenames can be passed back as
  query params. The true (unsanitized) provider/model remain stored inside the JSON.
- **Legacy files** (no suffix) remain readable as a fallback version — zero migration.
- If provider or model is missing/empty after sanitization, fall back to the legacy filename.

### Endpoints (backend, `api/api.py`)

- **`POST /api/wiki_cache`** — body already carries provider/model; now writes to the
  version-specific path. Different models never overwrite each other.
- **`GET /api/wiki_cache`** — new optional `provider` & `model` query params:
  - both given → load that exact version, `null` on miss;
  - omitted → glob all versions for the repo (incl. legacy), return **newest by mtime**;
    corrupt files are skipped (try next-newest).
- **`DELETE /api/wiki_cache`** — new optional `provider` & `model` query params:
  - both given → delete that version only;
  - omitted → delete **all** versions for the repo (legacy "wipe this repo" behavior).

### Processed projects listing

- Filename parser extracted to a pure helper `parse_wiki_cache_filename()`: split the stem
  on `~` first → `[base, provider, model]`; parse base as today (handles `_` in repo names).
- `ProcessedProjectEntry` gains optional `provider`/`model`.
- The projects page shows one row per saved version with a `provider/model` badge. Row links
  carry `&provider=&model=` so clicking opens that exact version. Row delete removes just
  that version (legacy rows delete all files for the repo, with that called out in the
  confirm dialog).

### Frontend (`src/app/[owner]/[repo]/page.tsx`, `ModelSelectionModal.tsx`)

- The initial cache fetch passes `provider`/`model` only when both are set in state (seeded
  from URL params) → otherwise the backend returns the newest version.
- `confirmRefresh` gains a `forceRegenerate` flag:
  - **Apply (Submit)** in the model selector → no cache DELETE; state resets and the normal
    load flow runs → GET for the selected provider/model → instant load on hit, generation
    on miss.
  - **Regenerate** (new secondary button in the modal footer) → DELETEs that version's
    cache first (the DELETE already sends provider/model), then regenerates.
- The page already displays provider/model from cache metadata, so the user always sees
  which LLM produced what is on screen.

### Error handling & edge cases

- Unsanitizable/empty provider or model → legacy filename (current behavior).
- Corrupt version file → skipped in newest-wins selection, logged as today.
- Cache is still written once at generation end; no mid-generation changes.
- A version row clicked on the projects page passes the *sanitized* model name; on a cache
  hit (the normal case) the page replaces it with the true model from the JSON metadata.

### Testing

- Backend pytest (`tests/unit/test_wiki_cache_versions.py`): sanitization, path generation
  (incl. traversal guards and legacy fallback), save/read roundtrip per version,
  newest-wins, version-specific and delete-all DELETE, filename parsing (legacy, versioned,
  underscored repo names).
- Frontend: type-check/build; manual verification (generate with model A, switch to B →
  generates; switch back to A → instant load; projects page shows both rows with badges).

### Deployment note

This deployment (HK-hosted) rebuilds containers for code changes; both `api/` and `src/`
change here, so a full rebuild is needed to test live.
