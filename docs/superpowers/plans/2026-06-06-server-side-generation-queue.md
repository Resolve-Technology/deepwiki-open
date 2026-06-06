# Server-Side Generation Queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wiki generation runs server-side in a job queue (2 parallel jobs) with incremental page saves — runs survive navigation/refresh, multiple repos generate in parallel, and the browser becomes a viewer with a progress panel.

**Architecture:** New backend modules: `api/wiki_prompts.py` (prompts ported verbatim from page.tsx / wikiRevision.ts), `api/llm_dispatch.py` (provider dispatch returning text+usage, injectable for tests), `api/wiki_generator.py` (the per-job engine with incremental `save_wiki_cache` after every page), `api/wiki_jobs.py` (JobManager: registry, asyncio queue, N workers, journal, REST endpoints). Frontend: enqueue from the config/refresh modals, a polling progress panel on the wiki page, a JobsPanel on the home page, and deletion of the in-browser orchestration.

**Tech Stack:** FastAPI + asyncio (backend), pytest with injected fake dispatch, Next.js/React (frontend).

**Spec:** `docs/superpowers/specs/2026-06-06-server-side-generation-queue-design.md`

**Conventions:**
- Run from `/home/ubuntu/deepwiki-open`. Backend tests: `PYTHONPATH=/home/ubuntu/deepwiki-open .venv/bin/python -m pytest tests/unit/<file> -v` (pytest.ini's testpaths is `test`; paths must be explicit).
- Frontend build: `docker run --rm -v /home/ubuntu/deepwiki-open:/app -w /app node:20-alpine sh -c "npm run build"`, then `git checkout -- yarn.lock package-lock.json 2>/dev/null; true`. Never claim an unrun build.
- Bump `src/version.ts` `APP_VERSION` (minor bump → 0.3.0) before the final image build.
- Line numbers below were checked on commit 6286d9b and WILL drift — always locate by the quoted search strings.

---

### Task 1: Port the prompts — `api/wiki_prompts.py`

**Files:**
- Create: `api/wiki_prompts.py`
- Create: `tests/unit/test_wiki_prompts.py`

The three generation prompts currently live as template literals in `src/app/[owner]/[repo]/page.tsx`; the self-review prompt lives in `src/utils/wikiRevision.ts`. Port them **verbatim** (same wording, same XML/markdown scaffolding) as Python functions. Do NOT redesign the prompts.

- [ ] **Step 1: Locate the sources** (search strings, not line numbers):
  - Structure prompt: in `determineWikiStructure`, the `requestBody` message content starting `"Analyze this GitHub repository"` (or similar) through the `IMPORTANT:` numbered list ending `"no markdown code block delimiters"` — includes the `${isComprehensiveView ? '18-30 pages total ...' : '4-6 pages'}` branch and the language clause.
  - Standard page prompt: in `generatePageContent`, the `standardPrompt` template — starts `"You are an expert technical writer and software architect."`, includes the `<details>` block instruction, mermaid rules (`NEVER use flowchart-style labels`...), and the language clause; ends `[CURRENT_FILE_CONTENT]: provided in the request context.`
  - Deep-dive prompt: the `deepDivePrompt` template in the same function (anchor: `isDeepDive`).
  - Self-review prompt: `buildSelfReviewPrompt` in `src/utils/wikiRevision.ts` (port as-is, including the `NO_CHANGES` contract).

- [ ] **Step 2: Write `api/wiki_prompts.py`** with these signatures (bodies are the verbatim ports with `${...}` → f-string substitutions):

```python
"""Generation prompts, ported verbatim from the frontend (single source now).

Any wording change here changes generation quality — edit deliberately.
"""
from typing import List

from api.api import WikiPage  # only for type hints on page-shaped dicts is also fine

LANGUAGE_NAMES = {
    "en": "English", "ja": "Japanese (日本語)", "zh": "Mandarin Chinese (中文)",
    "zh-tw": "Traditional Chinese (繁體中文)", "es": "Spanish (Español)",
    "kr": "Korean (한국어)", "vi": "Vietnamese (Tiếng Việt)",
    "pt-br": "Brazilian Portuguese (Português Brasileiro)", "fr": "Français (French)",
    "ru": "Русский (Russian)",
}

def language_clause(language: str) -> str: ...
def build_structure_prompt(file_tree: str, readme: str, owner: str, repo: str,
                           language: str, comprehensive: bool) -> str: ...
def build_page_prompt(page_title: str, file_paths: List[str], language: str,
                      deep_dive: bool) -> str: ...
def build_self_review_prompt(page_title: str, file_paths: List[str],
                             content: str, repo_url: str) -> str: ...
NO_CHANGES_TOKEN = "NO_CHANGES"
```

(If importing `WikiPage` from `api.api` creates a circular import once Task 4 wires endpoints, take plain `str`/`List[str]` args instead — the signatures above already avoid the model object.)

- [ ] **Step 3: Parity tests** — `tests/unit/test_wiki_prompts.py` asserts the load-bearing anchors survived the port (these strings exist in the TS sources today; copy them exactly):

```python
from api.wiki_prompts import (build_page_prompt, build_self_review_prompt,
                              build_structure_prompt, language_clause)

def test_structure_prompt_anchors():
    p = build_structure_prompt("file_tree", "readme", "o", "r", "zh-tw", True)
    assert "Return ONLY the valid XML structure" in p
    assert "18-30 pages total" in p
    assert "Traditional Chinese (繁體中文)" in p
    assert "<wiki_structure>" in p

def test_structure_prompt_concise_branch():
    p = build_structure_prompt("t", "r", "o", "r", "en", False)
    assert "4-6 pages" in p

def test_page_prompt_anchors():
    p = build_page_prompt("Core Features", ["a.py", "b.py"], "en", deep_dive=False)
    assert "expert technical writer and software architect" in p
    assert "<details>" in p
    assert "NEVER use flowchart-style labels" in p
    assert "[WIKI_PAGE_TOPIC]: Core Features" in p

def test_deep_dive_prompt_differs():
    assert build_page_prompt("P", ["x"], "en", True) != build_page_prompt("P", ["x"], "en", False)

def test_self_review_prompt_contract():
    p = build_self_review_prompt("P", ["x.py"], "page body", "https://g/o/r")
    assert "NO_CHANGES" in p
    assert "COMPLETE corrected page" in p
```

Adjust anchor strings to the *actual* TS text if any differ — the test must quote the real source.

- [ ] **Step 4: Run tests; commit** `feat: server-side generation prompts (ported from frontend)`.

---

### Task 2: Provider dispatch — `api/llm_dispatch.py`

**Files:**
- Create: `api/llm_dispatch.py`
- Create: `tests/unit/test_llm_dispatch.py`

A single async entry point the engine calls; injectable so engine tests never hit a network.

- [ ] **Step 1: Implement**

```python
"""One-call LLM dispatch for the generation engine.

Returns (text, usage) for a fully-assembled prompt. Mirrors the provider
branches of the websocket chat path for the providers this deployment uses;
unsupported providers raise so jobs fail fast with a clear error.
"""
import logging
from dataclasses import dataclass
from typing import Optional

from adalflow.core.types import ModelType

from api.anthropic_client import AnthropicClient
from api.config import get_model_config
from api.vllm_client import VLLMClient
from api.vllm_discovery import get_vllm_route

logger = logging.getLogger(__name__)


@dataclass
class LLMResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0


async def generate(provider: str, model: str, prompt: str) -> LLMResult:
    model_config = get_model_config(provider, model)["model_kwargs"]
    if provider == "claude":
        client = AnthropicClient()
        model_kwargs = {"model": model}
        for key in ("temperature", "top_p", "max_tokens", "thinking"):
            if key in model_config:
                model_kwargs[key] = model_config[key]
        api_kwargs = client.convert_inputs_to_api_kwargs(
            input=prompt, model_kwargs=model_kwargs, model_type=ModelType.LLM)
        text, usage = [], None
        stream = await client.acall(api_kwargs=api_kwargs, model_type=ModelType.LLM)
        async for chunk in stream:
            piece = chunk.choices[0].delta.content
            if piece:
                text.append(piece)
        # usage: read from the client's last logged usage — expose it properly:
        # extend AnthropicClient._stream_chunks to stash `self.last_usage` (see Step 2)
        u = getattr(client, "last_usage", None)
        return LLMResult("".join(text),
                         getattr(u, "input_tokens", 0) or 0,
                         getattr(u, "output_tokens", 0) or 0)
    if provider == "vllm":
        route = get_vllm_route(model)
        client = VLLMClient(base_url=route) if route else VLLMClient()
        model_kwargs = {"model": model, "stream": True}
        for key in ("temperature", "top_p"):
            if key in model_config:
                model_kwargs[key] = model_config[key]
        api_kwargs = client.convert_inputs_to_api_kwargs(
            input=prompt, model_kwargs=model_kwargs, model_type=ModelType.LLM)
        text = []
        stream = await client.acall(api_kwargs=api_kwargs, model_type=ModelType.LLM)
        async for chunk in stream:
            choices = getattr(chunk, "choices", [])
            if choices:
                piece = getattr(choices[0].delta, "content", None)
                if piece:
                    text.append(piece)
        u = getattr(client, "last_usage", None)
        return LLMResult("".join(text),
                         getattr(u, "prompt_tokens", 0) or 0,
                         getattr(u, "completion_tokens", 0) or 0)
    raise ValueError(f"Server-side generation does not support provider {provider!r} yet")
```

- [ ] **Step 2: Expose `last_usage` on both clients** — in `api/anthropic_client.py`'s `_stream_chunks`, after `final = await stream.get_final_message()`, add `self.last_usage = getattr(final, "usage", None)` (and `self.last_usage = None` in `__init__`). In `api/vllm_client.py`'s `_stream_with_usage_logging`, set `self.last_usage = usage` in the `finally` block (init it `None` in `__init__`). These are additive; the websocket paths ignore them.

- [ ] **Step 3: Tests** — unit-test the claude/vllm branches with monkeypatched client classes (fake `convert_inputs_to_api_kwargs` + async generator `acall` + `last_usage`), asserting text assembly and usage extraction; assert unsupported provider raises `ValueError`.

- [ ] **Step 4: Run tests; full existing suite still green; commit** `feat: one-call LLM dispatch with usage for server-side generation`.

---

### Task 3: The job engine — `api/wiki_generator.py`

**Files:**
- Create: `api/wiki_generator.py`
- Create: `tests/unit/test_wiki_generator.py`

- [ ] **Step 1: Job dataclasses** (shared with Task 4 — define here, import there):

```python
import asyncio
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

@dataclass
class PhaseStats:
    input_tokens: int = 0
    output_tokens: int = 0
    ms: float = 0.0
    def as_dict(self) -> Dict[str, int]:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "seconds": round(self.ms / 1000)}

@dataclass
class JobProgress:
    phase: str = "queued"        # cloning|embedding|structure|pages|saving|done
    pages_total: int = 0
    pages_done: int = 0
    current_page_title: str = ""
```

- [ ] **Step 2: The engine.** `run_generation(job, dispatch, on_progress)` where `dispatch` is `Callable[[str, str, str], Awaitable[LLMResult]]` (provider, model, prompt) — production passes `llm_dispatch.generate`, tests pass fakes. Responsibilities, in order:
  1. If `job.force_regenerate`: delete the target cache file (reuse `get_wiki_cache_path` + `os.remove`, ignore missing).
  2. `prepare_retriever` via a per-job `RAG(provider=..., model=...)` instance (`rag.prepare_retriever(repo_url, type, token, excluded..., included...)`) — run in `asyncio.to_thread`; progress phase `embedding`.
  3. Build the structure prompt (`wiki_prompts.build_structure_prompt`) using the file tree + readme. Get them the way the frontend's `fetchRepositoryStructure` does — port that lookup: for local repos call the existing `/local_repo/structure` logic (factor its directory-walk into a reusable function `read_local_repo_structure(path)` in `api/api.py` and import it); for remote repos the clone already exists at the standard path — walk it with the same exclusion rules. Phase `structure`.
  4. Call `dispatch` for the structure; strip ```/```xml fences; regex `<wiki_structure>[\s\S]*?</wiki_structure>`; on no-match retry up to 3 attempts (port the loop semantics from `determineWikiStructure`); parse with `xml.etree`, build the same `wiki_structure` dict shape the frontend caches today (`id,title,description,pages[],sections[],rootSections[]`) — port `parseXmlToStructure` faithfully (it lives in `determineWikiStructure` after the DOMParser section).
  5. Per page (sequential): phase `pages`, update `current_page_title`; RAG-retrieve context with the page's rag query; assemble the final prompt the way `websocket_wiki.py` does (system prompt is already inside the built page prompt; context block formatting — factor `format_context_text(retrieved_documents)` out of `websocket_wiki.py`'s grouping code into a module-level function and reuse it; respect `prompt_fit` budget helpers the same way). Deep-dive pages inject the file content (port the `filePath` semantics: read the file from the clone). Call `dispatch`; strip markdown fences (leading ```markdown / trailing ``` pair only — same rule as `parseRevisedContent`).
  6. Self-review (when `job.self_review`): build the review prompt, dispatch, apply the same guards as `wikiRevision.parseRevisedContent` — port that function as `parse_revised_content(original, response)` into `wiki_prompts.py` (NO_CHANGES, `Error:` prefix, <30% length, identical → unchanged).
  7. **Incremental save after every page**: call `save_wiki_cache(WikiCacheRequest(...))` with all pages completed so far + current stats. (Build the `WikiCacheRequest` from the job; `self_reviewed=job.self_review`, `stats={...as_dict}`.)
  8. Failure policy: a page failure stores `content="Error generating content: ..."` like the frontend does and continues; **3 consecutive failures abort the job** with status `failed`.
  9. Cancellation: `await asyncio.sleep(0)` + check `job.cancel_requested` between every dispatch call; raise `JobCancelled` → manager marks `cancelled` (pages saved so far remain).
  10. Stats: wrap each dispatch with time + add `LLMResult` tokens into `generation`/`review` `PhaseStats`.

