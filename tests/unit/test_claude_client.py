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
    assert (
        "vLLM usage: model=some-model prompt_tokens=10 completion_tokens=5 total_tokens=15"
        in caplog.text
    )


def test_usage_log_prefix_is_overridable(caplog):
    class Sub(VLLMClient):
        usage_log_prefix = "Claude usage"

    client = Sub.__new__(Sub)
    with caplog.at_level(logging.INFO, logger="api.vllm_client"):
        client._log_usage(FakeUsage(), "claude-sonnet-4-6")
    assert (
        "Claude usage: model=claude-sonnet-4-6 prompt_tokens=10 completion_tokens=5 total_tokens=15"
        in caplog.text
    )


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


def test_async_client_gets_token_and_beta_header(monkeypatch, captured_openai):
    monkeypatch.delenv("CLAUDE_API_BASE_URL", raising=False)
    monkeypatch.setenv("CLAUDE_OAUTH_TOKEN", "sk-ant-oat01-test")
    client = ClaudeClient()
    captured_openai.clear()  # discard kwargs captured by the eager sync init
    client.init_async_client()
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
