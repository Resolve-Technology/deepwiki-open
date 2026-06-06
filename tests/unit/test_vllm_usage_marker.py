"""Tests for the vLLM usage marker (api/vllm_client.py)."""
import asyncio
import re
from types import SimpleNamespace

from adalflow.core.types import ModelType

from api.vllm_client import VLLMClient, _usage_marker_chunk


def make_client():
    return VLLMClient(api_key="dummy", base_url="http://example.invalid:1/v1")


def test_convert_pops_marker_flag_and_keeps_stream_options():
    client = make_client()
    kwargs = client.convert_inputs_to_api_kwargs(
        input="hello",
        model_kwargs={"model": "openai/gpt-oss-120b", "stream": True,
                      "temperature": 0.7, "include_usage_marker": True},
        model_type=ModelType.LLM,
    )
    assert kwargs["_include_usage_marker"] is True
    assert "include_usage_marker" not in kwargs       # never reaches the API
    assert kwargs["stream_options"] == {"include_usage": True}


def test_convert_without_flag_has_no_marker_key():
    client = make_client()
    kwargs = client.convert_inputs_to_api_kwargs(
        input="hello",
        model_kwargs={"model": "m", "stream": True},
        model_type=ModelType.LLM,
    )
    assert "_include_usage_marker" not in kwargs


async def _collect(gen):
    return [c async for c in gen]


def _fake_stream(chunks):
    async def gen():
        for c in chunks:
            yield c
    return gen()


def test_stream_emits_marker_chunk_with_usage():
    client = make_client()
    chunks = [
        SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="Hello "))], usage=None),
        SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="world"))], usage=None),
        SimpleNamespace(choices=[],  # vLLM's trailing usage chunk
                        usage=SimpleNamespace(prompt_tokens=5787, completion_tokens=4058, total_tokens=9845)),
    ]
    out = asyncio.run(_collect(client._stream_with_usage_logging(_fake_stream(chunks), "m", emit_marker=True)))
    assert len(out) == 4
    marker_text = out[-1].choices[0].delta.content
    m = re.search(r"<<<USAGE_JSON:(\{.*\})>>>", marker_text)
    assert m
    import json
    assert json.loads(m.group(1)) == {"input_tokens": 5787, "output_tokens": 4058}


def test_stream_without_marker_flag_is_unchanged():
    client = make_client()
    chunks = [
        SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="x"))], usage=None),
        SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2)),
    ]
    out = asyncio.run(_collect(client._stream_with_usage_logging(_fake_stream(chunks), "m", emit_marker=False)))
    assert len(out) == 2


def test_stream_with_no_usage_chunk_emits_no_marker():
    client = make_client()
    chunks = [SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="x"))], usage=None)]
    out = asyncio.run(_collect(client._stream_with_usage_logging(_fake_stream(chunks), "m", emit_marker=True)))
    assert len(out) == 1


def test_marker_chunk_shape_matches_consumer_expectations():
    chunk = _usage_marker_chunk(SimpleNamespace(prompt_tokens=10, completion_tokens=20))
    # consumers do: chunk.choices[0].delta.content
    assert "<<<USAGE_JSON:" in chunk.choices[0].delta.content
    assert chunk.usage is None
