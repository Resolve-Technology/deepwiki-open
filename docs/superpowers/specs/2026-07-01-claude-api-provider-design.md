# `claude_api` Provider (Sonnet 5 via standard API key) — Design

**Date:** 2026-07-01
**Status:** Approved (design), pending implementation plan

## Problem

The subscription-OAuth `claude` provider 429s on Sonnet/Opus (only Haiku is
usable). We now have a standard Anthropic **API key** (`sk-ant-api03-…`) that can
reach `claude-sonnet-5` without those limits. But the server-side generator's
`claude` branch uses `AnthropicClient`, which authenticates with an OAuth
**Bearer** token (`auth_token=`) plus the `oauth-2025-04-20` beta header — a
scheme the standard key rejects (`"Invalid bearer token"`, verified). A standard
key must authenticate with `api_key=` (→ `x-api-key`), no oauth beta (verified
working with `claude-sonnet-5`).

## Goal

Add a new, separate generation provider `claude_api` that uses the standard API
key to generate with `claude-sonnet-5` (and `claude-opus-4-8`), while the
existing `claude` provider stays on the subscription (Haiku) unchanged.

## Non-goals (YAGNI)

- Don't change the `claude` (OAuth) or `claude_cli` providers.
- No key-string sniffing to auto-detect auth mode — the mode is chosen by an
  explicit constructor argument.
- Cost/quota management is out of scope (the key is pay-per-token; the operator
  chooses when to use `claude_api`).

## Architecture

### 1. API-key auth mode in `AnthropicClient` (`api/anthropic_client.py`)

Add an `api_key` parameter to `__init__(self, auth_token=None, api_key=None, base_url=None)`.
`init_async_client` selects the mode:

- **api-key mode** — when `api_key` (constructor arg) or the `ANTHROPIC_API_KEY`
  env var is present: build `anthropic.AsyncAnthropic(api_key=<key>, base_url=<resolved>, max_retries=3)` with **no** `anthropic-beta` header. (`x-api-key` auth.)
- **OAuth mode** — otherwise: the current
  `anthropic.AsyncAnthropic(auth_token=<CLAUDE_OAUTH_TOKEN>, base_url=<resolved>, default_headers={"anthropic-beta": "oauth-2025-04-20"}, max_retries=3)`.

Base URL: reuse the existing `_resolve_base_url()` (returns `None` → SDK default
`https://api.anthropic.com`, correct for `x-api-key`; the deployment's
`CLAUDE_API_BASE_URL` is empty). A helper `_auth_mode()` / `_resolve_api_key()`
keeps the selection explicit and unit-testable without a network call.

### 2. Key storage

`ANTHROPIC_API_KEY=<the standard key>` in the server's `.env` (gitignored,
already mounted into the container via docker-compose's env). Never committed.

### 3. New provider in `api/config/generator.json`

```json
"claude_api": {
  "client_class": "ClaudeClient",
  "default_model": "claude-sonnet-5",
  "supportsCustomModel": true,
  "models": {
    "claude-sonnet-5": { "temperature": 0.3, "max_tokens": 64000 },
    "claude-opus-4-8": { "max_tokens": 100000, "thinking": "adaptive" }
  }
}
```

(`client_class` mirrors the `claude` provider's metadata value; the actual
server-side client is chosen in the dispatch, below.) The `claude` provider is
left exactly as-is.

### 4. Dispatch (`api/llm_dispatch.py`)

Extend the existing `claude` branch to also handle `claude_api`:

```python
if provider in ("claude", "claude_api"):
    client = (AnthropicClient(api_key=os.getenv("ANTHROPIC_API_KEY"))
              if provider == "claude_api" else AnthropicClient())
    ... (unchanged streaming + usage logic) ...
```

`claude_api` with no `ANTHROPIC_API_KEY` set raises the client's clear
"key not set" error (fails the job fast with a clear message).

## Data flow

Job with `provider="claude_api", model="claude-sonnet-5"` → dispatch builds
`AnthropicClient(api_key=ANTHROPIC_API_KEY)` (x-api-key mode) → native Anthropic
`/v1/messages` with the standard key → streamed generation. The `claude`/haiku
subscription path is unaffected.

## Error handling

- Missing `ANTHROPIC_API_KEY` → `AnthropicClient` api-key mode raises a clear
  ValueError; the job fails fast (best-effort report/log still behave normally).
- Auth/model errors surface via the Anthropic SDK as today.

## Testing

`tests/unit/test_anthropic_client.py`:
1. `AnthropicClient(api_key="sk-ant-api03-x")` → api-key mode: the constructed
   async client uses `api_key`/`x-api-key` and sends NO `anthropic-beta` oauth
   header (assert via the client's kwargs / a `_auth_mode()` helper returning
   `"api_key"`).
2. `AnthropicClient()` with `CLAUDE_OAUTH_TOKEN` set → OAuth mode: `auth_token`
   + `anthropic-beta: oauth-2025-04-20` (assert `_auth_mode() == "oauth"`).
3. api-key mode with neither arg nor `ANTHROPIC_API_KEY` falls back / OAuth mode
   with no token raises the existing clear error.

`tests/unit/test_claude_api_provider_config.py` (mirrors the existing
`test_claude_cli_provider_config.py`): `claude_api` provider is registered in
`generator.json` with `claude-sonnet-5` and `claude-opus-4-8`.

`tests/unit/test_llm_dispatch.py`: `provider="claude_api"` constructs
`AnthropicClient` with the API key (patched), not the OAuth client.

E2e: with `ANTHROPIC_API_KEY` in `.env` and redeployed, a minimal
`claude_api`/`claude-sonnet-5` generation succeeds (a small smoke check to limit
per-token cost; a full wiki regen on Sonnet 5 is the operator's call).

## Security

The API key is a secret: `.env` only, gitignored, never logged. It was shared in
chat and used in verification calls, so it should be rotated by the operator.
