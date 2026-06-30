# claude_cli Generation Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a reusable `claude_cli` generation provider that routes wiki-generation LLM calls through the `claude -p` CLI (Claude Code subscription quota) instead of the Anthropic SDK OAuth path, then regenerate the bv401 wiki with Sonnet.

**Architecture:** A new async helper shells out to `claude -p --output-format json` and returns `(text, input_tokens, output_tokens)`. `llm_dispatch.generate` gets a `claude_cli` branch that calls it. A `generator.json` provider entry makes it selectable. docker-compose mounts the host's native `claude` binary + `~/.claude` credentials into the container. The existing job queue drives the actual regeneration unchanged.

**Tech Stack:** Python 3.12, asyncio subprocess, FastAPI, pytest 9 (run via `.venv/bin/python -m pytest`), Docker Compose. Spec: `docs/superpowers/specs/2026-06-30-claude-cli-generation-provider-design.md`.

## Global Constraints

- Generation path only — do NOT touch `simple_chat.py` / `websocket_wiki.py` (chat) for `claude_cli`.
- The `claude -p` invocation MUST include `--safe-mode --tools ""` (and `--strict-mcp-config`) — prevents the mounted host `~/.claude` (CLAUDE.md, settings.json, auto-memory) from contaminating prompts and blocks filesystem/tool access. Never set `CLAUDE_CODE_SIMPLE` (it disables OAuth → breaks subscription auth).
- The CLI exits 0 even on API errors; detect failure via JSON `is_error` / `subtype != "success"`, NOT stderr alone.
- Mount `~/.claude` **read-write** (OAuth token refresh + history writes); mount the binary dir **read-only**.
- Tests must not hit the network or spawn the real CLI — monkeypatch `asyncio.create_subprocess_exec` and `run_claude_cli`.
- Test runner: `.venv/bin/python -m pytest <path> -v` from `/home/ubuntu/deepwiki-open`.
- Bump `src/version.ts` `APP_VERSION` before any image rebuild.
- Branch: `feat/claude-cli-generation-provider` (already checked out).

---

### Task 1: `run_claude_cli` subprocess helper

**Files:**
- Create: `api/claude_cli_client.py`
- Test: `tests/unit/test_claude_cli_client.py`

**Interfaces:**
- Consumes: nothing (leaf module).
- Produces:
  - `async def run_claude_cli(model: str, prompt: str, *, timeout: float = ...) -> tuple[str, int, int]` returning `(text, input_tokens, output_tokens)`.
  - `class ClaudeCLIError(RuntimeError)` raised on any failure.
  - Module constant `CLAUDE_CLI_BIN` (env `CLAUDE_CLI_BIN`, default `"claude"`).

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_claude_cli_client.py`:

```python
"""Tests for api/claude_cli_client.py — fake subprocess, no real CLI."""
import asyncio
import json

import pytest

import api.claude_cli_client as cli
from api.claude_cli_client import ClaudeCLIError, run_claude_cli


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class FakeProc:
    def __init__(self, stdout=b"", stderr=b"", returncode=0):
        self._stdout, self._stderr, self.returncode = stdout, stderr, returncode
        self.stdin_written = None
        self.killed = False

    async def communicate(self, input=None):
        self.stdin_written = input
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


@pytest.fixture
def fake_exec(monkeypatch):
    """Patch create_subprocess_exec; capture argv/kwargs, return a configured proc."""
    state = {"proc": FakeProc(), "cmd": None, "kwargs": None}

    async def _exec(*args, **kwargs):
        state["cmd"] = list(args)
        state["kwargs"] = kwargs
        return state["proc"]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    return state


def _ok_payload(result="# Wiki\nbody", in_tok=120, out_tok=45):
    return json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": result,
        "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
    }).encode()


def test_success_parses_text_and_usage(fake_exec):
    fake_exec["proc"] = FakeProc(stdout=_ok_payload())
    text, in_tok, out_tok = run(run_claude_cli("claude-sonnet-4-6", "PROMPT"))
    assert text == "# Wiki\nbody"
    assert (in_tok, out_tok) == (120, 45)


