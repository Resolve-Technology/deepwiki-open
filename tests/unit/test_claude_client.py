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
