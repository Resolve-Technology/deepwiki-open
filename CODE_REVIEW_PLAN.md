# Bug & Optimization Plan — deepwiki-open

> Read-only review of the Python backend (`api/`) and Next.js frontend (`src/`),
> including the recently-added local vLLM provider and TSD/BRD prompt work.
> **Analysis only — no code changes have been made.**
>
> Status: **independently re-verified** by a second reviewer against the code
> (2026-06-03). Line numbers corrected; two findings demoted; one new high-impact
> frontend bug added (F0). Verdicts below: **CONFIRMED / PARTIAL / INCORRECT**.

---

## F0 — NEW, high impact: WebSocket URL never becomes `ws://` (CONFIRMED)
- **Where:** `src/app/[owner]/[repo]/page.tsx:551`
- **Detail:** `serverBaseUrl.replace(/^http/, 'ws') ? serverBaseUrl.replace(/^https/, 'wss') : ...`
  — the ternary condition is a `.replace()` result, which is always truthy, so it always
  takes the `/^https/ → wss` branch. For the default `http://localhost:8001`, `/^https/`
  doesn't match, leaving an invalid `http://…` WebSocket URL. The WS streaming path then
  always fails and falls back to HTTP in the common local config.
- **Impact:** the primary streaming path is effectively dead by default.
- **Fix direction:** convert scheme correctly (`https→wss`, else `http→ws`), e.g. a single
  regex on the leading scheme.

---

## P0 — Correctness bugs

### 1. `count_tokens` called with a boolean (CONFIRMED — impact is mild)
- **Where:** `api/simple_chat.py:86`, `api/websocket_wiki.py:83`
- **Detail:** `count_tokens(last_message.content, request.provider == "ollama")` passes a
  `bool` into the 2nd positional param `embedder_type` (signature
  `count_tokens(text, embedder_type=None, is_ollama_embedder=None)`, `data_pipeline.py:27`).
  `True/False == 'ollama'` is always False, so it always uses the default/OpenAI encoding.
- **Impact:** only affects the request-size *estimate* (threshold ~8000 tokens), not output
  correctness — milder than originally implied.
- **Fix direction:** pass `is_ollama_embedder=(request.provider == "ollama")` (keyword) — the
  back-compat shim at `data_pipeline.py:43-44` handles it. Verified to work.

### 2. `simple_chat.py` has no `litellm` branch (CONFIRMED)
- **Where:** provider branches `api/simple_chat.py:333-481` (corrected range)
- **Detail:** `websocket_wiki.py:511` handles `litellm`; the HTTP path does not, so a
  `litellm` request silently falls through to the Google default.
- **Fix direction:** add a `litellm` branch mirroring the `vllm`/`openai` one (init + stream + fallback).

### 3. Unchecked `configs["embedder_vllm"]` (CONFIRMED — low severity)
- **Where:** `api/tools/embedder.py:26` and `:43` (corrected)
- **Detail:** bracket access → `KeyError` if missing. Note: **every** embedder branch in
  this function uses the same unchecked access (lines 20/22/24/28…), so this isn't unique to vllm.
- **Fix direction:** `configs.get(...)` with a clear error, or validate embedder config at load.

---

## P1 — Security (weighted for internal/self-hosted)

### 4. Path traversal in `GET /local_repo/structure` (CONFIRMED — highest real risk)
- **Where:** `api/api.py:276` (gate `os.path.isdir` at :284, `os.walk` at :295, README `open()/read()` at :307-308)
- **Detail:** `path` from query, no base-dir restriction, `os.walk` follows symlinks.
  Scope is **directory enumeration of any host dir + reading any file named `README.md`**
  in it (slightly narrower than "arbitrary file read", still serious disclosure).
  Also: the README `f.read()` has **no size limit** (DoS/disclosure amplifier) — fold into this fix.
- **Fix direction:** `os.path.realpath` + require result under an allowed base
  (e.g. `/root/.adalflow/repos`), `followlinks=False`, and cap README read size.

### 5. Cache path traversal via `owner`/`repo` (CONFIRMED)
- **Where:** `api/api.py:408-411` (`get_wiki_cache_path`) used by GET :461, POST :486, DELETE :504-538
- **Detail:** `owner`/`repo` interpolated into the cache filename; `../` can escape
  `WIKI_CACHE_DIR`. DELETE only requires an auth code when `WIKI_AUTH_MODE` is on (optional).
- **Fix direction:** allow-list `[A-Za-z0-9._-]`, reject separators, assert resolved path stays in cache dir.

### 6. `CORS allow_origins=["*"]` + `allow_credentials=True` (CONFIRMED — partly browser-mitigated)
- **Where:** `api/api.py:29-30`, `api/simple_chat.py:46-47`
- **Note:** browsers reject credentialed wildcard CORS, so practical impact is partial, but
  still a misconfiguration.
