# Implementation Plan — fixes from CODE_REVIEW_PLAN.md

> Companion to `CODE_REVIEW_PLAN.md`. Concrete edits, verification, and risk per fix.
> **Nothing implemented yet.** Phased so each batch can ship + be verified independently.
>
> **Build note:** docker-compose mounts only `~/.adalflow` and `./api/logs` — source is
> NOT mounted, so EVERY code change (Python or frontend) requires
> `docker compose up -d --build` to take effect. Python-only fixes can be hot-tested first
> via `docker cp <file> deepwiki-open-deepwiki-1:/app/<path>` before the persisting rebuild.
> Branch: `feat/local-vllm-provider`. Suggest one commit per finding.
>
> **⚠️ Independently re-reviewed (2026-06-03).** Corrections folded in below. Headlines:
> - **P1-4 was FLAWED** — the proposed base-dir default would break the core local-folder
>   feature (local repos use the user's raw path, never relocated under `~/.adalflow/repos`;
>   confirmed `data_pipeline.py:834-835`). **Redesigned below; risk raised to HIGH.**
> - **P3-11** cache key MUST include the request's file-filter set, not just `(repo, embedder)`.
> - **P1-7** must preserve the substring-matched error-classification branches.
> - **F0** is correct but its https→wss branch is untestable in-browser (see note).
> Verdicts: most items SOUND; P1-5, P3-12, P2-8/9, frontend batch confirmed safe as written.

---

## Phase 1 — Quick correctness (F0 + P0). Low risk, high value.

### F0. Fix WebSocket URL scheme conversion
- **File:** `src/app/[owner]/[repo]/page.tsx:551`
- **Change:** replace the always-truthy ternary with a single scheme regex:
  `const wsBaseUrl = serverBaseUrl.replace(/^http(s?):/i, 'ws$1:');`
- **Verify:** for `http://localhost:8001` → `ws://localhost:8001`. After rebuild, generate a page
  and confirm logs show the WS path used (not "HTTP fallback").
- **Note (verified):** `SERVER_BASE_URL` is NOT exposed to the browser — `next.config.ts` uses it
  server-side only (no `NEXT_PUBLIC_`). So in the browser the value is `undefined` and the WS target
  is always `ws://localhost:8001`; the `https→wss` branch is effectively dead in-browser and can't be
  exercised via env. If a remote/https deployment is intended, separately expose the base via
  `NEXT_PUBLIC_*`. The fix is still correct and necessary.
- **Risk:** negligible. **Rollback:** revert one line.

### P0-1. `count_tokens` boolean arg
- **Files:** `api/simple_chat.py:86`, `api/websocket_wiki.py:83`
- **Change:** `count_tokens(last_message.content, request.provider == "ollama")`
  → `count_tokens(last_message.content, is_ollama_embedder=(request.provider == "ollama"))`
- **Verify:** `docker exec ... python -c "from api.data_pipeline import count_tokens; print(count_tokens('hi'*10, is_ollama_embedder=True))"` returns an int without error; spot-check a request logs a sane token count.
- **Completeness (verified):** the only other call sites, `data_pipeline.py:347,380`, already pass a real
  `embedder_type` string correctly — no change needed there.
- **Risk:** negligible (estimate only).

### P0-2. Add `litellm` branch to `simple_chat.py`
- **File:** `api/simple_chat.py`
- **Change:**
  1. Import: `from api.litellm_client import LiteLLMClient` and add `LITELLM_API_KEY` to the `api.config` import.
  2. Add an init branch `elif request.provider == "litellm":` (mirror the `vllm` branch — `model = LiteLLMClient()`, same model_kwargs).
  3. Widen the execution + fallback conditionals from `in ("openai", "vllm")` to `in ("openai", "vllm", "litellm")`.
- **Verify:** `POST /chat/completions/stream` with `provider:"litellm"` (set `LITELLM_BASE_URL`) reaches LiteLLM, not Google. Check the log line.
- **Risk:** low; isolated new branch.

