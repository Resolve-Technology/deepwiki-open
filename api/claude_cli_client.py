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