- [ ] **Step 3: Tests with a fake dispatch** (no network, milliseconds):
  - canned structure XML with 3 pages → engine produces 3 pages, cache file written incrementally (assert cache exists and has 1 page after first fake page completes — drive via a dispatch fake that counts calls), stats tokens summed, `self_reviewed` flag set.
  - self-review fake returning `NO_CHANGES` → content unchanged; returning a rewrite → changed; returning garbage shorter than 30% → unchanged.
  - structure XML invalid 3× → job fails with clear error.
  - 3 consecutive page failures → abort; 2 failures + success → continues.
  - cancel_requested mid-run → `JobCancelled`, partial cache present.
  - Use the existing `cache_dir` monkeypatch fixture pattern from `test_wiki_cache_versions.py`; stub `prepare_retriever`/file-tree/RAG retrieval with monkeypatched fakes.

- [ ] **Step 4: Run tests; commit** `feat: server-side wiki generation engine with incremental saves`.

---

### Task 4: Queue manager + endpoints — `api/wiki_jobs.py`

**Files:**
- Create: `api/wiki_jobs.py`
- Modify: `api/api.py` (mount router / endpoints + startup hook)
- Create: `tests/unit/test_wiki_jobs.py`

- [ ] **Step 1: JobManager**

```python
WIKI_JOBS_CONCURRENCY = int(os.getenv("WIKI_JOBS_CONCURRENCY", "2"))
QUEUE_CAP = 20
FINISHED_RETENTION = 50
JOURNAL_PATH = os.path.join(get_adalflow_default_root_path(), "wikicache", "wiki_jobs.json")

@dataclass
class WikiJob:
    id: str                      # uuid4 hex
    repo: RepoInfo               # token kept ONLY in memory
    language: str
    provider: str
    model: str
    comprehensive: bool = True
    self_review: bool = True
    force_regenerate: bool = False
    excluded_dirs: Optional[str] = None
    excluded_files: Optional[str] = None
    included_dirs: Optional[str] = None
    included_files: Optional[str] = None
    status: str = "queued"
    progress: JobProgress = field(default_factory=JobProgress)
    stats: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    cancel_requested: bool = False
    created_at: float = 0.0
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    def key(self):  # dedupe identity
        return (self.repo.owner, self.repo.repo, self.repo.type,
                self.language, self.provider, self.model)
    def to_public_dict(self):  # NO token, timestamps as ISO
        ...
```

