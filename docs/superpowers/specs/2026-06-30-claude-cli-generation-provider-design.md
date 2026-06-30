# `claude_cli` generation provider — design

**Date:** 2026-06-30
**Status:** Approved (design); implementation pending

## Problem

Wiki generation through the `claude` provider calls Anthropic's OpenAI-compatible
endpoint (`api.anthropic.com/v1/chat/completions`) with the `setup-token` OAuth
bearer (`api/claude_client.py`). On that path **Sonnet and Opus return 429**
while only Haiku succeeds; the premium cap on the setup-token path is separate
from, and tighter than, the Claude Code subscription quota. As a result the
bv401 wiki (`poc/code1_cbl_bv401`, gitlab, lang `zh-tw`, 23 pages) could only be
generated with Haiku.

The `claude -p` CLI authenticates against the Claude Code subscription quota,
which is expected to serve Sonnet/Opus where the SDK path 429s. We want a
reusable generation backend that routes through `claude -p`, and to use it first
to regenerate bv401 with Sonnet.

## Goal

Add a permanent, reusable `claude_cli` **generation** provider that drives the
`claude -p` CLI instead of the Anthropic SDK, wired into the existing server-side
job queue. Out of scope: the interactive chat path (`simple_chat.py`,
`websocket_wiki.py`) — generation only.

## Background: where the integration point is

The job runner injects a single dispatch function:

- `api/wiki_jobs.py`: `JobManager.dispatch = llm_dispatch.generate`
- `api/wiki_generator.py`: `run_generation(job, dispatch, on_progress)` calls
  `dispatch(job.provider, job.model, prompt) -> LLMResult` per page (and in the
  citation-grounding fix loop).
- `api/llm_dispatch.py`: `generate(provider, model, prompt)` branches per
  provider (`claude` → native Anthropic SDK, `vllm` → `VLLMClient`), drains the
  stream, returns `LLMResult(text, input_tokens, output_tokens)`.

Adding a `provider == "claude_cli"` branch to `generate()` is the entire wiring;
nothing else in the generation path needs to change.

## Components

### 1. `api/claude_cli_client.py` (new)

Async helper:

```
async def run_claude_cli(model: str, prompt: str, *, timeout: float) -> tuple[str, int, int]
```

- Runs `claude -p --model <model> --output-format json` via
  `asyncio.create_subprocess_exec` (non-blocking in the asyncio worker).
- Writes `prompt` to **stdin** (page prompts + RAG context are large; avoids
  ARG_MAX).
- Runs with **`--safe-mode --tools ""`** (and `--strict-mcp-config`) for
  deterministic pure-text generation. `--strict-mcp-config` alone is
  insufficient: by default `claude -p` auto-discovers CLAUDE.md, user
  `settings.json`, skills, hooks, output styles, and auto-memory — and the
  mounted host `~/.claude` contains exactly those (e.g.
  `projects/.../memory/MEMORY.md`), which would contaminate every generation
  prompt. `--safe-mode` disables CLAUDE.md/skills/plugins/hooks/MCP/custom
  commands/output styles while **keeping OAuth + model selection + built-in
  tools + permissions working** (so the subscription auth still works);
  `--tools ""` then disables the built-in tools that safe-mode leaves on.
  **Do not** use `CLAUDE_CODE_SIMPLE=1` — it skips OAuth/keychain and would
  break the subscription auth this whole effort depends on.
- Parses stdout JSON: returns (`result`, `usage.input_tokens`,
  `usage.output_tokens`). The CLI also reports cache token fields; only
  input/output are surfaced to keep parity with `LLMResult`.
- **Fails fast** (raises), matching the existing "jobs fail fast with a clear
  error" philosophy in `llm_dispatch`, on: nonzero exit, JSON parse failure,
  missing `result`, OR an error reported **inside the JSON** — the CLI exits 0
  even on API errors and signals them via `is_error == true` /
  `subtype != "success"` / a non-empty `api_error_status`. Detecting these is
  what makes the step-0 rate-limit smoke gate meaningful (stderr string-matching
  would miss them).
- Binary path configurable via `CLAUDE_CLI_BIN` env (default `claude`).
- Generous default timeout (a single page can take minutes); configurable via
  env (e.g. `CLAUDE_CLI_TIMEOUT`, default 600s).

### 2. `api/llm_dispatch.py`

Add:

```
if provider == "claude_cli":
    text, in_tok, out_tok = await run_claude_cli(model, prompt, timeout=...)
    return LLMResult(text, in_tok, out_tok)
```

Non-streaming (the CLI returns the whole result). Temperature/top_p from
`get_model_config` are not forwarded — the CLI manages sampling itself.

### 3. `api/config/generator.json`

Add provider:

```
"claude_cli": {
  "client_class": "ClaudeClient",   // so get_model_config() resolves a model_client; the branch ignores it
  "default_model": "claude-sonnet-4-6",
  "supportsCustomModel": true,
  "models": {
    "claude-sonnet-4-6": { "max_tokens": 64000 },
    "claude-opus-4-8":   { "max_tokens": 100000 },
    "claude-haiku-4-5-20251001": { "max_tokens": 64000 }
  }
}
```

