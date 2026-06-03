import os
from typing import Optional, Callable
from openai import AsyncOpenAI, OpenAI

from api.openai_client import OpenAIClient


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
