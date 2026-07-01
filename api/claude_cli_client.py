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
import re

log = logging.getLogger(__name__)

# The shared prompt envelope (api/prompt_assembly.py) prepends the vLLM "/no_think"
# directive, which Claude's SDK ignores as plain text. But `claude -p` parses a
# leading "/no_think" as a slash command and answers "Unknown command: /no_think"
# with zero model output — so strip it for the CLI path. Anchored to the start so
# only a leading directive is removed, never "/no_think" appearing inside content.
_LEADING_NO_THINK = re.compile(r"^\s*/no_think\b[ \t]*")

# Path to the claude binary. In the container this points at the mounted native
# binary (see docker-compose.yml); the default suits a host with claude on PATH.
CLAUDE_CLI_BIN = os.getenv("CLAUDE_CLI_BIN", "claude")
# A single page can take minutes; allow a generous, env-tunable ceiling.
CLAUDE_CLI_TIMEOUT = float(os.getenv("CLAUDE_CLI_TIMEOUT", "600"))

# Concurrent `claude -p` invocations fail fast with EMPTY stdout+stderr and a
# nonzero exit: the CLI races on the shared ~/.claude config and/or the
# subscription's concurrent-request limit. A single call always succeeds, so we
# serialize the CLI path with a process-wide semaphore (default 1). Generation
# runs in one API process, so this reliably caps it; raise via env only if a
# higher concurrency is proven safe.
CLAUDE_CLI_MAX_CONCURRENCY = max(1, int(os.getenv("CLAUDE_CLI_MAX_CONCURRENCY", "1")))
# Even serialized, a call can fail transiently (API overload, a lost config
# race); retry a few times with backoff before giving up on the page.
CLAUDE_CLI_RETRIES = max(1, int(os.getenv("CLAUDE_CLI_RETRIES", "3")))

# Created lazily so it binds to the running event loop, not import-time state.
_cli_semaphore: "asyncio.Semaphore | None" = None


def _get_semaphore() -> "asyncio.Semaphore":
    global _cli_semaphore
    if _cli_semaphore is None:
        _cli_semaphore = asyncio.Semaphore(CLAUDE_CLI_MAX_CONCURRENCY)
    return _cli_semaphore


class ClaudeCLIError(RuntimeError):
    """Raised when `claude -p` fails to run or reports an error payload."""


async def run_claude_cli(model: str, prompt: str, *,
                         timeout: float = CLAUDE_CLI_TIMEOUT) -> tuple[str, int, int]:
    """Run `claude -p` for one prompt; return (text, input_tokens, output_tokens).

    Serialized against other CLI calls (see CLAUDE_CLI_MAX_CONCURRENCY) because
    concurrent invocations fail fast with empty output, and retried a few times
    on transient failure. Retries happen while holding the semaphore so a failing
    call never adds concurrency.
    """
    prompt = _LEADING_NO_THINK.sub("", prompt, count=1)
    async with _get_semaphore():
        last_err: "ClaudeCLIError | None" = None
        for attempt in range(1, CLAUDE_CLI_RETRIES + 1):
            try:
                return await _invoke_once(model, prompt, timeout)
            except ClaudeCLIError as e:
                last_err = e
                if attempt < CLAUDE_CLI_RETRIES:
                    backoff = min(2 ** attempt, 10)
                    log.warning(f"claude -p attempt {attempt}/{CLAUDE_CLI_RETRIES} "
                                f"failed ({e}); retrying in {backoff}s")
                    await asyncio.sleep(backoff)
        assert last_err is not None
        raise last_err


async def _invoke_once(model: str, prompt: str,
                       timeout: float) -> tuple[str, int, int]:
    """One `claude -p` run.

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
        # Concurrent-failure exits leave stderr empty and put any payload on
        # stdout, so surface both to keep the cause diagnosable.
        err = stderr.decode(errors="replace").strip()
        out = stdout.decode(errors="replace").strip()
        raise ClaudeCLIError(
            f"claude -p exited {proc.returncode}: "
            f"stderr={err[:400]!r} stdout={out[:400]!r}")

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