`client_class: "ClaudeClient"` is reused only so `get_model_config()`
(`api/config.py`, which maps `client_class` → `model_client` via
`CLIENT_CLASSES`) does not raise; the `claude_cli` dispatch branch never
instantiates the client.

### 4. `docker-compose.yml` (+ Dockerfile fallback)

The container (`python:3.11-slim`, runs as root, `HOME=/root`) has neither the
CLI nor the subscription credentials. Add mounts:

- Host `~/.local/share/claude` → container, **read-only** (the native ELF
  binary, v2.1.196 — a dynamically-linked x86-64 glibc ELF), with `claude` on
  PATH (symlink or `/usr/local/bin/claude`).
- Host `~/.claude` → `/root/.claude`, **read-write** — NOT read-only. The CLI
  writes to this dir (`file-history/`, `projects/`, `.last-cleanup`) and, in
  particular, **refreshes the OAuth token in `.credentials.json`**; a read-only
  mount blocks the refresh and a long multi-page run can fail when the access
  token expires mid-run. `--safe-mode` neutralizes the contamination risk from
  the `settings.json` / auto-memory this dir also contains (see Component 1).

The host `claude` is a self-contained native ELF binary (not npm/node), so
mounting it needs no node in the Python stage. **Fallback** if the host ELF will
not run on `python:3.11-slim` (glibc mismatch): run the native installer in the
Dockerfile (`curl -fsSL https://claude.ai/install.sh | bash`) — same ELF, still
node-free — rather than `npm i -g @anthropic-ai/claude-code` (npm would need
node, which the Python stage lacks). The smoke test (below) determines whether a
fallback is needed at all.

## Execution plan

1. **Smoke test gates everything (step 0).** Run `claude -p --model
   claude-sonnet-4-6 "ping"` on the host, then inside the container after the
   mounts are in place. If it 429s the way the SDK does, **stop** — the premise
   is false and the approach gives no benefit. Proceed only on a clean response.
2. Verify the embedder is reachable for RAG (`VLLM_EMBEDDER_BASE_URL`; the
   embedder port has moved before — all-citations-unverified is the outage
   signature).
3. Bump `src/version.ts` `APP_VERSION` and rebuild the image (code changes are
   baked in).
4. Regenerate: `POST /api/wiki_jobs` with the bv401 repo info (owner `poc`, repo
   `code1_cbl_bv401`, type `gitlab`, repoUrl
   `https://gitlab.reslv.one/poc/code1_cbl_bv401.git`, token for the private
   repo), `provider=claude_cli`, `model=claude-sonnet-4-6`, `language=zh-tw`,
   `force_regenerate=true`. Poll the job to completion.

The new cache key is distinct
(`deepwiki_cache_gitlab_poc_code1_cbl_bv401_zh-tw~claude-cli~claude-sonnet-4-6.json`
— note `claude_cli` → `claude-cli`: `sanitize_version_segment` in `api/api.py`
replaces underscores with dashes), so the existing Haiku wiki is left intact.

## Risks / assumptions

- **Premise risk:** if `claude -p` shares the same premium cap that 429s the
  SDK, there is no benefit. A host `claude -p --model claude-sonnet-4-6` ping
  already succeeds (verified during design review), but a trivial ping is not a
  sustained-load test — 23 real page prompts may still hit a cap. The step-0
  smoke test (and the JSON `is_error` detection) catch this before/early in the
  regen rather than after a wasted rebuild.
- **ELF compatibility:** the host native binary may not run on the slim Debian
  image; fallback is the npm install (see Component 4).
- **Embedder availability:** RAG retrieval needs the vLLM embedder reachable.
- **Concurrency:** 2 job workers → up to 2 concurrent `claude -p` processes; the
  subscription may throttle. Drop `WIKI_JOBS_CONCURRENCY` to 1 if 429s appear
  under concurrency.
- **Rebuild + version bump** required for the code changes.
- **Output-fidelity divergence from the SDK path.** On the SDK path the
  analyst system prompt is sent as the real `system` role; via `claude -p` the
  whole assembled envelope is piped as the **user** message and the CLI prepends
  its own Claude Code agent system prompt (smoke test showed ~25k cache tokens
  of CLI system/tooling context on a trivial call). Nothing crashes, but tone /
  formatting can differ from the SDK output. Acceptable for now; revisit with
  `--append-system-prompt` if the generated wikis read off. `wiki_generator`
  treats prompt fidelity as its acceptance criterion, so flagging explicitly.
- **Frontend exposure.** `/models/config` (`api/api.py:233-255`) iterates ALL
  `configs["providers"]`, so `claude_cli` automatically becomes a
  user-selectable provider in the model-selector UI, displayed as the
  capitalized id ("Claude_cli"). This is acceptable for this single-tenant ops
  deployment; the display name is cosmetic. If it should be ops-only, hiding it
  from `/models/config` is a separate change (not in scope here).

## Testing

Unit tests mirroring `tests/unit/test_claude_client.py`:

- `run_claude_cli` parsing: fake subprocess returning well-formed JSON →
  correct (text, in, out); nonzero exit / bad JSON / `is_error: true` (or
  `subtype != "success"`) in an exit-0 payload → raises.
- `llm_dispatch.generate("claude_cli", ...)`: monkeypatch `run_claude_cli`,
  assert it returns the expected `LLMResult` without touching the network.
