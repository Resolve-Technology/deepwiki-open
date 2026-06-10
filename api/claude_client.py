import logging
import os
from typing import Optional, Callable

from openai import AsyncOpenAI, OpenAI

from api.vllm_client import VLLMClient

log = logging.getLogger(__name__)

# Identifies OAuth (sk-ant-oat01-...) bearer-token requests to Anthropic.
# Verified 2026-06-05: requests succeed with or without this header; we send
# it anyway in case Anthropic starts enforcing it.
ANTHROPIC_OAUTH_BETA = "oauth-2025-04-20"


class ClaudeClient(VLLMClient):
    """
    Claude via Anthropic's OpenAI-compatible endpoint with OAuth bearer auth.

    Subclasses VLLMClient because that class is effectively a generic
    OpenAI-compatible client with per-request token-usage logging; only the
    endpoint, authentication, and log prefix differ.

    Expected environment variables:

        CLAUDE_API_BASE_URL=https://api.anthropic.com   # or a relay/proxy URL
        CLAUDE_OAUTH_TOKEN=sk-ant-oat01-...             # from `claude setup-token`

    The token is a long-lived OAuth token generated with `claude setup-token`
    (requires a Claude Pro/Max subscription). There is no refresh logic; if
    the token is revoked or expires, generate a new one and update .env.
    """

    usage_log_prefix = "Claude usage"

    def __init__(
        self,
        api_key: Optional[str] = None,
        chat_completion_parser: Optional[Callable] = None,
        input_type: str = "text",
        base_url: Optional[str] = None,
    ):
        super().__init__(
            api_key=api_key,
            chat_completion_parser=chat_completion_parser,
            input_type=input_type,
            base_url=base_url
            or os.getenv("CLAUDE_API_BASE_URL", "https://api.anthropic.com"),
            env_base_url_name="CLAUDE_API_BASE_URL",
            env_api_key_name="CLAUDE_OAUTH_TOKEN",
        )

    def _resolve_token(self) -> str:
        token = self._api_key or os.getenv(self._env_api_key_name)
        if not token:
            raise ValueError(
                "CLAUDE_OAUTH_TOKEN is not set. Run `claude setup-token` and "
                "add the sk-ant-oat01-... value to .env"
            )
        return token

    def init_sync_client(self):
        """Initialize synchronous OpenAI-compatible client for Anthropic."""
        return OpenAI(
            api_key=self._resolve_token(),
            base_url=self.base_url,
            default_headers={"anthropic-beta": ANTHROPIC_OAUTH_BETA},
        )

    def init_async_client(self):
        """Initialize asynchronous OpenAI-compatible client for Anthropic."""
        return AsyncOpenAI(
            api_key=self._resolve_token(),
            base_url=self.base_url,
            default_headers={"anthropic-beta": ANTHROPIC_OAUTH_BETA},
        )
