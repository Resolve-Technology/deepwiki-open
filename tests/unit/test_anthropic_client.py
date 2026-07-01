"""Tests for the native Anthropic SDK client (api/anthropic_client.py)."""
import pytest
from adalflow.core.types import ModelType

from api.anthropic_client import AnthropicClient, split_prompt


# --- split_prompt ---

def test_split_extracts_system_and_user():
    prompt = (
        "/no_think <role>You are a code wiki writer.</role>\n\n"
        "<conversation_history>\nuser: hi\n</conversation_history>\n\n"
        "<query>\nDescribe the architecture\n</query>\n\nAssistant: "
    )
    system, user = split_prompt(prompt)
    assert system == "<role>You are a code wiki writer.</role>"
    assert user.startswith("<conversation_history>")
    assert user.endswith("</query>")
    assert "/no_think" not in user
    assert "Assistant:" not in user


def test_split_cuts_at_earliest_marker():
    prompt = (
        "sys part\n\n"
        "<currentFileContent path=\"a.py\">\ncode\n</currentFileContent>\n\n"
        "<query>\nq\n</query>\n\nAssistant: "
    )
    system, user = split_prompt(prompt)
    assert system == "sys part"
    assert user.startswith("<currentFileContent")


def test_split_without_markers_is_all_user():
    system, user = split_prompt("/no_think just review this wiki please")
    assert system == ""
    assert user == "just review this wiki please"


def test_split_handles_empty():
    assert split_prompt("") == ("", "")


def test_split_strips_trailing_no_think_suffix():
    # The size-fallback path appends "/no_think" after "Assistant: "
    prompt = "sys\n\n<query>\nq\n</query>\n\nAssistant:  /no_think"
    system, user = split_prompt(prompt)
    assert system == "sys"
    assert user.endswith("</query>")
    assert "/no_think" not in user
    assert "Assistant:" not in user


# --- convert_inputs_to_api_kwargs ---

def make_prompt():
    return "/no_think sys\n\n<query>\nq\n</query>\n\nAssistant: "


def test_convert_builds_native_kwargs_with_cached_system():
    client = AnthropicClient(auth_token="sk-ant-oat01-test")
    kwargs = client.convert_inputs_to_api_kwargs(
        input=make_prompt(),
        model_kwargs={"model": "claude-sonnet-4-6", "stream": True,
                      "temperature": 0.3, "max_tokens": 64000},
        model_type=ModelType.LLM,
    )
    assert kwargs["model"] == "claude-sonnet-4-6"
    assert kwargs["max_tokens"] == 64000
    assert kwargs["temperature"] == 0.3          # passthrough kept
    assert "stream" not in kwargs                # stream helper handles it
    assert kwargs["messages"] == [{"role": "user", "content": "<query>\nq\n</query>"}]
    assert kwargs["system"][0]["text"] == "sys"
    assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_convert_omits_system_when_prompt_has_no_markers():
    client = AnthropicClient(auth_token="sk-ant-oat01-test")
    kwargs = client.convert_inputs_to_api_kwargs(
        input="plain question",
        model_kwargs={"model": "claude-opus-4-8"},
        model_type=ModelType.LLM,
    )
    assert "system" not in kwargs
    assert kwargs["messages"] == [{"role": "user", "content": "plain question"}]
    assert kwargs["max_tokens"] == 16000  # default


def test_convert_thinking_adaptive_flag():
    client = AnthropicClient(auth_token="sk-ant-oat01-test")
    kwargs = client.convert_inputs_to_api_kwargs(
        input=make_prompt(),
        model_kwargs={"model": "claude-opus-4-8", "max_tokens": 100000,
                      "thinking": "adaptive"},
        model_type=ModelType.LLM,
    )
    assert kwargs["thinking"] == {"type": "adaptive"}
    assert "temperature" not in kwargs


def test_convert_rejects_non_llm():
    client = AnthropicClient(auth_token="sk-ant-oat01-test")
    with pytest.raises(ValueError):
        client.convert_inputs_to_api_kwargs(
            input="x", model_kwargs={"model": "m"}, model_type=ModelType.EMBEDDER,
        )


def test_missing_token_raises():
    client = AnthropicClient()
    # only raises when the client is actually constructed
    import os
    saved = os.environ.pop("CLAUDE_OAUTH_TOKEN", None)
    try:
        with pytest.raises(ValueError):
            client.init_async_client()
    finally:
        if saved is not None:
            os.environ["CLAUDE_OAUTH_TOKEN"] = saved


def test_base_url_strips_v1_from_compat_var(monkeypatch):
    monkeypatch.delenv("CLAUDE_NATIVE_API_BASE_URL", raising=False)
    monkeypatch.setenv("CLAUDE_API_BASE_URL", "https://relay.example.com/v1")
    client = AnthropicClient(auth_token="sk-ant-oat01-test")
    assert client._resolve_base_url() == "https://relay.example.com"


# --- usage marker ---

def test_format_usage_marker_roundtrip():
    from api.anthropic_client import format_usage_marker
    import json as _json
    import re as _re
    marker = format_usage_marker(445359, 255929)
    # Same regex shape the frontend uses to strip it
    m = _re.search(r"\n?<<<USAGE_JSON:(\{.*\})>>>\s*$", marker)
    assert m
    payload = _json.loads(m.group(1))
    assert payload == {"input_tokens": 445359, "output_tokens": 255929}


def test_convert_pops_usage_marker_flag():
    client = AnthropicClient(auth_token="sk-ant-oat01-test")
    kwargs = client.convert_inputs_to_api_kwargs(
        input=make_prompt(),
        model_kwargs={"model": "claude-haiku-4-5-20251001", "max_tokens": 64000,
                      "include_usage_marker": True},
        model_type=ModelType.LLM,
    )
    assert kwargs["_include_usage_marker"] is True
    assert "include_usage_marker" not in kwargs  # never reaches the API params


# --- api-key auth mode ---

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
