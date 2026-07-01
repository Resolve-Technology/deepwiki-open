# claude_api Provider + Completeness Report Durability — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** (A) Add a `claude_api` provider that generates with `claude-sonnet-5` via a standard Anthropic API key, and (B) make the completeness report durable — atomic writes + rebuild-from-cache on read so the latest report never disappears.

**Architecture:** (A) `AnthropicClient` gains an explicit api-key auth mode (x-api-key, no oauth beta); a new `claude_api` provider + a dispatch branch pass the key. (B) A shared pure `build_report_payload` builds the report dict; report writes go through an atomic `atomic_write_text`; `read_completeness_report` self-heals a missing report from the sibling wikicache.

**Tech Stack:** Python 3.12 / FastAPI, pytest. Tests: `.venv/bin/python -m pytest`.

## Global Constraints

- **A:** api-key mode is chosen ONLY by the explicit `api_key` constructor arg — NOT by auto-detecting `ANTHROPIC_API_KEY` from env (auto-detect would hijack the OAuth `claude` provider). The dispatch reads `ANTHROPIC_API_KEY` and passes it explicitly for `claude_api`.
- **A:** The existing `claude` (OAuth) and `claude_cli` providers are unchanged.
- **B:** No report history — latest only. Report content/computation unchanged.
- **B:** `api/completeness_io.py` must stay import-light (stdlib + `api.tsd_brd_completeness` only; no `api.api`), so it remains unit-testable in this sandbox.
- The API key lives ONLY in the server `.env` (gitignored); never committed or logged.
- Backend tests: `.venv/bin/python -m pytest <file> -q` (system python/pytest absent). Do not import `api.api` in a unit test.

---

## Part A — `claude_api` provider

### Task A1: API-key auth mode in `AnthropicClient`

**Files:**
- Modify: `api/anthropic_client.py` (`__init__` ~120; add `_auth_mode`/`_resolve_api_key`; `init_async_client` ~148-156)
- Test: `tests/unit/test_anthropic_client.py`

**Interfaces:**
- Produces: `AnthropicClient(auth_token=None, api_key=None, base_url=None)`; `_auth_mode() -> "api_key" | "oauth"` (api_key iff `self._api_key` is truthy); api-key mode builds `anthropic.AsyncAnthropic(api_key=..., base_url=..., max_retries=3)` with NO `anthropic-beta` header.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/unit/test_anthropic_client.py
import api.anthropic_client as ac_mod


class _FakeAsync:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def test_api_key_mode_uses_x_api_key_no_oauth_beta(monkeypatch):
    monkeypatch.setattr(ac_mod.anthropic, "AsyncAnthropic", _FakeAsync)
    c = ac_mod.AnthropicClient(api_key="sk-ant-api03-xyz")
    assert c._auth_mode() == "api_key"
    client = c.init_async_client()
    assert client.kwargs.get("api_key") == "sk-ant-api03-xyz"
    assert "auth_token" not in client.kwargs
    assert "default_headers" not in client.kwargs  # no oauth beta in api-key mode


def test_oauth_mode_uses_auth_token_and_beta(monkeypatch):
    monkeypatch.setattr(ac_mod.anthropic, "AsyncAnthropic", _FakeAsync)
    c = ac_mod.AnthropicClient(auth_token="sk-ant-oat01-xyz")
    assert c._auth_mode() == "oauth"
    client = c.init_async_client()
    assert client.kwargs.get("auth_token") == "sk-ant-oat01-xyz"
    assert client.kwargs["default_headers"]["anthropic-beta"] == ac_mod.ANTHROPIC_OAUTH_BETA