### P0-3. Guard `configs["embedder_vllm"]`
- **File:** `api/tools/embedder.py` (lines 26, 43)
- **Change:** for the vllm branch (minimal scope), `configs.get("embedder_vllm")` with an explicit
  `raise ValueError("embedder_vllm not configured in embedder.json")` if falsy. (Optionally apply the
  same pattern to the sibling branches — note them but keep this commit focused.)
- **Verify:** temporarily unset the key in a test config → clear error instead of bare `KeyError`.
- **Risk:** negligible.

**Phase 1 exit:** one rebuild, generate a wiki, confirm WS path active + no regressions.

---

## Phase 2 — Security (P1). Medium risk: validation can reject previously-accepted input.

### P1-4. Path traversal in `GET /local_repo/structure` — REDESIGNED (risk: HIGH)
- **File:** `api/api.py` (`get_local_repo_structure`, ~276–320)
- **Why the original was flawed (verified):** local repos are read at the user's *raw arbitrary
  path* — `page.tsx:188` reads `local_path` from the query, sent verbatim to
  `/local_repo/structure` (`page.tsx:1219`), and `data_pipeline.py:834-835` sets
  `save_repo_dir = repo_url_or_path` (NOT relocated under `~/.adalflow/repos`, unlike cloned remotes).
  A default base of `/root/.adalflow/repos` would therefore reject **every** legitimate local-folder
  wiki — the normal case, not an edge case.