def test_prompt_goes_to_stdin_and_argv_has_isolation_flags(fake_exec):
    fake_exec["proc"] = FakeProc(stdout=_ok_payload())
    run(run_claude_cli("claude-opus-4-8", "THE PROMPT"))
    assert fake_exec["proc"].stdin_written == b"THE PROMPT"
    cmd = fake_exec["cmd"]
    assert cmd[0] == cli.CLAUDE_CLI_BIN
    assert "-p" in cmd
    assert cmd[cmd.index("--model") + 1] == "claude-opus-4-8"
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert "--safe-mode" in cmd
    assert "--strict-mcp-config" in cmd
    assert cmd[cmd.index("--tools") + 1] == ""


def test_custom_bin_via_env(monkeypatch, fake_exec):
    monkeypatch.setattr(cli, "CLAUDE_CLI_BIN", "/opt/claude-cli/versions/2.1.196")
    fake_exec["proc"] = FakeProc(stdout=_ok_payload())
    run(run_claude_cli("claude-sonnet-4-6", "P"))
    assert fake_exec["cmd"][0] == "/opt/claude-cli/versions/2.1.196"


def test_nonzero_exit_raises(fake_exec):
    fake_exec["proc"] = FakeProc(stdout=b"", stderr=b"boom", returncode=1)
    with pytest.raises(ClaudeCLIError, match="exited 1"):
        run(run_claude_cli("m", "P"))


def test_non_json_output_raises(fake_exec):
    fake_exec["proc"] = FakeProc(stdout=b"not json at all")
    with pytest.raises(ClaudeCLIError, match="non-JSON"):
        run(run_claude_cli("m", "P"))


def test_is_error_payload_raises_even_on_exit_zero(fake_exec):
    payload = json.dumps({
        "type": "result", "subtype": "error_during_execution", "is_error": True,
        "api_error_status": 429, "result": "",
    }).encode()
    fake_exec["proc"] = FakeProc(stdout=payload, returncode=0)
    with pytest.raises(ClaudeCLIError, match="reported error"):
        run(run_claude_cli("m", "P"))


def test_missing_result_raises(fake_exec):
    payload = json.dumps({"subtype": "success", "is_error": False,
                          "usage": {"input_tokens": 1, "output_tokens": 2}}).encode()
    fake_exec["proc"] = FakeProc(stdout=payload)
    with pytest.raises(ClaudeCLIError, match="missing 'result'"):
        run(run_claude_cli("m", "P"))


def test_timeout_kills_proc(monkeypatch, fake_exec):
    async def _slow_wait_for(coro, timeout):
        coro.close()  # avoid "never awaited" warning
        raise asyncio.TimeoutError()
    monkeypatch.setattr(cli.asyncio, "wait_for", _slow_wait_for)
    with pytest.raises(ClaudeCLIError, match="timed out"):
        run(run_claude_cli("m", "P", timeout=0.01))
    assert fake_exec["proc"].killed is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_claude_cli_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'api.claude_cli_client'`

- [ ] **Step 3: Write the implementation**

Create `api/claude_cli_client.py`:

```python
"""Drive the `claude -p` CLI as a wiki-generation backend.

Routes generation through the Claude Code subscription quota instead of the
Anthropic OpenAI-compatible OAuth endpoint, which 429s on Sonnet/Opus. Pure
text generation only: tools, MCP, and project context (CLAUDE.md / settings /
auto-memory from the mounted host ~/.claude) are disabled so the CLI cannot
touch the filesystem or contaminate the prompt.
"""
import asyncio
import json
import logging
import os

log = logging.getLogger(__name__)

