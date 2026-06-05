# Claude (OAuth Token) Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `claude` generator provider to deepwiki that talks to Anthropic's OpenAI-compatible endpoint using a long-lived OAuth token from `claude setup-token`.

**Architecture:** `ClaudeClient` subclasses the existing `VLLMClient` (which is really a generic OpenAI-compatible client with per-request usage logging), overriding only auth (bearer OAuth token + `anthropic-beta: oauth-2025-04-20` header) and endpoint (`https://api.anthropic.com/v1`, overridable for a relay). Provider plumbing follows the exact pattern used for `vllm`: registry entry in `config.py`, model block in `generator.json`, request branches in `websocket_wiki.py` and `simple_chat.py`. No frontend changes — the UI reads providers from `/models/config`.

**Tech Stack:** Python (FastAPI backend), `openai` SDK 2.6.0 (already installed — no new dependencies), Docker Compose deployment.

---

## Constraints & context (read before starting)

1. **Token source:** User runs `claude setup-token` (requires Claude Pro/Max) and pastes the long-lived `sk-ant-oat01-...` token into `.env` as `CLAUDE_OAUTH_TOKEN`. No refresh logic. `docker-compose.yml` already loads `.env` via `env_file`, so no compose changes.
2. **Geo-blocking:** This deployment is HK-hosted and `api.anthropic.com` may be unreachable directly. `CLAUDE_API_BASE_URL` must be overridable so traffic can route via a relay/proxy. Task 1 verifies connectivity *before* any code is written.
3. **ToS caveat (surface to user, do not hide):** OAuth tokens from `claude setup-token` are issued for Claude Code / Agent SDK use. Anthropic may reject them on the OpenAI-compat endpoint or require Claude-Code-identifying request shapes on the native endpoint. Task 1 is the go/no-go gate; if the compat endpoint rejects OAuth tokens, STOP and re-plan around the native `anthropic` SDK (different streaming event shape → new branches in both consumers).
4. **Embeddings are unaffected.** Anthropic has no embedding API; the bge-m3 vLLM embedder stays as-is.
5. **Code is baked into the Docker image** (only `api/logs` and `~/.adalflow` are bind-mounted). Tests run against the built image; final deploy needs `docker compose up -d --build`.
6. **Test runner:** The host has no Python deps. Run tests inside the image with the working tree mounted over `/app`:
   ```bash
   docker compose build
   docker run --rm --entrypoint python -v /home/ubuntu/deepwiki-open:/app deepwiki-open-deepwiki -m pytest tests/unit/test_claude_client.py -v
   ```
   (If the image defines an entrypoint that interferes, fall back to `docker cp` of changed files into the running container + `docker exec ... python -m pytest ...` — this pattern was already used successfully for the vLLM usage-logging change.)
   Verified 2026-06-05: pytest 8.4.2 is present in the current image (transitive dependency) even though the Dockerfile installs `--only main` — the commands above work as written.
7. **RAG instantiation note:** `RAG.__init__` (`api/rag.py:240`) constructs the provider client immediately on every chat request, not just at generation time — so with `provider=claude`, a missing `CLAUDE_OAUTH_TOKEN` fails fast at RAG init with the `ValueError` from `_resolve_token`. This is intended behavior (clear error, early), just don't be surprised that the error appears before any API call.

---

### Task 1: Connectivity spike (go/no-go gate — no code)

**Files:** none (manual verification)

- [ ] **Step 1: Obtain the token**

User runs on a machine with Claude Code logged in:
```bash
claude setup-token
```
Copy the `sk-ant-oat01-...` value.

- [ ] **Step 2: Verify non-streaming chat completion with OAuth bearer + beta header**

```bash
export TOKEN="sk-ant-oat01-..."
export BASE="https://api.anthropic.com/v1"   # or relay URL if direct access is geo-blocked
curl -s "$BASE/chat/completions" \
  -H "Authorization: Bearer $TOKEN" \
  -H "anthropic-beta: oauth-2025-04-20" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-4-6","max_tokens":32,"messages":[{"role":"user","content":"Reply with the single word OK"}]}'
```
Expected: HTTP 200 JSON with `choices[0].message.content` ≈ "OK".
If 401/403: retry **without** the `anthropic-beta` header (some gateways inject it). If both fail with an auth error mentioning the token type → **STOP: compat endpoint rejects OAuth tokens; re-plan with native anthropic SDK.**

