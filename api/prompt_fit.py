"""Provider-aware prompt budget fitting.

Deep-dive pages inject entire program sources into the prompt
(``ChatCompletionRequest.filePath``). Providers differ wildly in context
size (claude: 1M tokens; local vLLM/gemma: tens of k), so before assembly
the handlers call :func:`fit_to_budget` which, in order:

1. leaves everything untouched if the estimate fits the provider budget;
2. drops the RAG context (redundant when the full source is present);
3. truncates the *middle* of the file content, keeping head and tail —
   COBOL sources put IDENTIFICATION/ENVIRONMENT/DATA divisions at the top
   and the tail of PROCEDURE DIVISION carries termination logic, so the
   middle is the least-bad cut.

Token counts are estimated at 4 chars/token to avoid tokenizer costs on
huge strings; budgets carry enough slack that the estimate is safe.
"""
import logging
import os

log = logging.getLogger(__name__)

CHARS_PER_TOKEN = 4
TRUNCATION_MARKER = "\n\n*** [TRUNCATED: middle of file omitted to fit the model's context window] ***\n\n"

# Conservative defaults; override per deployment via env if needed.
# 24k (not higher) because the 4-chars/token estimate UNDERCOUNTS dense
# COBOL tokens (short keywords, numerics) — keep ~20% slack vs the model
# context rather than sail close to it.
_DEFAULT_BUDGET = int(os.getenv("DEEPWIKI_PROMPT_TOKEN_BUDGET", "24000"))
_PROVIDER_BUDGETS = {
    "claude": int(os.getenv("DEEPWIKI_CLAUDE_PROMPT_TOKEN_BUDGET", "800000")),
}


def prompt_token_budget(provider: str) -> int:
    """Return the prompt token budget for a provider."""
    return _PROVIDER_BUDGETS.get(provider, _DEFAULT_BUDGET)


def _estimate_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


def fit_to_budget(file_content: str, context_text: str, base_tokens: int, budget: int):
    """Fit (file_content, context_text) into ``budget`` tokens.

    ``base_tokens`` is the estimated size of everything else in the prompt
    (system prompt, instruction, conversation history).
    Returns the possibly-reduced ``(file_content, context_text)`` pair.
    """
    def total():
        return base_tokens + _estimate_tokens(file_content) + _estimate_tokens(context_text)

    if total() <= budget:
        return file_content, context_text

    # 1) Drop RAG context when the full source is present — it is redundant.
    if file_content and context_text:
        log.info("Prompt over budget (%d > %d tokens): dropping RAG context", total(), budget)
        context_text = ""
        if total() <= budget:
            return file_content, context_text

    # 2) Truncate the middle of whichever block remains too large.
    if file_content:
        allowed_chars = max((budget - base_tokens) * CHARS_PER_TOKEN - len(TRUNCATION_MARKER), 0)
        if allowed_chars < len(file_content):
            head = allowed_chars // 2
            tail = allowed_chars - head
            if allowed_chars == 0:
                log.warning(
                    "File content over budget and budget exhausted: omitting entire file (%d chars)",
                    len(file_content),
                )
                file_content = "[file omitted: context budget exhausted]"
            else:
                log.warning(
                    "File content over budget: keeping first %d and last %d of %d chars",
                    head, tail, len(file_content),
                )
                file_content = file_content[:head] + TRUNCATION_MARKER + file_content[-tail:]
    elif context_text:
        allowed_chars = max((budget - base_tokens) * CHARS_PER_TOKEN, 0)
        if allowed_chars < len(context_text):
            context_text = context_text[:allowed_chars]

    return file_content, context_text
