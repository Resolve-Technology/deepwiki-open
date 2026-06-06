"""Tests for api/llm_dispatch.py — monkeypatched clients, no network."""
import asyncio
from types import SimpleNamespace

import pytest

import api.llm_dispatch as llm_dispatch
from api.llm_dispatch import LLMResult, generate


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def chunk(content):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=content))])


def chunk_no_choices():
    # vLLM's trailing usage chunk has empty choices
    return SimpleNamespace(choices=[])


class FakeClient:
    """Stands in for AnthropicClient / VLLMClient."""
    instances = []
    # vLLM streams end with an empty-choices usage chunk; Anthropic shim
    # chunks always carry exactly one choice.
    _yield_empty_chunk = False

    def __init__(self, *args, **kwargs):
        self.init_kwargs = kwargs
        self.last_usage = None
        self.seen = {}
        FakeClient.instances.append(self)

    def convert_inputs_to_api_kwargs(self, input=None, model_kwargs=None, model_type=None):
        self.seen["input"] = input
        self.seen["model_kwargs"] = dict(model_kwargs or {})
        return {"converted": True}

    async def acall(self, api_kwargs=None, model_type=None):
        self.seen["api_kwargs"] = api_kwargs

        async def stream():
            yield chunk("Hello ")
            yield chunk(None)
            yield chunk("world")
            if self._yield_empty_chunk:
                yield chunk_no_choices()
            self.last_usage = self._usage
        return stream()


@pytest.fixture(autouse=True)
def fixed_model_config(monkeypatch):
    FakeClient.instances = []
    FakeClient._yield_empty_chunk = False
    monkeypatch.setattr(
        llm_dispatch, "get_model_config",
        lambda provider, model: {"model_kwargs": {"temperature": 0.6, "top_p": 0.9,
                                                  "max_tokens": 9000,
                                                  "thinking": "adaptive"}})


def test_claude_branch(monkeypatch):
    monkeypatch.setattr(llm_dispatch, "AnthropicClient", FakeClient)
    FakeClient._usage = SimpleNamespace(input_tokens=11, output_tokens=22)

    result = run(generate("claude", "claude-opus-4-8", "PROMPT"))

    assert result == LLMResult("Hello world", 11, 22)
    client = FakeClient.instances[0]
    assert client.seen["input"] == "PROMPT"
    assert client.seen["model_kwargs"] == {
        "model": "claude-opus-4-8", "temperature": 0.6, "top_p": 0.9,
        "max_tokens": 9000, "thinking": "adaptive"}


def test_claude_branch_minimal_config(monkeypatch):
    monkeypatch.setattr(llm_dispatch, "AnthropicClient", FakeClient)
    monkeypatch.setattr(llm_dispatch, "get_model_config",
                        lambda p, m: {"model_kwargs": {}})
    FakeClient._usage = None  # stream never produced usage

    result = run(generate("claude", "m", "P"))

    assert result == LLMResult("Hello world", 0, 0)
    assert FakeClient.instances[0].seen["model_kwargs"] == {"model": "m"}


def test_vllm_branch_with_route(monkeypatch):
    monkeypatch.setattr(llm_dispatch, "VLLMClient", FakeClient)
    monkeypatch.setattr(llm_dispatch, "get_vllm_route",
                        lambda model: "http://10.0.0.5:8005")
    FakeClient._usage = SimpleNamespace(prompt_tokens=33, completion_tokens=44)
    FakeClient._yield_empty_chunk = True

    result = run(generate("vllm", "gemma", "PROMPT"))

    assert result == LLMResult("Hello world", 33, 44)
    client = FakeClient.instances[0]
    assert client.init_kwargs == {"base_url": "http://10.0.0.5:8005"}
    assert client.seen["model_kwargs"] == {
        "model": "gemma", "stream": True, "temperature": 0.6, "top_p": 0.9}


def test_vllm_branch_without_route(monkeypatch):
    monkeypatch.setattr(llm_dispatch, "VLLMClient", FakeClient)
    monkeypatch.setattr(llm_dispatch, "get_vllm_route", lambda model: None)
    scans = []

    async def fake_scan():
        scans.append(True)
        return []
    monkeypatch.setattr(llm_dispatch, "get_vllm_models", fake_scan)
    monkeypatch.setattr(llm_dispatch, "get_model_config",
                        lambda p, m: {"model_kwargs": {"temperature": 0.5}})
    FakeClient._usage = None

    result = run(generate("vllm", "gemma", "P"))

    assert result.text == "Hello world"
    assert scans == [True]  # a route miss triggers a discovery refresh
    client = FakeClient.instances[0]
    assert client.init_kwargs == {}  # default base URL
    assert client.seen["model_kwargs"] == {
        "model": "gemma", "stream": True, "temperature": 0.5}  # no top_p


def test_vllm_route_found_after_refresh(monkeypatch):
    monkeypatch.setattr(llm_dispatch, "VLLMClient", FakeClient)
    routes = iter([None, "http://10.0.0.9:8001"])
    monkeypatch.setattr(llm_dispatch, "get_vllm_route", lambda model: next(routes))

    async def fake_scan():
        return ["gemma"]
    monkeypatch.setattr(llm_dispatch, "get_vllm_models", fake_scan)
    FakeClient._usage = None

    run(generate("vllm", "gemma", "P"))

    assert FakeClient.instances[0].init_kwargs == {"base_url": "http://10.0.0.9:8001"}


def test_unsupported_provider():
    with pytest.raises(ValueError, match="does not support provider 'google'"):
        run(generate("google", "gemini", "P"))