- [ ] **Step 3: Verify streaming + usage reporting**

```bash
curl -sN "$BASE/chat/completions" \
  -H "Authorization: Bearer $TOKEN" \
  -H "anthropic-beta: oauth-2025-04-20" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-4-6","max_tokens":32,"stream":true,"stream_options":{"include_usage":true},"messages":[{"role":"user","content":"Reply with the single word OK"}]}' | tail -5
```
Expected: SSE chunks ending with a final chunk containing `"usage":{"prompt_tokens":...}` then `data: [DONE]`.
If `stream_options` is rejected (400): note it — usage logging will silently log nothing (the inherited `_log_usage` no-ops on `None`), and the `stream_options` injection must be dropped in Task 2 (remove the `setdefault` inheritance by overriding `convert_inputs_to_api_kwargs` to strip it).

- [ ] **Step 4: Confirm available model IDs**

```bash
curl -s "$BASE/models" -H "Authorization: Bearer $TOKEN" -H "anthropic-beta: oauth-2025-04-20" | python3 -m json.tool | grep '"id"'
```
Expected: a list including `claude-sonnet-4-6`, `claude-opus-4-8`, `claude-haiku-4-5-20251001` (the IDs used in Task 4's `generator.json`). If the actual IDs differ, use the listed ones in Task 4 instead.

- [ ] **Step 5: Record results**

Write the working combination (base URL used, whether the beta header was needed, whether usage chunks arrived, confirmed model IDs) at the bottom of this plan file under "## Spike results". Subsequent tasks reference it — in particular, **if Step 3 showed `stream_options` rejected, the override in Task 3 Step 3's final note becomes a REQUIRED part of the implementation, not optional.**

---

### Task 2: Parameterize the usage-log prefix in VLLMClient

`ClaudeClient` will inherit `VLLMClient`'s usage logging, but the hardcoded `"vLLM usage:"` message would mislead. Make the prefix a class attribute first.

**Files:**
- Modify: `api/vllm_client.py` (the `_log_usage` method, ~line 68)
- Test: `tests/unit/test_claude_client.py` (created here, extended in Task 3)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_claude_client.py`:
```python
"""Tests for usage-log prefix parameterization and the ClaudeClient."""
import logging

from api.vllm_client import VLLMClient


class FakeUsage:
    prompt_tokens = 10
    completion_tokens = 5
    total_tokens = 15


def test_vllm_usage_log_prefix(caplog):
    client = VLLMClient.__new__(VLLMClient)  # skip __init__ (no network/env needed)
    with caplog.at_level(logging.INFO, logger="api.vllm_client"):
        client._log_usage(FakeUsage(), "some-model")
    assert "vLLM usage: model=some-model prompt_tokens=10" in caplog.text


def test_usage_log_prefix_is_overridable(caplog):
    class Sub(VLLMClient):
        usage_log_prefix = "Claude usage"

    client = Sub.__new__(Sub)
    with caplog.at_level(logging.INFO, logger="api.vllm_client"):
        client._log_usage(FakeUsage(), "claude-sonnet-4-6")
    assert "Claude usage: model=claude-sonnet-4-6" in caplog.text
```

- [ ] **Step 2: Run test to verify it fails**

```bash
docker run --rm --entrypoint python -v /home/ubuntu/deepwiki-open:/app deepwiki-open-deepwiki \
  -m pytest tests/unit/test_claude_client.py -v
```
Expected: `test_usage_log_prefix_is_overridable` FAILS (log says "vLLM usage:" regardless of subclass).

- [ ] **Step 3: Implement the class attribute**

In `api/vllm_client.py`, inside `class VLLMClient(OpenAIClient):` add the attribute (right after the docstring, before `__init__`):
```python
    # Prefix used by _log_usage lines; subclasses override (e.g. "Claude usage").
    usage_log_prefix = "vLLM usage"
```
and change `_log_usage` to use it:
```python
    def _log_usage(self, usage: Any, model: Optional[str]) -> None:
        """Log token usage reported by the server."""
        if usage is None:
            return
        log.info(
            f"{self.usage_log_prefix}: model={model} prompt_tokens={getattr(usage, 'prompt_tokens', None)} "
            f"completion_tokens={getattr(usage, 'completion_tokens', None)} "
            f"total_tokens={getattr(usage, 'total_tokens', None)}"
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
docker run --rm --entrypoint python -v /home/ubuntu/deepwiki-open:/app deepwiki-open-deepwiki \
  -m pytest tests/unit/test_claude_client.py -v
```
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add api/vllm_client.py tests/unit/test_claude_client.py
git commit -m "Parameterize usage-log prefix on VLLMClient"
```

---

### Task 3: ClaudeClient

**Files:**
- Create: `api/claude_client.py`
- Test: `tests/unit/test_claude_client.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_claude_client.py`:
```python
import pytest

from api import claude_client as claude_module
from api.claude_client import ClaudeClient


@pytest.fixture
def captured_openai(monkeypatch):
    """Capture kwargs passed to the OpenAI/AsyncOpenAI constructors."""
    captured = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(claude_module, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(claude_module, "AsyncOpenAI", FakeOpenAI)
    return captured


def test_default_base_url_appends_v1(monkeypatch, captured_openai):
    monkeypatch.delenv("CLAUDE_API_BASE_URL", raising=False)
    monkeypatch.setenv("CLAUDE_OAUTH_TOKEN", "sk-ant-oat01-test")
    client = ClaudeClient()
    assert client.base_url == "https://api.anthropic.com/v1"


def test_relay_base_url_from_env(monkeypatch, captured_openai):
    monkeypatch.setenv("CLAUDE_API_BASE_URL", "http://relay.local:9000")
    monkeypatch.setenv("CLAUDE_OAUTH_TOKEN", "sk-ant-oat01-test")
    client = ClaudeClient()
    assert client.base_url == "http://relay.local:9000/v1"


def test_sync_client_gets_token_and_beta_header(monkeypatch, captured_openai):
    monkeypatch.delenv("CLAUDE_API_BASE_URL", raising=False)
    monkeypatch.setenv("CLAUDE_OAUTH_TOKEN", "sk-ant-oat01-test")
    # OpenAIClient.__init__ calls init_sync_client() eagerly (openai_client.py:182),
    # so constructing the client is enough to capture the constructor kwargs.
    ClaudeClient()
    assert captured_openai["api_key"] == "sk-ant-oat01-test"
    assert captured_openai["default_headers"]["anthropic-beta"] == "oauth-2025-04-20"
    assert captured_openai["base_url"] == "https://api.anthropic.com/v1"


def test_missing_token_raises(monkeypatch):
    monkeypatch.delenv("CLAUDE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_API_BASE_URL", raising=False)
    # init_sync_client() runs inside __init__, so construction itself raises.
    with pytest.raises(ValueError, match="CLAUDE_OAUTH_TOKEN"):
        ClaudeClient()


def test_claude_usage_log_prefix():
    assert ClaudeClient.usage_log_prefix == "Claude usage"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
docker run --rm --entrypoint python -v /home/ubuntu/deepwiki-open:/app deepwiki-open-deepwiki \
  -m pytest tests/unit/test_claude_client.py -v
```
Expected: new tests FAIL with `ModuleNotFoundError: No module named 'api.claude_client'`.

- [ ] **Step 3: Implement ClaudeClient**

Create `api/claude_client.py`:
```python
import logging
import os
from typing import Optional, Callable

from openai import AsyncOpenAI, OpenAI

from api.vllm_client import VLLMClient

log = logging.getLogger(__name__)

# Required for Anthropic to accept OAuth (sk-ant-oat01-...) bearer tokens.
# If the Task-1 spike showed the header is unnecessary (e.g. a relay injects
# it), it is still harmless to send.
ANTHROPIC_OAUTH_BETA = "oauth-2025-04-20"


class ClaudeClient(VLLMClient):
    """
    Claude via Anthropic's OpenAI-compatible endpoint with OAuth bearer auth.

    Subclasses VLLMClient because that class is effectively a generic
    OpenAI-compatible client with per-request token-usage logging; only the
    endpoint, authentication, and log prefix differ.

    Expected environment variables:

        CLAUDE_API_BASE_URL=https://api.anthropic.com   # or a relay/proxy URL
        CLAUDE_OAUTH_TOKEN=sk-ant-oat01-...             # from `claude setup-token`

    The token is a long-lived OAuth token generated with `claude setup-token`
    (requires a Claude Pro/Max subscription). There is no refresh logic; if
    the token is revoked or expires, generate a new one and update .env.
    """

    usage_log_prefix = "Claude usage"

    def __init__(
        self,
        api_key: Optional[str] = None,
        chat_completion_parser: Optional[Callable] = None,
        input_type: str = "text",
        base_url: Optional[str] = None,
    ):
        super().__init__(
            api_key=api_key,
            chat_completion_parser=chat_completion_parser,
            input_type=input_type,
            base_url=base_url
            or os.getenv("CLAUDE_API_BASE_URL", "https://api.anthropic.com"),
            env_base_url_name="CLAUDE_API_BASE_URL",
            env_api_key_name="CLAUDE_OAUTH_TOKEN",
        )

    def _resolve_token(self) -> str:
        token = self._api_key or os.getenv(self._env_api_key_name)
        if not token:
            raise ValueError(
                "CLAUDE_OAUTH_TOKEN is not set. Run `claude setup-token` and "
                "add the sk-ant-oat01-... value to .env"
            )
        return token

    def init_sync_client(self):
        """Initialize synchronous OpenAI-compatible client for Anthropic."""
        return OpenAI(
            api_key=self._resolve_token(),
            base_url=self.base_url,
            default_headers={"anthropic-beta": ANTHROPIC_OAUTH_BETA},
        )

    def init_async_client(self):
        """Initialize asynchronous OpenAI-compatible client for Anthropic."""
        return AsyncOpenAI(
            api_key=self._resolve_token(),
            base_url=self.base_url,
            default_headers={"anthropic-beta": ANTHROPIC_OAUTH_BETA},
        )
```
Notes:
- `VLLMClient.__init__` appends `/v1` to the base URL if missing — that is why the default is `https://api.anthropic.com` (becomes `https://api.anthropic.com/v1`).
- Streaming usage logging and `stream_options={"include_usage": True}` injection are inherited from `VLLMClient`. **If the Task-1 spike showed `stream_options` is rejected**, add this override to strip it:
```python
    def convert_inputs_to_api_kwargs(self, input=None, model_kwargs={}, model_type=None) -> dict:
        api_kwargs = super().convert_inputs_to_api_kwargs(input, model_kwargs, model_type)
        api_kwargs.pop("stream_options", None)  # compat endpoint rejects it (see spike results)
        return api_kwargs
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
docker run --rm --entrypoint python -v /home/ubuntu/deepwiki-open:/app deepwiki-open-deepwiki \
  -m pytest tests/unit/test_claude_client.py -v
```
Expected: all tests pass (2 from Task 2 + 5 new).

- [ ] **Step 5: Commit**

```bash
git add api/claude_client.py tests/unit/test_claude_client.py
git commit -m "Add ClaudeClient: Anthropic OpenAI-compat endpoint with OAuth bearer auth"
```

---

### Task 4: Provider registry + model config

**Files:**
- Modify: `api/config.py` (imports ~line 12, `CLIENT_CLASSES` ~line 68, provider list + `default_map` ~lines 141–156)
- Modify: `api/config/generator.json` (add `claude` provider block after `vllm`, which is at lines 116–126)

- [ ] **Step 1: Register the client class in `api/config.py`**

Add to the imports (next to `from api.vllm_client import VLLMClient`):
```python
from api.claude_client import ClaudeClient
```
Add to `CLIENT_CLASSES` dict:
```python
    "ClaudeClient": ClaudeClient,
```
In `load_generator_config()`, change the provider-id list:
```python
            elif provider_id in ["google", "openai", "openrouter", "ollama", "bedrock", "azure", "dashscope", "litellm", "vllm", "claude"]:
```
and add to `default_map`:
```python
                    "claude": ClaudeClient,
```

- [ ] **Step 2: Add the provider block to `api/config/generator.json`**

After the `"vllm"` block (match surrounding JSON style):
```json
    "claude": {
      "client_class": "ClaudeClient",
      "default_model": "claude-sonnet-4-6",
      "supportsCustomModel": true,
      "models": {
        "claude-sonnet-4-6": {
          "temperature": 0.7,
          "max_tokens": 8192
        },
        "claude-opus-4-8": {
          "temperature": 0.7,
          "max_tokens": 8192
        },
        "claude-haiku-4-5-20251001": {
          "temperature": 0.7,
          "max_tokens": 8192
        }
      }
    },
```
(`max_tokens` is passed through in Task 5's branches; the Anthropic API enforces a max output and benefits from an explicit value. No `top_p` — Anthropic recommends temperature only.)

- [ ] **Step 3: Validate JSON and registry wiring**

```bash
python3 -c "import json; json.load(open('api/config/generator.json')); print('JSON OK')"
docker run --rm --entrypoint python -v /home/ubuntu/deepwiki-open:/app deepwiki-open-deepwiki -c "
from api.config import get_model_config
cfg = get_model_config('claude', 'claude-sonnet-4-6')
assert cfg['model_client'].__name__ == 'ClaudeClient', cfg
assert cfg['model_kwargs']['max_tokens'] == 8192
print('registry OK:', cfg['model_kwargs'])
"
```
Expected: `JSON OK` and `registry OK: {'model': 'claude-sonnet-4-6', 'temperature': 0.7, 'max_tokens': 8192}` (exact shape depends on `get_model_config`; assert what it actually returns).

- [ ] **Step 4: Commit**

```bash
git add api/config.py api/config/generator.json
git commit -m "Register claude provider in config and generator.json"
```

---

### Task 5: Request branches in websocket_wiki.py and simple_chat.py

The streaming consumers already handle OpenAI-shaped chunks for `("litellm", "vllm")` / `("openai", "vllm", "litellm")` — claude only needs a client-construction branch plus membership in those tuples (primary + fallback paths).

**Files:**
- Modify: `api/websocket_wiki.py` — import (~line 25), new `elif` after the vllm branch (vllm branch ends ~line 553), tuple at ~line 689, fallback tuple at ~line 864
- Modify: `api/simple_chat.py` — import (~line 17), new `elif` after the vllm branch (~line 420), tuple at ~line 537, fallback tuple at ~line 669

- [ ] **Step 1: websocket_wiki.py — import**

Next to `from api.vllm_client import VLLMClient`:
```python
from api.claude_client import ClaudeClient
```

- [ ] **Step 2: websocket_wiki.py — client-construction branch**

Insert after the `elif request.provider == "vllm":` block (i.e. immediately before `elif request.provider == "bedrock":`):
```python
        elif request.provider == "claude":
            logger.info(f"Using Claude (OpenAI-compat protocol) with model: {request.model}")

            # OAuth bearer token from `claude setup-token` (CLAUDE_OAUTH_TOKEN)
            model = ClaudeClient()
            model_kwargs = {
                "model": request.model,
                "stream": True,
                "temperature": model_config["temperature"]
            }
            # Only add top_p / max_tokens if present in the model config
            if "top_p" in model_config:
                model_kwargs["top_p"] = model_config["top_p"]
            if "max_tokens" in model_config:
                model_kwargs["max_tokens"] = model_config["max_tokens"]

            api_kwargs = model.convert_inputs_to_api_kwargs(
                input=prompt,
                model_kwargs=model_kwargs,
                model_type=ModelType.LLM
            )
```

- [ ] **Step 3: websocket_wiki.py — response-handling tuples**

Line ~689 (primary path):
```python
            elif request.provider in ("litellm", "vllm", "claude"):
```
Line ~864 (fallback path):
```python
                    elif request.provider in ("litellm", "vllm", "claude"):
```
(Search for `("litellm", "vllm")` — there are exactly these two occurrences; update both.)

- [ ] **Step 4: simple_chat.py — same three changes**

Import next to `from api.vllm_client import VLLMClient`:
```python
from api.claude_client import ClaudeClient
```
Insert after the `elif request.provider == "vllm":` block (same code as Step 2 — repeat it verbatim, indentation matches the surrounding `elif` chain in this file):
```python
        elif request.provider == "claude":
            logger.info(f"Using Claude (OpenAI-compat protocol) with model: {request.model}")

            # OAuth bearer token from `claude setup-token` (CLAUDE_OAUTH_TOKEN)
            model = ClaudeClient()
            model_kwargs = {
                "model": request.model,
                "stream": True,
                "temperature": model_config["temperature"]
            }
            # Only add top_p / max_tokens if present in the model config
            if "top_p" in model_config:
                model_kwargs["top_p"] = model_config["top_p"]
            if "max_tokens" in model_config:
                model_kwargs["max_tokens"] = model_config["max_tokens"]

            api_kwargs = model.convert_inputs_to_api_kwargs(
                input=prompt,
                model_kwargs=model_kwargs,
                model_type=ModelType.LLM
            )
```
Tuples — search for `("openai", "vllm", "litellm")` (primary ~line 537, fallback ~line 669) and change both to:
```python
                elif request.provider in ("openai", "vllm", "litellm", "claude"):
```

- [ ] **Step 5: Syntax-check both files**

```bash
python3 -c "import ast; ast.parse(open('api/websocket_wiki.py').read()); ast.parse(open('api/simple_chat.py').read()); print('syntax OK')"
```
Expected: `syntax OK`

- [ ] **Step 6: Commit**

```bash
git add api/websocket_wiki.py api/simple_chat.py
git commit -m "Wire claude provider into chat and wiki-generation request paths"
```

---

### Task 6: Environment template + deploy + end-to-end verification

**Files:**
- Modify: `.env` (live config — append, do not reorder existing lines)

- [ ] **Step 1: Add env vars to `.env`**

```bash
# Claude via OAuth token (from `claude setup-token`, requires Pro/Max)
CLAUDE_OAUTH_TOKEN=sk-ant-oat01-REPLACE_ME
# Optional: relay/proxy if api.anthropic.com is unreachable from this host
# CLAUDE_API_BASE_URL=https://your-relay.example.com
```
Use the real token from Task 1. If the Task 1 spike required a relay, uncomment and set `CLAUDE_API_BASE_URL`.

- [ ] **Step 2: Rebuild and restart**

```bash
docker compose up -d --build
```
Expected: container recreated, `docker ps` shows `(healthy)` within ~30s.

- [ ] **Step 3: Verify provider appears in UI config**

```bash
curl -s http://localhost:8001/models/config | python3 -m json.tool | grep -A2 '"claude"'
```
Expected: a `claude` provider with the three models.

- [ ] **Step 4: End-to-end chat through the real API**

```bash
curl -s --max-time 90 -X POST http://localhost:8001/chat/completions/stream \
  -H 'Content-Type: application/json' -d '{
  "repo_url": "https://gitlab.reslv.one/poc/code_advanced.git",
  "type": "gitlab",
  "provider": "claude",
  "model": "claude-sonnet-4-6",
  "messages": [{"role": "user", "content": "Reply with the single word OK"}]
}'
grep "Claude usage" api/logs/application.log | tail -1
```
Expected: response contains `OK`; log line like
`api.claude_client - Claude usage: model=claude-sonnet-4-6 prompt_tokens=... completion_tokens=... total_tokens=...`
(If the spike showed no streaming-usage support, the log line will be absent — that is expected, not a failure.)

- [ ] **Step 5: Commit any final tweaks and verify clean tree**

```bash
git status   # only .env (untracked changes to live config) and the two pre-existing .docx files should remain
```
`.env` contains the secret token — **do not commit it.** Confirm `.env` is in `.gitignore`; if not, add it before committing anything else.

---

## Spike results (fill in during Task 1)

- Base URL used: _
- `anthropic-beta: oauth-2025-04-20` header required: yes / no
- Streaming `stream_options.include_usage` supported: yes / no  (if **no** → the `convert_inputs_to_api_kwargs` override in Task 3 is REQUIRED)
- Confirmed model IDs: _
- Notes: _
