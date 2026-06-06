"""Native Anthropic Messages API client for the chat streaming paths.

Replaces the OpenAI-compatibility bridge (see ``claude_client.ClaudeClient``)
for the websocket / HTTP chat flows. Compared to the compat endpoint this
gives us:

  * prompt caching (``cache_control`` on the stable system block),
  * adaptive thinking on models that support it,
  * SDK-managed retries that honor ``retry-after`` on 429s,
  * real, model-specific error messages instead of the compat layer's
    generic ``{'message': 'Error'}``.

``ClaudeClient`` stays in place for RAG's adalflow pipelines (which expect a
full OpenAI-style ModelClient); this class only implements the two methods
the chat branches use: ``convert_inputs_to_api_kwargs`` and ``acall``. Its
stream yields OpenAI-shaped shim chunks (``chunk.choices[0].delta.content``)
so the existing stream consumers work unchanged.

Expected environment variables:

    CLAUDE_OAUTH_TOKEN=sk-ant-oat01-...        # from `claude setup-token`
    CLAUDE_NATIVE_API_BASE_URL=...             # optional; SDK default otherwise
"""

import json
import logging
import os
import re
from typing import Any, AsyncIterator, Dict, Optional, Tuple

import anthropic
from adalflow.core.types import ModelType

log = logging.getLogger(__name__)

# Identifies OAuth (sk-ant-oat01-...) bearer-token requests to Anthropic.
ANTHROPIC_OAUTH_BETA = "oauth-2025-04-20"

# The chat paths assemble one combined prompt string:
#   "/no_think {system_prompt}\n\n<conversation_history>...<query>...Assistant: "
# Everything before the first structural tag is the (stable, cacheable) system
# prompt; the rest is the per-request user content.
_STRUCTURAL_MARKERS = (
    "<conversation_history>",
    "<currentFileContent",
    "<START_OF_CONTEXT>",
    "<note>",
    "<query>",
)

_TRAILING_ASSISTANT = re.compile(r"\n*Assistant:\s*$")


def format_usage_marker(input_tokens: int, output_tokens: int) -> str:
    """Trailer appended to a stream when the caller asked for usage accounting.

    The frontend strips it from the content with a matching regex
    (see src/utils/wikiRevision.ts extractUsageMarker).
    """
    payload = json.dumps({"input_tokens": input_tokens, "output_tokens": output_tokens})
    return f"\n<<<USAGE_JSON:{payload}>>>"


def split_prompt(prompt: str) -> Tuple[str, str]:
    """Splits a combined chat prompt into (system, user) parts.

    Strips the vLLM-specific "/no_think" prefix and the trailing
    "Assistant: " completion cue, neither of which mean anything to the
    native Messages API. If no structural marker is found the whole prompt
    becomes the user message (the API requires a non-empty user turn).
    """
    text = (prompt or "").rstrip()
    if text.startswith("/no_think"):
        text = text[len("/no_think"):].lstrip()
    if text.endswith("/no_think"):  # the fallback path appends it as a suffix
        text = text[: -len("/no_think")].rstrip()
    text = _TRAILING_ASSISTANT.sub("", text)

    cut = len(text)
    for marker in _STRUCTURAL_MARKERS:
        index = text.find(marker)
        if index != -1:
            cut = min(cut, index)

    system = text[:cut].strip()
    user = text[cut:].strip()
    if not user:
        # No structural part — send everything as the user turn.
        return "", text.strip()
    return system, user


class _ShimDelta:
    __slots__ = ("content",)

    def __init__(self, content: Optional[str]):
        self.content = content


class _ShimChoice:
    __slots__ = ("delta",)

    def __init__(self, content: Optional[str]):
        self.delta = _ShimDelta(content)


class _ShimChunk:
    """Quacks like an OpenAI streaming chunk for the existing consumers."""

    __slots__ = ("choices", "usage")

    def __init__(self, content: Optional[str]):
        self.choices = [_ShimChoice(content)]
        self.usage = None


