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
