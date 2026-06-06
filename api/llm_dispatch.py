"""One-call LLM dispatch for the generation engine.

Returns (text, usage) for a fully-assembled prompt. Mirrors the provider
branches of the websocket chat path for the providers this deployment uses;
unsupported providers raise so jobs fail fast with a clear error.
"""
import logging
from dataclasses import dataclass

from adalflow.core.types import ModelType

from api.anthropic_client import AnthropicClient
from api.config import get_model_config
from api.vllm_client import VLLMClient
from api.vllm_discovery import get_vllm_route

logger = logging.getLogger(__name__)


@dataclass
class LLMResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0


async def generate(provider: str, model: str, prompt: str) -> LLMResult:
    """Send a fully-assembled prompt, drain the stream, return text + usage."""
    model_config = get_model_config(provider, model)["model_kwargs"]

    if provider == "claude":
        logger.info(f"Dispatching to Claude (native Anthropic SDK) model: {model}")
        client = AnthropicClient()
        model_kwargs = {"model": model}
        # Same conditional passthrough as the websocket's claude branch (Opus
        # 4.7+ rejects temperature/top_p, so only forward what the config has).
        for key in ("temperature", "top_p", "max_tokens", "thinking"):
            if key in model_config:
                model_kwargs[key] = model_config[key]
        api_kwargs = client.convert_inputs_to_api_kwargs(
            input=prompt, model_kwargs=model_kwargs, model_type=ModelType.LLM)
        text = []
        stream = await client.acall(api_kwargs=api_kwargs, model_type=ModelType.LLM)
        async for chunk in stream:
            piece = chunk.choices[0].delta.content
            if piece:
                text.append(piece)
        u = client.last_usage
        return LLMResult("".join(text),
                         getattr(u, "input_tokens", 0) or 0,
                         getattr(u, "output_tokens", 0) or 0)

    if provider == "vllm":
        route = get_vllm_route(model)
        logger.info(f"Dispatching to vLLM model: {model} via {route or 'default base URL'}")
        client = VLLMClient(base_url=route) if route else VLLMClient()
        model_kwargs = {
            "model": model,
            "stream": True,
            "temperature": model_config["temperature"],
        }
        # Only add top_p if it exists in the model config (websocket parity)
        if "top_p" in model_config:
            model_kwargs["top_p"] = model_config["top_p"]
        api_kwargs = client.convert_inputs_to_api_kwargs(
            input=prompt, model_kwargs=model_kwargs, model_type=ModelType.LLM)
        text = []
        stream = await client.acall(api_kwargs=api_kwargs, model_type=ModelType.LLM)
        async for chunk in stream:
            choices = getattr(chunk, "choices", [])
            if choices:
                piece = getattr(choices[0].delta, "content", None)
                if piece:
                    text.append(piece)
        u = client.last_usage
        return LLMResult("".join(text),
                         getattr(u, "prompt_tokens", 0) or 0,
                         getattr(u, "completion_tokens", 0) or 0)

    raise ValueError(f"Server-side generation does not support provider {provider!r} yet")
