"""Tests for provider-aware prompt budget fitting."""
from api.prompt_fit import fit_to_budget, prompt_token_budget


def test_budget_for_claude_is_large():
    assert prompt_token_budget("claude") >= 500_000


def test_budget_default_is_conservative():
    assert prompt_token_budget("vllm") == 24_000
    assert prompt_token_budget("unknown-provider") == 24_000


def test_fit_noop_when_under_budget():
    file_content, context_text = fit_to_budget(
        file_content="A" * 1000, context_text="B" * 1000,
        base_tokens=500, budget=28_000,
    )
    assert file_content == "A" * 1000
    assert context_text == "B" * 1000


def test_fit_drops_rag_context_first():
    # ~120k chars ≈ 30k tokens of file + 40k chars ≈ 10k tokens of RAG, budget 31k:
    # dropping RAG alone gets under budget, file stays whole.
    file_content, context_text = fit_to_budget(
        file_content="A" * 120_000, context_text="B" * 40_000,
        base_tokens=1000, budget=32_000,
    )
    assert context_text == ""
    assert file_content == "A" * 120_000


def test_fit_truncates_file_middle_keeping_head_and_tail():
    big = "HEAD" + ("M" * 400_000) + "TAIL"
    file_content, context_text = fit_to_budget(
        file_content=big, context_text="", base_tokens=1000, budget=28_000,
    )
    assert file_content.startswith("HEAD")
    assert file_content.endswith("TAIL")
    assert "[TRUNCATED" in file_content
    assert len(file_content) < len(big)


def test_fit_empty_file_content_untouched():
    file_content, context_text = fit_to_budget(
        file_content="", context_text="C" * 200_000, base_tokens=1000, budget=28_000,
    )
    # No file content -> RAG is the only context; it gets tail-trimmed, not dropped.
    assert 0 < len(context_text) < 200_000


def test_fit_exhausted_budget_returns_marker_only():
    # budget barely above base_tokens -> allowed_chars clamps to 0;
    # the [-0:] slice must not wrap around to the whole file.
    file_content, context_text = fit_to_budget(
        file_content="X" * 10_000, context_text="", base_tokens=27_999, budget=28_000,
    )
    assert file_content == "[file omitted: context budget exhausted]"
