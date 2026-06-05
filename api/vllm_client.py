import logging
import os
from typing import Any, Dict, Optional, Callable
from openai import AsyncOpenAI, OpenAI

from adalflow.core.types import ModelType

from api.openai_client import OpenAIClient

log = logging.getLogger(__name__)


class VLLMClient(OpenAIClient):
    """
    vLLM OpenAI-compatible client.

    vLLM exposes an OpenAI-compatible API surface (``/v1/chat/completions`` and
    ``/v1/embeddings``), so we reuse almost all OpenAIClient behavior and only
    override client initialization to point at the local vLLM server. The API
    key defaults to a dummy value because vLLM does not require one unless it
    was started with ``--api-key``.

    Expected environment variables:

        VLLM_API_BASE_URL=http://192.168.96.135:8005   # chat/completions server
        VLLM_API_KEY=dummy                             # optional

    For embeddings (often a separate vLLM instance serving an embedding model),
    pass ``base_url`` explicitly via the embedder config's ``initialize_kwargs``
    so the embedding endpoint can differ from the chat endpoint.

    Example model names:
        google/gemma-4-26B-A4B-it
        BAAI/bge-m3
    """

    # Prefix used by _log_usage lines; subclasses override (e.g. "Claude usage").
    usage_log_prefix = "vLLM usage"

    def __init__(
        self,
        api_key: Optional[str] = None,
        chat_completion_parser: Optional[Callable] = None,
        input_type: str = "text",
        base_url: Optional[str] = None,
        env_base_url_name: str = "VLLM_API_BASE_URL",
        env_api_key_name: str = "VLLM_API_KEY",
    ):
        resolved_base_url = base_url or os.getenv(env_base_url_name, "http://localhost:8000")
        if not resolved_base_url.rstrip("/").endswith("/v1"):
            resolved_base_url = f"{resolved_base_url.rstrip('/')}/v1"
        super().__init__(
            api_key=api_key,
            chat_completion_parser=chat_completion_parser,
            input_type=input_type,
            base_url=resolved_base_url,
            env_base_url_name=env_base_url_name,
            env_api_key_name=env_api_key_name,
        )

    def convert_inputs_to_api_kwargs(self, input=None, model_kwargs={}, model_type=ModelType.UNDEFINED) -> Dict:
        """Ask vLLM to report token usage on streaming responses.

        With ``stream_options.include_usage`` set, vLLM appends a final chunk
        with empty ``choices`` and a populated ``usage`` field. Existing
        consumers skip chunks without choices, so this is transparent to them.
        """
        api_kwargs = super().convert_inputs_to_api_kwargs(input, model_kwargs, model_type)
        if model_type == ModelType.LLM and api_kwargs.get("stream", False):
            api_kwargs.setdefault("stream_options", {"include_usage": True})
        return api_kwargs

    def _log_usage(self, usage: Any, model: Optional[str]) -> None:
        """Log token usage reported by the server."""
        if usage is None:
            return
        log.info(
            f"{self.usage_log_prefix}: model={model} prompt_tokens={getattr(usage, 'prompt_tokens', None)} "
            f"completion_tokens={getattr(usage, 'completion_tokens', None)} "
            f"total_tokens={getattr(usage, 'total_tokens', None)}"
        )

    async def _stream_with_usage_logging(self, stream, model: Optional[str]):
        """Yield chunks unchanged while capturing the final usage chunk."""
        usage = None
        try:
            async for chunk in stream:
                usage = getattr(chunk, "usage", None) or usage
                yield chunk
        finally:
            self._log_usage(usage, model)

    async def acall(self, api_kwargs: Dict = {}, model_type: ModelType = ModelType.UNDEFINED):
        """Same as OpenAIClient.acall, but logs token usage for LLM calls."""
        response = await super().acall(api_kwargs=api_kwargs, model_type=model_type)
        if model_type == ModelType.LLM:
            model = api_kwargs.get("model")
            if api_kwargs.get("stream", False):
                return self._stream_with_usage_logging(response, model)
            self._log_usage(getattr(response, "usage", None), model)
        return response

    def call(self, api_kwargs: Dict = {}, model_type: ModelType = ModelType.UNDEFINED):
        """Same as OpenAIClient.call, but logs token usage for streaming LLM calls."""
        response = super().call(api_kwargs=api_kwargs, model_type=model_type)
        if model_type == ModelType.LLM and api_kwargs.get("stream", False):
            model = api_kwargs.get("model")

            def _sync_stream():
                usage = None
                try:
                    for chunk in response:
                        usage = getattr(chunk, "usage", None) or usage
                        yield chunk
                finally:
                    self._log_usage(usage, model)

            return _sync_stream()
        return response

    def init_sync_client(self):
        """Initialize synchronous vLLM OpenAI-compatible client."""
        api_key = self._api_key or os.getenv(self._env_api_key_name, "dummy")
        return OpenAI(
            api_key=api_key,
            base_url=self.base_url,
        )

    def init_async_client(self):
        """Initialize asynchronous vLLM OpenAI-compatible client."""
        api_key = self._api_key or os.getenv(self._env_api_key_name, "dummy")
        return AsyncOpenAI(
            api_key=api_key,
            base_url=self.base_url,
        )
