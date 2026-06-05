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