def test_env_anthropic_api_key_does_not_hijack_oauth(monkeypatch):
    # A plain AnthropicClient() (the OAuth `claude` provider) must stay OAuth
    # even if ANTHROPIC_API_KEY is set in the environment.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-envkey")
    c = ac_mod.AnthropicClient()
    assert c._auth_mode() == "oauth"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_anthropic_client.py -k "api_key_mode or oauth_mode or hijack" -q`
Expected: FAIL — `AnthropicClient` has no `api_key` param / no `_auth_mode`.

- [ ] **Step 3: Implement the api-key mode**

In `api/anthropic_client.py`, change `__init__` to accept `api_key` and add the helpers:

```python
    def __init__(self, auth_token: Optional[str] = None,
                 api_key: Optional[str] = None,
                 base_url: Optional[str] = None):
        self._auth_token = auth_token
        self._api_key = api_key
        self._base_url = base_url
        self._async_client: Optional[anthropic.AsyncAnthropic] = None
        self.last_usage = None

    def _auth_mode(self) -> str:
        """'api_key' when constructed with a standard API key, else 'oauth'.
        Keyed only on the explicit arg — never on env — so a global
        ANTHROPIC_API_KEY can't hijack the OAuth `claude` provider."""
        return "api_key" if self._api_key else "oauth"

    def _resolve_api_key(self) -> str:
        if not self._api_key:
            raise ValueError("ANTHROPIC_API_KEY is not set for the claude_api provider.")
        return self._api_key
```

(Keep the existing `_resolve_token` and `_resolve_base_url` as they are.) Replace `init_async_client`:

```python
    def init_async_client(self) -> anthropic.AsyncAnthropic:
        if self._async_client is None:
            if self._auth_mode() == "api_key":
                self._async_client = anthropic.AsyncAnthropic(
                    api_key=self._resolve_api_key(),
                    base_url=self._resolve_base_url(),
                    max_retries=3,
                )
            else:
                self._async_client = anthropic.AsyncAnthropic(
                    auth_token=self._resolve_token(),
                    base_url=self._resolve_base_url(),
                    default_headers={"anthropic-beta": ANTHROPIC_OAUTH_BETA},
                    max_retries=3,
                )
        return self._async_client
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_anthropic_client.py -q`
Expected: PASS (all — 3 new + pre-existing).

- [ ] **Step 5: Commit**

```bash
git add api/anthropic_client.py tests/unit/test_anthropic_client.py
git commit -m "feat: AnthropicClient api-key auth mode (x-api-key, no oauth beta)"
```

---

### Task A2: Register the `claude_api` provider

**Files:**
- Modify: `api/config/generator.json`
- Test: `tests/unit/test_claude_api_provider_config.py`

**Interfaces:**
- Produces: `generator.json` `providers.claude_api` with models `claude-sonnet-5`, `claude-opus-4-8`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_claude_api_provider_config.py
import json
from pathlib import Path


def _providers():
    p = Path(__file__).resolve().parents[2] / "api" / "config" / "generator.json"
    return json.loads(p.read_text())["providers"]


def test_claude_api_provider_registered():
    prov = _providers()
    assert "claude_api" in prov
    models = prov["claude_api"]["models"]
    assert "claude-sonnet-5" in models
    assert "claude-opus-4-8" in models
    assert prov["claude_api"]["default_model"] == "claude-sonnet-5"


def test_claude_api_does_not_disturb_claude():
    prov = _providers()
    # existing OAuth provider still present and unchanged in shape
    assert "claude-haiku-4-5-20251001" in prov["claude"]["models"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_claude_api_provider_config.py -q`
Expected: FAIL — `claude_api` not in providers.

- [ ] **Step 3: Add the provider to `generator.json`**

In `api/config/generator.json`, add a `"claude_api"` entry to `"providers"` (alongside `"claude"`), verbatim:

```json
    "claude_api": {
      "client_class": "ClaudeClient",
      "default_model": "claude-sonnet-5",
      "supportsCustomModel": true,
      "models": {
        "claude-sonnet-5": { "temperature": 0.3, "max_tokens": 64000 },
        "claude-opus-4-8": { "max_tokens": 100000, "thinking": "adaptive" }
      }
    },
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_claude_api_provider_config.py -q`
Expected: PASS. Also `.venv/bin/python -c "import json; json.load(open('api/config/generator.json')); print('json OK')"` → `json OK`.

- [ ] **Step 5: Commit**

```bash
git add api/config/generator.json tests/unit/test_claude_api_provider_config.py
git commit -m "feat: register claude_api provider (claude-sonnet-5, opus-4-8)"
```

---

### Task A3: Dispatch `claude_api` through the API-key client

**Files:**
- Modify: `api/llm_dispatch.py` (the `if provider == "claude":` branch ~32; ensure `os` imported)
- Test: `tests/unit/test_llm_dispatch.py`