Manager: `submit(job)` (dedupe by `key()` over queued+running → raise `DuplicateJob`; cap → `QueueFull`), `get(id)`, `list()`, `cancel(id)` (sets `cancel_requested`; if still queued, mark cancelled immediately), `start()` (spawn `WIKI_JOBS_CONCURRENCY` worker tasks on the running loop), `_worker()` loop pulling from `asyncio.Queue`, running `wiki_generator.run_generation(job, llm_dispatch.generate, ...)`, mapping exceptions: `JobCancelled` → cancelled, anything else → failed with `error=str(e)`. Journal (`_persist()`): write all jobs' `to_public_dict()` after every status/progress change (atomic temp-file rename). On import/startup: read journal, mark any `queued|running` as `interrupted`, keep last `FINISHED_RETENTION`.

- [ ] **Step 2: Endpoints in api/api.py** (after the wiki_cache endpoints; same `WIKI_AUTH_MODE` check as DELETE for POST/cancel):

```python
class WikiJobRequest(BaseModel):
    repo: RepoInfo
    language: str = "en"
    provider: str
    model: str
    comprehensive: bool = True
    self_review: bool = True
    force_regenerate: bool = False
    excluded_dirs: Optional[str] = None
    excluded_files: Optional[str] = None
    included_dirs: Optional[str] = None
    included_files: Optional[str] = None
    authorization_code: Optional[str] = None

@app.post("/api/wiki_jobs")            # -> {job_id} | 409 dup | 429 full | 401 auth
@app.get("/api/wiki_jobs")             # -> {jobs: [public dicts]}
@app.get("/api/wiki_jobs/{job_id}")    # -> public dict | 404
@app.post("/api/wiki_jobs/{job_id}/cancel")  # auth-gated -> {status}
```