class AnthropicClient:
    """Streams chat completions through the native Anthropic Messages API."""

    def __init__(self, auth_token: Optional[str] = None, base_url: Optional[str] = None):
        self._auth_token = auth_token
        self._base_url = base_url
        self._async_client: Optional[anthropic.AsyncAnthropic] = None

    def _resolve_token(self) -> str:
        token = self._auth_token or os.getenv("CLAUDE_OAUTH_TOKEN")
        if not token:
            raise ValueError(
                "CLAUDE_OAUTH_TOKEN is not set. Run `claude setup-token` and "
                "add the sk-ant-oat01-... value to .env"
            )
        return token

    def _resolve_base_url(self) -> Optional[str]:
        base = self._base_url or os.getenv("CLAUDE_NATIVE_API_BASE_URL")
        if base:
            return base.rstrip("/")
        # Fall back to the compat-endpoint var so relay/proxy setups keep
        # working; the native SDK appends /v1/messages itself.
        compat = os.getenv("CLAUDE_API_BASE_URL", "").rstrip("/")
        if compat:
            return compat[:-3] if compat.endswith("/v1") else compat
        return None  # SDK default: https://api.anthropic.com

    def init_async_client(self) -> anthropic.AsyncAnthropic:
        if self._async_client is None:
            self._async_client = anthropic.AsyncAnthropic(
                auth_token=self._resolve_token(),
                base_url=self._resolve_base_url(),
                default_headers={"anthropic-beta": ANTHROPIC_OAUTH_BETA},
                max_retries=3,  # SDK backoff honors retry-after on 429/5xx
            )
        return self._async_client

    def convert_inputs_to_api_kwargs(
        self,
        input: Optional[str] = None,
        model_kwargs: Optional[Dict[str, Any]] = None,
        model_type: ModelType = ModelType.UNDEFINED,
    ) -> Dict[str, Any]:
        """Builds native Messages API kwargs from a combined prompt string."""
        if model_type != ModelType.LLM:
            raise ValueError(f"AnthropicClient only supports LLM calls, got {model_type}")

        kwargs = dict(model_kwargs or {})
        kwargs.pop("stream", None)  # the stream helper handles streaming
        # Internal flag (not an API param): emit a usage marker after the stream
        # so the frontend can account tokens per generation phase.
        include_usage_marker = bool(kwargs.pop("include_usage_marker", False))

        system, user = split_prompt(input or "")
        api_kwargs: Dict[str, Any] = {
            "model": kwargs.pop("model"),
            "max_tokens": kwargs.pop("max_tokens", 16000),
            "messages": [{"role": "user", "content": user}],
        }
        if system:
            # The system prompt is identical across all page generations of a
            # wiki run; mark it cacheable. Below the model's minimum prefix
            # size this is silently ignored, which is fine.
            api_kwargs["system"] = [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]

        thinking = kwargs.pop("thinking", None)
        if thinking == "adaptive":
            api_kwargs["thinking"] = {"type": "adaptive"}
        elif isinstance(thinking, dict):
            api_kwargs["thinking"] = thinking

        # Anything else the config provides (e.g. temperature on models that
        # still accept it) passes through untouched.
        api_kwargs.update(kwargs)
        if include_usage_marker:
            api_kwargs["_include_usage_marker"] = True
        return api_kwargs

    async def acall(
        self,
        api_kwargs: Optional[Dict[str, Any]] = None,
        model_type: ModelType = ModelType.UNDEFINED,
    ) -> AsyncIterator[_ShimChunk]:
        if model_type != ModelType.LLM:
            raise ValueError(f"AnthropicClient only supports LLM calls, got {model_type}")
        return self._stream_chunks(dict(api_kwargs or {}))

    async def _stream_chunks(self, api_kwargs: Dict[str, Any]) -> AsyncIterator[_ShimChunk]:
        include_marker = api_kwargs.pop("_include_usage_marker", False)
        client = self.init_async_client()
        model = api_kwargs.get("model")
        async with client.messages.stream(**api_kwargs) as stream:
            async for text in stream.text_stream:
                yield _ShimChunk(text)
            final = await stream.get_final_message()
        usage = getattr(final, "usage", None)
        if usage is not None:
            log.info(
                f"Claude (native) usage: model={model} "
                f"input_tokens={getattr(usage, 'input_tokens', None)} "
                f"output_tokens={getattr(usage, 'output_tokens', None)} "
                f"cache_read_input_tokens={getattr(usage, 'cache_read_input_tokens', None)} "
                f"cache_creation_input_tokens={getattr(usage, 'cache_creation_input_tokens', None)}"
            )
            if include_marker:
                yield _ShimChunk(format_usage_marker(
                    getattr(usage, "input_tokens", 0) or 0,
                    getattr(usage, "output_tokens", 0) or 0,
                ))