- **Fix direction:** restrict to known origin(s) via env-configurable list.

### 7. Error messages echo internals to clients (CONFIRMED)
- **Where:** `api/simple_chat.py:123,130,756,764`; `api/websocket_wiki.py:122,131`; `api/api.py:271,319,535`
- **Fix direction:** log full error server-side (`exc_info=True`); return generic client message.

---

## P2 — Robustness

### 8. ~~Paginated repo-tree fetch missing `response.ok`~~ → INCORRECT; real bug is Bitbucket pagination
- **Verdict:** INCORRECT as written — GitLab (`page.tsx:1385/1390`) **does** check `response.ok`
  per page and throws; Bitbucket (`:1458`) also checks before parse.
- **Actual bug (CONFIRMED):** Bitbucket file tree is **not paginated at all**
  (`page.tsx:1450-1459`): a single `?per_page=100` request, no follow of the `next` cursor.
  Repos with >100 files get a silently truncated tree → incomplete wiki.
- **Fix direction:** follow Bitbucket's `next` pagination cursor.

### 9. XML parse continues after `parsererror` (CONFIRMED)
- **Where:** `page.tsx:982-992` (detects error, logs, "continue anyway" at :991; the
  "regex fallback" at :1011 only logs — there is no real fallback parse). Empty structure → no user error.
- **Fix direction:** on `parsererror`, throw/show an error or add a real regex fallback.

### 10. ~~Broad `except` swallowing validation errors~~ → MOSTLY INCORRECT at cited location
- **Verdict:** the retriever-**setup** paths (`simple_chat.py:117-130`, `websocket_wiki.py:114-133`)
  actually **fail fast** — they re-raise as `HTTPException` / send over the socket and `return`.
  The "silent continuation" premise is wrong there.
- **Where it IS true:** RAG **retrieval** (`simple_chat.py:233-239`) intentionally continues
  without context on error — arguably by design. Low priority; re-scope or drop.

---

## P3 — Performance

### 11. Embedding DB (`.pkl`) reloaded from disk every page (CONFIRMED — top perf win; location corrected)
- **Where:** `LocalDB.load_state(...)` at `api/data_pipeline.py:893` (NOT `rag.py`), reached
  per request via `RAG()` (`simple_chat.py:94`, `websocket_wiki.py:91`) → `prepare_retriever`
  → `prepare_db_index`. ~20 pages ⇒ ~20 full deserializes (matches observed logs).
- **Fix direction:** cache the loaded `RAG`/retriever per (repo, embedder) for a generation run.

### 12. Embedder re-instantiated per request/pipeline (CONFIRMED — locations corrected)
- **Where:** `api/rag.py:191` (per `RAG()`); pipeline site is `api/data_pipeline.py:429`
  (the plan's earlier `:347` was a `count_tokens` line — wrong).
- **Fix direction:** memoize the embedder instance.

### 13. Frontend render/abort hygiene (PARTIAL)
- **Where:** `src/components/Markdown.tsx:16` is a non-memoized FC → the visible page's
  Markdown re-renders on parent re-render (only the single `currentPageId` page is rendered,
  `page.tsx:2179`, not all pages). HTTP fallback (`page.tsx:610`) has no `AbortController`.
- **Fix direction:** `React.memo`/`useMemo` the rendered page; add `AbortController` to the fallback fetch.

---

## Frontend items (verified, low severity)
- **WebSocket `onopen` assigned twice** (`page.tsx:560-565` then overwritten `:578-584`; first is dead code) and **socket not closed on HTTP fallback** (`catch` at :606) → leak + possible late-`onmessage` race writing into `content` reset at :625. CONFIRMED, low.
- **Queue completion** (`page.tsx:1159`): authoritative path is the `.finally` at :1144-1147 (robust closure vars); the stale `pagesInProgress.size` only guards a redundant `setIsLoading(false)` — real "stuck loading" unlikely. PARTIAL, low.
- **Fallback page-ID collision** (`page.tsx:1015`): `getAttribute('id') || page-${pages.length+1}` can collide with an explicit `page-N`. CONFIRMED, minor.

---

## Suggested sequencing (revised)
1. **F0** (WS URL) — quick, fixes the dead streaming path; **and P0 #1–#3**.
2. **P1 #4–#6** — `/local_repo/structure` (#4, incl. README size cap) first if ever network-exposed.
3. **P3 #11** — biggest perf win; needs care in the generation flow.
4. **P2 #8 (Bitbucket pagination) + #9**, then the low-severity frontend items.
5. Drop/re-scope **#10**.

## Notes on the recent vLLM work
Wiring is consistent with the existing `openai`/`litellm` providers; the only gap is the
unchecked `configs["embedder_vllm"]` access (P0 #3, and shared by all embedder branches).
The two embedder-resolution fixes (`get_embedder_type` + the `configs` merge loop) are correct.