Start workers from a FastAPI startup hook (`@app.on_event("startup")` or lifespan — match the file's existing style). Add the Next.js rewrite for `/api/wiki_jobs/:path*` AND `/api/wiki_jobs` in `next.config.ts` (both forms, like wiki_cache has).

- [ ] **Step 3: Tests** — manager with a stubbed `run_generation` (instant fakes): submit → done lifecycle; dedupe 409 semantics (`DuplicateJob`); parallel=2 (submit 3 slow fakes, assert 2 run concurrently via an asyncio.Event handshake, third waits); cancel queued + cancel running; journal round-trip (restart marks interrupted, token never serialized — assert journal text lacks the token string); failure mapping.

- [ ] **Step 4: Run tests + whole backend suite; commit** `feat: wiki generation job queue with parallel workers and REST endpoints`.

---

### Task 5: Frontend — enqueue + progress panel + delete old orchestration

**Files:**
- Modify: `src/app/page.tsx` (home submit → POST job), `src/components/ConfigurationModal.tsx` (no change beyond what exists — submit handler lives in page.tsx)
- Modify: `src/app/[owner]/[repo]/page.tsx` (major)
- Modify: `src/components/ModelSelectionModal.tsx` (Submit/Regenerate → enqueue callbacks; wording)
- Modify: `next.config.ts` (rewrites — if not done in Task 4)

- [ ] **Step 1: Home page submit** — in `handleFormSubmit` (search `params.append('comprehensive'`), FIRST `POST /api/wiki_jobs` with the form's repo/provider/model/language/comprehensive/self_review (+ token, filters, `authorization_code`), then navigate exactly as today (keep all existing URL params so the wiki page knows which version to watch). On 409 (already running) just navigate — the page will attach to the running job. On other errors, show the existing error path.

- [ ] **Step 2: Wiki page becomes viewer + poller.** In `src/app/[owner]/[repo]/page.tsx`:
  (a) Add job state: `const [activeJob, setActiveJob] = useState<WikiJobStatus | null>(null);` and a polling effect: when mounted (and after any enqueue), `GET /api/wiki_jobs` and find a job matching (owner, repo, type, language, provider, model) with status queued/running; poll `GET /api/wiki_jobs/{id}` every 3s while active. On `pages_done` change → re-run the cache fetch (reuse the existing `loadData` cache-read block factored into a callable `refreshFromCache()`; pages render incrementally). On `done` → final refresh + clear job. On `failed/cancelled/interrupted` → surface `job.error` with a Retry (re-enqueue) button.
  (b) `confirmRefresh` becomes an enqueue: POST `/api/wiki_jobs` with `force_regenerate` set for the Regenerate path and false for Submit; reset view state; start polling. The cache DELETE call moves server-side (engine does it) — remove the frontend DELETE block.
  (c) **Delete**: `determineWikiStructure`, `generatePageContent`, `fetchRepositoryStructure`'s generation trigger (keep whatever feeds the viewer if reused elsewhere — check call sites), the self-review block, `runStatsRef`, the saveCache effect, `addTokensToRequestBody`'s now-unused generation wiring (keep it for Ask if shared — check). Keep: cache loading, version switching, export, Ask, review modal, mermaid retry + `originalMarkdown`.
  (d) Progress panel UI (replaces the current `pagesInProgress` sidebar list): phase label, `pages_done/pages_total`, current page title, elapsed time, cancel button (auth code passed like refresh does today).

- [ ] **Step 3: Build + fix the inevitable dead-import fallout. Commit** `feat: wiki page enqueues server-side generation and polls progress`.

> NOTE for the implementer: this is the riskiest task — page.tsx loses ~800 lines. Work in small compiles; the build gate is the safety net. Anything ambiguous about what still consumes a function you're deleting: grep its call sites first, ask if genuinely unclear.

---

### Task 6: Home page JobsPanel

**Files:**
- Create: `src/components/JobsPanel.tsx`
- Modify: `src/app/page.tsx`

- [ ] **Step 1:** `JobsPanel` — polls `GET /api/wiki_jobs` every 5s while any job is queued/running (back off to 30s otherwise); renders each non-finished job as a row: `owner/repo · provider/model · phase · pages x/y` with a progress bar and a cancel button; finished jobs from the last hour shown collapsed with status badges. Links each row to the wiki page URL with the job's provider/model params.
- [ ] **Step 2:** Mount it on the home page above `ProcessedProjects` (search `<ProcessedProjects`); hidden when there are no jobs at all.
- [ ] **Step 3:** Build; commit `feat: home page jobs panel for queued/running generations`.

---

### Task 7: Full verification + deploy

- [ ] **Step 1:** Whole backend suite (`tests/unit` selected files + legacy `test/`) green; frontend build green.
- [ ] **Step 2:** Bump `APP_VERSION` to `0.3.0`; `docker compose build`; recreate `deepwiki-staging` (ports 3001/8002, `PUBLIC_API_PORT=8002`, `~/.adalflow` mount).
- [ ] **Step 3: Manual checklist (the headline behaviors):**
  1. Enqueue a wiki from the home page → **navigate to the homepage mid-run** → wiki keeps generating (watch `docker logs`); return to the wiki page → progress panel re-attaches; pages appeared while you were away.
  2. Hard-refresh mid-run → same.
  3. Enqueue TWO different repos → both progress in parallel in the JobsPanel; backend logs show interleaved dispatches.
  4. Cancel a running job → status cancelled; pages generated so far are in the cache and render.
  5. `docker restart deepwiki-staging` mid-job → job shows `interrupted`; partial wiki is viewable; re-enqueue completes it (full regeneration — resume is a known follow-up).
  6. Duplicate submit of a running job → attaches to the existing job (409 path).
  7. Stats on the home card after a queued run match the job's final stats; self-review on/off respected; `force_regenerate` deletes only the target version.
  8. Ask + Model Review + apply-review still work unchanged.

---

### Explicit non-goals / follow-ups (do NOT build now)
- Resume-from-page-N after interruption.
- Live token streaming of the current page to a watching tab.
- Moving Ask / review flows into the queue.
- Multi-user fairness or per-provider concurrency limits.