# Path to the claude binary. In the container this points at the mounted native
# binary (see docker-compose.yml); the default suits a host with claude on PATH.
CLAUDE_CLI_BIN = os.getenv("CLAUDE_CLI_BIN", "claude")
# A single page can take minutes; allow a generous, env-tunable ceiling.
CLAUDE_CLI_TIMEOUT = float(os.getenv("CLAUDE_CLI_TIMEOUT", "600"))


class ClaudeCLIError(RuntimeError):
    """Raised when `claude -p` fails to run or reports an error payload."""


async def run_claude_cli(model: str, prompt: str, *,
                         timeout: float = CLAUDE_CLI_TIMEOUT) -> tuple[str, int, int]:
    """Run `claude -p` for one prompt; return (text, input_tokens, output_tokens).

    The CLI exits 0 even on API errors and signals them inside the JSON, so we
    inspect ``is_error`` / ``subtype`` rather than trusting the exit code alone.
    """
    cmd = [
        CLAUDE_CLI_BIN, "-p",
        "--model", model,
        "--output-format", "json",
        "--safe-mode",          # no CLAUDE.md/skills/hooks/MCP/commands; keeps OAuth
        "--strict-mcp-config",  # ignore any discovered MCP servers
        "--tools", "",          # disable built-in tools safe-mode leaves on
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=prompt.encode()), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ClaudeCLIError(f"claude -p timed out after {timeout}s")

    if proc.returncode != 0:
        raise ClaudeCLIError(
            f"claude -p exited {proc.returncode}: "
            f"{stderr.decode(errors='replace')[:500]}")

    try:
        payload = json.loads(stdout.decode())
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ClaudeCLIError(f"claude -p returned non-JSON output: {e}") from e

    if payload.get("is_error") or payload.get("subtype") not in (None, "success"):
        raise ClaudeCLIError(
            f"claude -p reported error: subtype={payload.get('subtype')} "
            f"status={payload.get('api_error_status')}")

    text = payload.get("result")
    if not isinstance(text, str):
        raise ClaudeCLIError("claude -p JSON missing 'result' string")

    usage = payload.get("usage") or {}
    in_tok = int(usage.get("input_tokens", 0) or 0)
    out_tok = int(usage.get("output_tokens", 0) or 0)
    log.info(f"Claude CLI usage: model={model} input_tokens={in_tok} "
             f"output_tokens={out_tok}")
    return text, in_tok, out_tok
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_claude_cli_client.py -v`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add api/claude_cli_client.py tests/unit/test_claude_cli_client.py
git commit -m "feat: add run_claude_cli subprocess helper for claude -p generation"
```

---

### Task 2: `claude_cli` branch in `llm_dispatch.generate`

**Files:**
- Modify: `api/llm_dispatch.py` (add import + branch before the final `raise`)
- Test: `tests/unit/test_llm_dispatch.py` (add cases)

**Interfaces:**
- Consumes: `run_claude_cli(model, prompt) -> (text, in, out)` from Task 1; `LLMResult` (existing).
- Produces: `generate("claude_cli", model, prompt) -> LLMResult` (non-streaming).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_llm_dispatch.py`:

```python
def test_claude_cli_branch(monkeypatch):
    calls = {}

    async def fake_run(model, prompt, **kwargs):
        calls["model"] = model
        calls["prompt"] = prompt
        return ("# Generated\nbody", 77, 88)

    monkeypatch.setattr(llm_dispatch, "run_claude_cli", fake_run)

    result = run(generate("claude_cli", "claude-sonnet-4-6", "PROMPT"))

    assert result == LLMResult("# Generated\nbody", 77, 88)
    assert calls == {"model": "claude-sonnet-4-6", "prompt": "PROMPT"}