**Interfaces:**
- Consumes: `AnthropicClient(api_key=...)` (Task A1).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/test_llm_dispatch.py
import api.llm_dispatch as dispatch


def test_claude_api_builds_api_key_client(monkeypatch):
    captured = {}

    class _FakeClient:
        def __init__(self, api_key=None):
            captured["api_key"] = api_key
        def convert_inputs_to_api_kwargs(self, **kw):
            return {}
        async def acall(self, **kw):
            async def _gen():
                if False:
                    yield None
            return _gen()
        last_usage = type("U", (), {"input_tokens": 0, "output_tokens": 0})()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test")
    monkeypatch.setattr(dispatch, "AnthropicClient", _FakeClient)
    monkeypatch.setattr(dispatch, "get_model_config",
                        lambda p, m: {"model_kwargs": {"max_tokens": 100}})
    import asyncio
    asyncio.run(dispatch.generate("claude_api", "claude-sonnet-5", "hi"))
    assert captured["api_key"] == "sk-ant-api03-test"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_llm_dispatch.py -k claude_api -q`
Expected: FAIL — `claude_api` unsupported (raises ValueError) / no api-key client built.

- [ ] **Step 3: Extend the claude branch**

In `api/llm_dispatch.py`, add `import os` to the top import block (it is NOT currently imported — the stdlib imports are `logging` and `from dataclasses import dataclass`; add `import os` above `import logging`). Then change the branch head from:

```python
    if provider == "claude":
        logger.info(f"Dispatching to Claude (native Anthropic SDK) model: {model}")
        client = AnthropicClient()
```

to:

```python
    if provider in ("claude", "claude_api"):
        if provider == "claude_api":
            key = os.getenv("ANTHROPIC_API_KEY")
            if not key:
                raise ValueError(
                    "ANTHROPIC_API_KEY is not set; the claude_api provider needs it.")
            logger.info(f"Dispatching to Claude (native SDK, API key) model: {model}")
            client = AnthropicClient(api_key=key)
        else:
            logger.info(f"Dispatching to Claude (native Anthropic SDK) model: {model}")
            client = AnthropicClient()
```

(The rest of the branch — `model_kwargs`, streaming, usage return — is unchanged.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_llm_dispatch.py -q`
Expected: PASS (all).

Run: `.venv/bin/python -c "import ast; ast.parse(open('api/llm_dispatch.py').read()); print('syntax OK')"`
Expected: `syntax OK`

- [ ] **Step 5: Commit**

```bash
git add api/llm_dispatch.py tests/unit/test_llm_dispatch.py
git commit -m "feat: route claude_api provider through AnthropicClient api-key mode"
```

---

## Part B — Completeness report durability

### Task B1: Shared `build_report_payload` helper

**Files:**
- Modify: `api/tsd_brd_completeness.py` (add `build_report_payload`)
- Test: `tests/unit/test_tsd_brd_completeness.py`

**Interfaces:**
- Consumes: `check_tsd_brd_completeness` (existing).
- Produces: `build_report_payload(pages: list, repo: str, provider, model, generated_at) -> dict` = `{"repo", "provider", "model", "generated_at", **check_tsd_brd_completeness(pages)}`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/test_tsd_brd_completeness.py
from api.tsd_brd_completeness import build_report_payload