- **Redesigned change (don't break the normal case):**
  1. `real = os.path.realpath(path)` and operate on `real`.
  2. `os.walk(real, followlinks=False)` (block symlink escape) + cap README read (`f.read(1_000_000)`).
  3. **Opt-in** lockdown: only if env `DEEPWIKI_LOCAL_REPO_BASE` is set, enforce
     `real` is under it; if unset, allow any real directory (preserves current behavior) but still
     with `followlinks=False` + size cap. This hardens against symlink/`..` tricks without breaking
     arbitrary legitimate local paths.
  4. (Optional, stronger) also reject obviously-sensitive roots (`/etc`, `/proc`, `/sys`) by prefix.
- **Verify:** a normal local path (e.g. `/root/.adalflow/repos/...` AND an arbitrary `/some/code/dir`)
  still works with no env set; with `DEEPWIKI_LOCAL_REPO_BASE` set, paths outside it 400; a symlink
  inside the tree pointing to `/etc` is not followed.
- **Risk:** HIGH if the base were mandatory (would brick local-folder wikis). The opt-in design avoids
  that; still smoke-test the local-folder flow after rebuild.
- **Rollback:** revert; behavior returns to fully permissive.

### P1-5. Cache path traversal via `owner`/`repo`
- **File:** `api/api.py` (`get_wiki_cache_path`, ~408–411; used by GET/POST/DELETE)
- **Change:** validate `owner`, `repo`, `language`, `repo_type` against `^[A-Za-z0-9._-]+$` (reject `/`, `..`);
  after building the path, assert `os.path.realpath(path).startswith(os.path.realpath(WIKI_CACHE_DIR) + os.sep)`.
- **Verify:** normal owner/repo still read/write; `owner=../../x` → 400.
- **Risk:** low (real owner/repo names already match the allow-list).

### P1-6. CORS allow-list
- **Files:** `api/api.py:29-30`, `api/simple_chat.py:46-47`
- **Change:** read `DEEPWIKI_ALLOWED_ORIGINS` (comma-separated) → list; default
  `["http://localhost:3000"]` (+ `SERVER_BASE_URL` if set). Keep `allow_credentials` only if an explicit list is used.
- **Verify:** UI on `localhost:3000` still works; a random Origin is not reflected.
- **Risk:** lower than it looks (verified) — the browser→API WebSocket isn't CORS-gated, and the HTTP
  fallback goes same-origin through the Next.js proxy (`/api/...`), so a wrong default is unlikely to
  brick the UI. Still keep `http://localhost:3000` in the default to be safe.

### P1-7. Stop echoing exception text to clients
- **Files:** `api/simple_chat.py:123,130,756,764`; `api/websocket_wiki.py:122,131`; `api/api.py:271,319,535`
- **Change:** `logger.error(..., exc_info=True)` server-side; return a generic message
  (e.g. "Error preparing retriever. Please retry.").
- **⚠️ Must preserve (verified):** several handlers branch on the exception *text*
  (`simple_chat.py:118` `"No valid documents with embeddings found"`, `:127`
  `"All embeddings should be of the same size"`, `:584` token-limit matches). Genericize ONLY the
  final user-facing string, AFTER those `if "..." in str(e)` classification checks run — don't replace
  `str(e)` before them or the friendly-error branches break.
- **Verify:** trigger a retriever error → client sees generic text; full trace in container logs;
  the "no embeddings"/"size mismatch"/token-limit friendly messages still fire.
- **Risk:** low if the classification order is respected.

---

## Phase 3 — Performance (P3). Higher risk: touches the hot generation path.

### P3-11. Cache the loaded RAG/embedding DB across a generation run (top win)
- **Files:** `api/rag.py` (`prepare_retriever`/`RAG`), `api/data_pipeline.py:893` (`LocalDB.load_state`)
- **Approach:** process-level cache holding the prepared retriever/transformed DB, so the ~20
  per-page requests reuse one deserialize instead of N. Add invalidation when the `.pkl` mtime changes
  (so a re-index is picked up).
- **⚠️ Cache key must include the file-filter set (verified):** `prepare_database`/`prepare_db_index`
  take `excluded_dirs/excluded_files/included_dirs/included_files` (`data_pipeline.py:745-746`, from the
  request). Two requests for the same repo with different filters produce different `transformed_docs`.
  Key on `(repo_url, embedder_type, frozenset(filters))` — NOT just `(repo_url, embedder_type)` — or you
  will serve a retriever built with the wrong filters.
- **Verify:** generate a multi-page wiki; logs show ONE "Restoring class from_dict …" instead of one per page;
  output unchanged; re-index (delete pkl + regenerate) still reflects new content.
- **Risk:** **highest in this plan** — staleness and memory. Gate behind a flag if unsure; test the
  re-index path explicitly. Consider a small LRU (size 1–2 repos).

### P3-12. Memoize embedder
- **Files:** `api/rag.py:191`, `api/data_pipeline.py:429`
- **Change:** wrap `get_embedder` resolution in `functools.lru_cache` keyed by `embedder_type`
  (clients are stateless/reusable).
- **Verify:** embedder constructed once per type; embeddings still correct.
- **Risk:** low; ensure no per-request state lives on the client.

---

## Phase 4 — Robustness (P2) + low-severity frontend

### P2-8. Bitbucket file-tree pagination (the real bug behind old #8)
- **File:** `src/app/[owner]/[repo]/page.tsx:1450-1459`
- **Change:** loop following Bitbucket's `next` cursor (response `.next`) until exhausted, accumulating `values`.
- **Verify:** a Bitbucket repo with >100 files yields a complete tree.
- **Risk:** low; only affects Bitbucket.

### P2-9. Surface XML structure parse errors
- **File:** `src/app/[owner]/[repo]/page.tsx:982-992`
- **Change:** on `parsererror`, if no pages were extracted, throw a clear error (set `error` state) instead of
  silently rendering an empty wiki; optionally add a real regex fallback.
- **Verify:** feed malformed structure XML → user sees an error, not a blank wiki.
- **Risk:** low.

### Frontend low-severity (batch)
- WS: register `onopen` once (remove dead duplicate at `page.tsx:560-565`); call `ws.close()` in the
  HTTP-fallback `catch` (~606) and guard late `onmessage`.
- Page-ID collision (`page.tsx:1015`): make the fallback id unique (check against existing / use a counter+uuid).
- (Optional) `React.memo` the Markdown component; add `AbortController` to the fallback fetch.
- **Risk:** low.

---

## Suggested order & checkpoints
1. **Phase 1** → rebuild → verify WS path + generate a wiki (fast, safe, visible win).
2. **Phase 2** → rebuild → verify UI still loads (CORS) + local-folder path still works (P1-4).
3. **Phase 3** → rebuild → verify single DB load + re-index correctness (most caution here).
4. **Phase 4** → rebuild → verify Bitbucket/large repos + malformed-structure handling.

Each phase = its own commit(s) on `feat/local-vllm-provider`; rebuild + smoke-test before the next.
Defer/drop old P2-#10 (retriever setup already fails fast).