def test_claude_cli_branch_propagates_errors(monkeypatch):
    from api.claude_cli_client import ClaudeCLIError

    async def fake_run(model, prompt, **kwargs):
        raise ClaudeCLIError("claude -p reported error: subtype=error status=429")

    monkeypatch.setattr(llm_dispatch, "run_claude_cli", fake_run)

    with pytest.raises(ClaudeCLIError, match="status=429"):
        run(generate("claude_cli", "claude-sonnet-4-6", "P"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_llm_dispatch.py -k claude_cli -v`
Expected: FAIL — `AttributeError: module 'api.llm_dispatch' has no attribute 'run_claude_cli'`

- [ ] **Step 3: Add the import and branch**

In `api/llm_dispatch.py`, add to the imports near the top (after the existing `from api.vllm_discovery import ...` line):

```python
from api.claude_cli_client import run_claude_cli
```

Then, immediately **before** the final `raise ValueError(f"Server-side generation does not support provider {provider!r} yet")`, add:

```python
    if provider == "claude_cli":
        logger.info(f"Dispatching to Claude via CLI (claude -p) model: {model}")
        text, in_tok, out_tok = await run_claude_cli(model, prompt)
        return LLMResult(text, in_tok, out_tok)

```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_llm_dispatch.py -v`
Expected: PASS (existing 6 + 2 new = 8 passed)

- [ ] **Step 5: Commit**

```bash
git add api/llm_dispatch.py tests/unit/test_llm_dispatch.py
git commit -m "feat: route claude_cli provider through run_claude_cli in llm_dispatch"
```

---

### Task 3: Register the `claude_cli` provider in generator.json

**Files:**
- Modify: `api/config/generator.json` (add `claude_cli` provider object)
- Test: `tests/unit/test_claude_cli_provider_config.py` (create)

**Interfaces:**
- Consumes: `get_model_config` and `configs` from `api/config.py` (existing).
- Produces: provider `claude_cli` resolvable via `get_model_config("claude_cli", "claude-sonnet-4-6")`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_claude_cli_provider_config.py`:

```python
"""The claude_cli provider must be registered and resolvable in config."""
from api.config import configs, get_model_config


def test_claude_cli_provider_registered():
    assert "claude_cli" in configs["providers"]
    provider = configs["providers"]["claude_cli"]
    assert provider["default_model"] == "claude-sonnet-4-6"
    assert "claude-sonnet-4-6" in provider["models"]


def test_get_model_config_resolves_claude_cli():
    cfg = get_model_config("claude_cli", "claude-sonnet-4-6")
    # client_class: "ClaudeClient" maps to a real model_client so this never raises
    assert cfg["model_client"].__name__ == "ClaudeClient"
    assert cfg["model_kwargs"]["model"] == "claude-sonnet-4-6"


def test_get_model_config_claude_cli_default_model():
    cfg = get_model_config("claude_cli")  # None model -> default_model
    assert cfg["model_kwargs"]["model"] == "claude-sonnet-4-6"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_claude_cli_provider_config.py -v`
Expected: FAIL — `assert 'claude_cli' in configs["providers"]` fails (KeyError / AssertionError)

- [ ] **Step 3: Add the provider entry**

In `api/config/generator.json`, inside the `"providers"` object, add this entry immediately after the closing brace of the existing `"claude": { ... }` block (and before `"ollama"`). Add a comma after the `claude` block's closing brace:

```json
    "claude_cli": {
      "client_class": "ClaudeClient",
      "default_model": "claude-sonnet-4-6",
      "supportsCustomModel": true,
      "models": {
        "claude-sonnet-4-6": {
          "max_tokens": 64000
        },
        "claude-opus-4-8": {
          "max_tokens": 100000
        },
        "claude-haiku-4-5-20251001": {
          "max_tokens": 64000
        }
      }
    },
```

- [ ] **Step 4: Run test to verify it passes (and JSON is valid)**

Run: `.venv/bin/python -c "import json; json.load(open('api/config/generator.json')); print('json ok')"`
Expected: `json ok`

Run: `.venv/bin/python -m pytest tests/unit/test_claude_cli_provider_config.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add api/config/generator.json tests/unit/test_claude_cli_provider_config.py
git commit -m "feat: register claude_cli provider in generator.json"
```

---

### Task 4: Mount the claude CLI + credentials into the container

**Files:**
- Modify: `docker-compose.yml` (add 2 volumes + 1 env var)

**Interfaces:**
- Consumes: `CLAUDE_CLI_BIN` (read by Task 1's module).
- Produces: a container where `run_claude_cli` can execute the real CLI with subscription auth.

> No unit test — this is infra, verified by the in-container smoke test in Task 5. Do the edit and commit; the runtime check lives in Task 5 (gated).

- [ ] **Step 1: Add the volumes and env var**

In `docker-compose.yml`, extend the `volumes:` list (currently lines 19-21) to:

```yaml
    volumes:
      - ~/.adalflow:/root/.adalflow      # Persist repository and embedding data
      - ./api/logs:/app/api/logs          # Persist log files across container restarts
      - ~/.local/share/claude:/opt/claude-cli:ro   # claude CLI native binary (read-only)
      - ~/.claude:/root/.claude                     # Claude Code subscription creds (RW: OAuth token refresh + history writes)
```

And add to the `environment:` list (after the `DEEPWIKI_PROMPT_TOKEN_BUDGET` line):

```yaml
      - CLAUDE_CLI_BIN=${CLAUDE_CLI_BIN:-/opt/claude-cli/versions/2.1.196}  # mounted native binary; bump version when the host CLI updates
```

> Note: the version segment (`2.1.196`) tracks the host install at `~/.local/share/claude/versions/`. If the host CLI auto-updates, update this path (or override `CLAUDE_CLI_BIN` in `.env`). Confirm the current version with `ls ~/.local/share/claude/versions/`.

- [ ] **Step 2: Validate compose syntax**

Run: `docker compose config >/dev/null && echo "compose ok"`
Expected: `compose ok` (no YAML/interpolation errors)

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml
git commit -m "chore: mount claude CLI binary + ~/.claude creds for claude_cli provider"
```

---

### Task 5: Deploy, smoke-gate, and regenerate the bv401 wiki

**Files:**
- Modify: `src/version.ts` (bump `APP_VERSION`)
- Create (scratch, not committed): a one-off submit script

**Interfaces:**
- Consumes: everything from Tasks 1-4; the existing `POST /api/wiki_jobs` endpoint and job queue.
- Produces: a new cache file `deepwiki_cache_gitlab_poc_code1_cbl_bv401_zh-tw~claude-cli~claude-sonnet-4-6.json`.

> This task is operational, not TDD. The smoke test in Step 3 is a HARD GATE: if it shows a 429/rate-limit, STOP — the premise (CLI dodges the SDK 429) is false and there is nothing to gain from regenerating.

- [ ] **Step 1: Bump the app version**

Edit `src/version.ts`: increment `APP_VERSION` (e.g. patch bump). Then:

```bash
git add src/version.ts
git commit -m "chore: bump APP_VERSION for claude_cli provider"
```

- [ ] **Step 2: Rebuild and restart the container**

Run:
```bash
docker compose up -d --build
```
Expected: build succeeds; `deepwiki` container is `Up` / healthy. Verify:
```bash
docker compose ps
```

- [ ] **Step 3: HARD GATE — smoke-test `claude -p` Sonnet inside the container**

Run:
```bash
docker compose exec deepwiki /opt/claude-cli/versions/2.1.196 -p \
  --model claude-sonnet-4-6 --output-format json \
  --safe-mode --strict-mcp-config --tools "" \
  "Reply with exactly the word: pong"
```
Expected: a JSON object with `"is_error": false`, `"subtype": "success"`, and `"result"` containing `pong`.

- If `is_error` is `true` or `api_error_status` is `429`: **STOP**. The CLI hits the same cap as the SDK; do not proceed. Report this and revisit the approach (e.g. Haiku-only, or different auth).
- If the binary path errors (`no such file`): run `docker compose exec deepwiki ls /opt/claude-cli/versions/` and update `CLAUDE_CLI_BIN` / the docker-compose path to the actual version, then `docker compose up -d` and retry.
- If it errors about credentials/login: confirm `~/.claude/.credentials.json` exists on the host and the `~/.claude` mount is read-write.

- [ ] **Step 4: Verify the embedder is reachable (RAG dependency)**

Generation needs the vLLM embedder. Confirm `VLLM_EMBEDDER_BASE_URL` in `.env` points at the current embedder port (it has moved before; all-citations-unverified is the outage signature). Quick check:
```bash
docker compose exec deepwiki sh -c 'curl -fsS "$VLLM_EMBEDDER_BASE_URL/models" >/dev/null && echo embedder-ok || echo embedder-UNREACHABLE'
```
Expected: `embedder-ok`. If unreachable, fix `VLLM_EMBEDDER_BASE_URL` before regenerating (otherwise the wiki will be poisoned with unverified citations).

- [ ] **Step 5: Submit the regeneration job**

Reuses the bv401 repo info (including the private-GitLab token) from the existing Haiku cache file so the token never lands in shell history. Write `scratch_submit_bv401.py` (do NOT commit it):

```python
import glob, json, urllib.request

cache = glob.glob("/home/ubuntu/.adalflow/wikicache/*bv401*claude-haiku*")[0]
repo = json.load(open(cache))["repo"]

payload = {
    "repo": {
        "owner": repo["owner"],
        "repo": repo["repo"],
        "type": repo["type"],
        "repoUrl": repo["repoUrl"],
        "token": repo.get("token"),
    },
    "language": "zh-tw",
    "provider": "claude_cli",
    "model": "claude-sonnet-4-6",
    "force_regenerate": True,
}
req = urllib.request.Request(
    "http://localhost:8001/api/wiki_jobs",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
)
print(urllib.request.urlopen(req).read().decode())
```

Run:
```bash
.venv/bin/python scratch_submit_bv401.py
```
Expected: JSON with a job `id` and `status` `queued` (or `running`). Note the `id`.

> If the API port differs, confirm with `grep PORT .env` (default 8001).

- [ ] **Step 6: Poll the job to completion**

Run (substitute the job id):
```bash
curl -s http://localhost:8001/api/wiki_jobs/<JOB_ID> | .venv/bin/python -m json.tool
```
Repeat until `status` is `completed` (watch `progress` / `stats`). Watch for `claude_cli` 429s surfacing as job failure:
```bash
docker compose logs --tail=50 deepwiki | grep -i "claude cli\|claude -p\|reported error"
```
- If the job FAILS with a CLI rate-limit under load (even though the Step-3 ping passed): lower `WIKI_JOBS_CONCURRENCY` to `1` in `.env`, `docker compose up -d`, and resubmit.

- [ ] **Step 7: Verify the new wiki cache was written**

Run:
```bash
ls -la /home/ubuntu/.adalflow/wikicache/*bv401*claude-cli*
```
Expected: `deepwiki_cache_gitlab_poc_code1_cbl_bv401_zh-tw~claude-cli~claude-sonnet-4-6.json` exists, recent timestamp. The original `~claude~claude-haiku-...` file is untouched.

- [ ] **Step 8: Clean up the scratch script**

```bash
rm scratch_submit_bv401.py
```

---

## Self-Review notes

- **Spec coverage:** Component 1 → Task 1; Component 2 → Task 2; Component 3 → Task 3; Component 4 (mounts) → Task 4; Execution plan (smoke gate, embedder check, version bump, regen) → Task 5. Risks (premise/429, embedder, concurrency, fidelity) are encoded as gates/fallbacks in Task 5. Frontend exposure (accepted) needs no task. ✓
- **Type consistency:** `run_claude_cli(model, prompt) -> (str, int, int)` defined in Task 1, consumed identically in Task 2; `ClaudeCLIError` raised in Task 1 and asserted in Task 2. `get_model_config`/`configs` used per their real signatures. ✓
- **No placeholders:** all code blocks complete; all commands have expected output. ✓