def test_build_report_payload_wraps_metadata_and_summary():
    pages = [{"id": "page-tsd-a", "title": "A", "content": "## a (Alpha)\nreal\n"}]
    payload = build_report_payload(pages, "poc/x", "claude", "haiku", "2026-07-01T00:00:00Z")
    assert payload["repo"] == "poc/x"
    assert payload["provider"] == "claude"
    assert payload["model"] == "haiku"
    assert payload["generated_at"] == "2026-07-01T00:00:00Z"
    assert "summary" in payload and "pages" in payload
    assert "by_document" in payload["summary"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_tsd_brd_completeness.py -k build_report_payload -q`
Expected: FAIL — `cannot import name 'build_report_payload'`.

- [ ] **Step 3: Implement**

Add to `api/tsd_brd_completeness.py` (after `check_tsd_brd_completeness`):

```python
def build_report_payload(pages: list, repo: str, provider, model,
                         generated_at) -> dict:
    """Full completeness report payload (metadata + summary/pages) — the exact
    shape written to <cache>.completeness.json, shared by the generator and the
    read-time self-heal path."""
    return {"repo": repo, "provider": provider, "model": model,
            "generated_at": generated_at,
            **check_tsd_brd_completeness(pages)}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_tsd_brd_completeness.py -q`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add api/tsd_brd_completeness.py tests/unit/test_tsd_brd_completeness.py
git commit -m "feat: build_report_payload shared completeness-report builder"
```

---

### Task B2: Atomic writes + read-time self-heal (`api/completeness_io.py`)

**Files:**
- Modify: `api/completeness_io.py`
- Test: `tests/unit/test_completeness_io.py`

**Interfaces:**
- Consumes: `completeness_report_paths`, `build_report_payload`, `render_markdown_report` from `api.tsd_brd_completeness` (B1 + existing).
- Produces: `atomic_write_text(path: str, text: str) -> None`; `read_completeness_report(cache_path, fmt)` now rebuilds+persists a missing report from the sibling wikicache.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/unit/test_completeness_io.py
import os
from api.completeness_io import atomic_write_text


def test_atomic_write_creates_complete_file_no_tmp_left(tmp_path):
    dest = tmp_path / "out.json"
    atomic_write_text(str(dest), '{"a": 1}')
    assert dest.read_text() == '{"a": 1}'
    leftovers = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def _wikicache(tmp_path):
    cache = tmp_path / "deepwiki_cache_x.json"
    cache.write_text(json.dumps({
        "repo": {"owner": "poc", "repo": "x"}, "provider": "claude", "model": "haiku",
        "generated_at": "2026-07-01T00:00:00Z",
        "generated_pages": {"page-tsd-a": {"id": "page-tsd-a", "title": "A",
                                           "content": "## a (Alpha)\nreal\n"}},
    }), encoding="utf-8")
    return str(cache)


def test_self_heal_rebuilds_missing_json_from_wikicache(tmp_path):
    cache = _wikicache(tmp_path)  # no .completeness.json present
    res = read_completeness_report(cache, "json")
    assert res is not None
    data, media = res
    assert media is None
    assert "by_document" in data["summary"] and data["repo"] == "poc/x"
    # healed on disk
    assert (tmp_path / "deepwiki_cache_x.completeness.json").exists()
    assert (tmp_path / "deepwiki_cache_x.completeness.md").exists()


def test_self_heal_md(tmp_path):
    cache = _wikicache(tmp_path)
    res = read_completeness_report(cache, "md")
    assert res is not None
    text, media = res
    assert media == "text/markdown; charset=utf-8" and "TSD" in text


def test_no_report_and_no_wikicache_returns_none(tmp_path):
    assert read_completeness_report(str(tmp_path / "deepwiki_cache_x.json"), "json") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_completeness_io.py -k "atomic or self_heal or no_report_and" -q`
Expected: FAIL — `atomic_write_text` missing; self-heal not implemented (returns None).

- [ ] **Step 3: Implement atomic write + self-heal**

Replace the contents of `api/completeness_io.py` with:

```python
"""Read the sibling completeness report files for a wikicache path.

Kept separate from api.api so it imports no FastAPI/app state (and no file
logger), which makes it unit-testable in isolation. Also provides atomic writes
and read-time self-heal: a missing report is rebuilt from the sibling wikicache
(a pure function of its generated_pages) so the latest report never disappears.
"""
import json
import os
import tempfile
from typing import Optional, Tuple

from api.tsd_brd_completeness import (build_report_payload,
                                      completeness_report_paths,
                                      render_markdown_report)

_MD_MEDIA = "text/markdown; charset=utf-8"


def atomic_write_text(path: str, text: str) -> None:
    """Write text atomically: temp file in the same dir, fsync, os.replace."""
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _rebuild_from_wikicache(cache_path: str, json_path: str, md_path: str):
    """Rebuild the report from the sibling wikicache and persist it atomically.
    Returns the parsed payload dict, or None if the wikicache is unusable."""
    if not os.path.isfile(cache_path):
        return None
    with open(cache_path, encoding="utf-8") as f:
        wc = json.load(f)
    repo = wc.get("repo") or {}
    payload = build_report_payload(
        list((wc.get("generated_pages") or {}).values()),
        f"{repo.get('owner')}/{repo.get('repo')}",
        wc.get("provider"), wc.get("model"), wc.get("generated_at"))
    atomic_write_text(json_path, json.dumps(payload, ensure_ascii=False, indent=2))
    atomic_write_text(md_path, render_markdown_report(payload))
    return payload


def read_completeness_report(cache_path: str,
                             fmt: str) -> Optional[Tuple[object, Optional[str]]]:
    """Return (content, media_type) for the sibling report, or None if absent.

    fmt="md" -> (markdown_text, "text/markdown; charset=utf-8").
    Any other fmt -> (parsed_json, None).
    If the report file is missing but the wikicache exists, rebuild + persist it
    from the cached pages (self-heal). Never raises.
    """
    json_path, md_path = completeness_report_paths(cache_path)
    target = md_path if fmt == "md" else json_path
    try:
        if os.path.isfile(target):
            if fmt == "md":
                with open(md_path, encoding="utf-8") as f:
                    return f.read(), _MD_MEDIA
            with open(json_path, encoding="utf-8") as f:
                return json.load(f), None
        # Report missing — self-heal from the wikicache if it exists.
        payload = _rebuild_from_wikicache(cache_path, json_path, md_path)
        if payload is None:
            return None
        if fmt == "md":
            return render_markdown_report(payload), _MD_MEDIA
        return payload, None
    except Exception:
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_completeness_io.py -q`
Expected: PASS (all — new + the original json/md/absent/malformed tests; note the "absent report returns none" test now also has no wikecache, so it still returns None).

- [ ] **Step 5: Commit**

```bash
git add api/completeness_io.py tests/unit/test_completeness_io.py
git commit -m "feat: atomic report writes + read-time self-heal from wikicache"
```

---

### Task B3: Generator writes atomically via the shared builder

**Files:**
- Modify: `api/wiki_generator.py` (the best-effort completeness block ~571-590)
- Test: covered by B1/B2 unit tests + syntax check (importing `api.api`/`wiki_generator` is log-hostile in this sandbox).

**Interfaces:**
- Consumes: `build_report_payload` (B1), `atomic_write_text` (B2).

- [ ] **Step 1: Update imports**

In `api/wiki_generator.py`, add to the `from api.tsd_brd_completeness import (...)` block: `build_report_payload`. Add a new import: `from api.completeness_io import atomic_write_text`.

- [ ] **Step 2: Rewrite the report-write to use the shared builder + atomic writes**

Replace the current block:

```python
        report = check_tsd_brd_completeness(list(generated.values()))
        report = {"repo": f"{repo.owner}/{repo.repo}", "provider": job.provider,
                  "model": job.model,
                  "generated_at": datetime.now(timezone.utc).isoformat(),
                  **report}
        cache_path = get_wiki_cache_path(repo.owner, repo.repo, repo.type,
                                         job.language, job.provider, job.model)
        json_path, md_path = completeness_report_paths(cache_path)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(render_markdown_report(report))
```

with:

```python
        report = build_report_payload(
            list(generated.values()), f"{repo.owner}/{repo.repo}",
            job.provider, job.model, datetime.now(timezone.utc).isoformat())
        cache_path = get_wiki_cache_path(repo.owner, repo.repo, repo.type,
                                         job.language, job.provider, job.model)
        json_path, md_path = completeness_report_paths(cache_path)
        atomic_write_text(json_path, json.dumps(report, ensure_ascii=False, indent=2))
        atomic_write_text(md_path, render_markdown_report(report))
```

(The `by_document` log-summary lines that follow are unchanged.)

- [ ] **Step 3: Syntax + no-regression**

Run: `.venv/bin/python -c "import ast; ast.parse(open('api/wiki_generator.py').read()); print('syntax OK')"`
Expected: `syntax OK`

Run: `.venv/bin/python -m pytest tests/unit/test_tsd_brd_completeness.py tests/unit/test_completeness_io.py -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add api/wiki_generator.py
git commit -m "feat: generator writes completeness report atomically via shared builder"
```

---

### Task C: Key config + version bump + deploy + e2e

**Files:**
- Modify: `src/version.ts`; server `.env` (NOT committed)

- [ ] **Step 1: Put the API key in `.env` (gitignored, not committed)**

Append `ANTHROPIC_API_KEY=<the key>` to the repo-root `.env` used by docker-compose. Confirm `.env` is gitignored: `git check-ignore .env` → prints `.env`. Do NOT `git add` it.

- [ ] **Step 2: Bump APP_VERSION + commit code**

Edit `src/version.ts` (`0.3.27` → `0.3.28`).

```bash
git add src/version.ts
git commit -m "chore: bump APP_VERSION for claude_api provider + report durability"
```

- [ ] **Step 3: Rebuild + redeploy**

```bash
docker compose build deepwiki
docker compose up -d --no-deps deepwiki
```
Expected: builds; healthy; version `0.3.28`. Confirm the key reached the container: `docker exec deepwiki-open-deepwiki-1 sh -c 'echo ${ANTHROPIC_API_KEY:+set}'` → `set`.

- [ ] **Step 4: E2e — durability self-heal (free)**

```bash
BASE="owner=poc&repo=code1_cbl_bv401&repo_type=gitlab&language=zh-tw&provider=claude&model=claude-haiku-4-5-20251001"
docker exec deepwiki-open-deepwiki-1 sh -c 'rm -f /root/.adalflow/wikicache/*bv401*completeness.json'
curl -s "http://localhost:3000/api/wiki_completeness?${BASE}&format=json" | python3 -c "import sys,json; print('healed by_document:', bool(json.load(sys.stdin).get('summary',{}).get('by_document')))"
docker exec deepwiki-open-deepwiki-1 sh -c 'ls /root/.adalflow/wikicache/*bv401*completeness.json'
```
Expected: `healed by_document: True` and the `.json` file recreated on disk.

- [ ] **Step 5: E2e — claude_api smoke (minimal, paid)**

Cheap single call through the deployed dispatch (no full wiki regen):

```bash
docker exec -i deepwiki-open-deepwiki-1 python3 - <<'PY'
import asyncio
from api.llm_dispatch import generate
r = asyncio.run(generate("claude_api", "claude-sonnet-5",
    "/no_think Reply with exactly: PONG"))
print("claude_api sonnet-5 result:", (r.text or "").strip()[:40], "| in/out tok:", r.input_tokens, r.output_tokens)
PY
```
Expected: prints `PONG` (or similar) with non-zero output tokens — confirms `claude_api`/`claude-sonnet-5` generates via the API key end-to-end. (A full Sonnet-5 wiki regen is the operator's call given per-token cost.)

- [ ] **Step 6: Commit (version already committed in Step 2)**

No code change here; version committed in Step 2.

---

## Self-Review

**Spec A coverage:** api-key mode (A1) + provider (A2) + dispatch (A3); explicit-arg mode selection (no env hijack) → A1 `_auth_mode` + A3 guard + test `test_env_..._does_not_hijack_oauth`. `claude`/`claude_cli` untouched. ✓
**Spec B coverage:** atomic writes (B2 `atomic_write_text`, used in B2 self-heal + B3 generator); self-heal on read (B2); shared payload builder (B1) so generator + heal produce identical JSON; deletion-path audit noted (no change). Latest-only, no history. ✓
**E2e:** durability heal (free) + claude_api smoke (cheap) → Task C. ✓

**Placeholder scan:** none — every code/test step is complete. `<the key>` in Task C Step 1 is an operator-supplied secret, intentionally not written into the plan.

**Type consistency:** `AnthropicClient(auth_token, api_key, base_url)` + `_auth_mode()->str` used identically in A1/A3. `build_report_payload(pages, repo, provider, model, generated_at)` used in B1 test, B2 self-heal, B3 generator. `atomic_write_text(path, text)` used in B2 + B3. `read_completeness_report(cache_path, fmt) -> (content, media)|None` contract preserved (endpoint in api.py unchanged, still consumes it).

**Cross-feature note:** A and B touch disjoint files except none overlap (`anthropic_client.py`/`generator.json`/`llm_dispatch.py` for A; `tsd_brd_completeness.py`/`completeness_io.py`/`wiki_generator.py` for B). `wiki_generator.py` is touched only by B. No task ordering hazard beyond B3 depending on B1+B2 (later in sequence).
