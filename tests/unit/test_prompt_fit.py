"""Tests for provider-aware prompt budget fitting."""
import importlib

from api.prompt_fit import fit_to_budget, prompt_token_budget


def test_budget_claude_200k_model_leaves_completion_room():
    # A 200k-context Claude model must reserve room for its completion, so the
    # input budget is well under 200k — otherwise a full-program prompt + the
    # response overflow the context window (the B5349 "prompt is too long" bug).
    budget = prompt_token_budget("claude", "claude-haiku-4-5-20251001")
    assert budget < 200_000
    assert budget <= 200_000 - 64_000  # haiku max_tokens reserved
    assert budget >= 100_000


def test_budget_opus_reserves_more_than_haiku():
    # opus-4-8 reserves 100k for completion vs haiku's 64k, so opus gets a
    # SMALLER input budget on the same 200k window — proves it's per-model.
    assert (prompt_token_budget("claude", "claude-opus-4-8")
            < prompt_token_budget("claude", "claude-haiku-4-5-20251001"))


def test_budget_claude_1m_model_is_large():
    # A model carrying the "[1m]" beta tag gets the 1M context window.
    assert prompt_token_budget("claude", "claude-opus-4-8[1m]") >= 800_000


def test_budget_claude_unknown_model_still_under_200k():
    # Unknown model -> fallback completion reserve, still safely under 200k.
    assert prompt_token_budget("claude", "claude-future-x") < 200_000


def test_budget_claude_env_override_wins(monkeypatch):
    monkeypatch.setenv("DEEPWIKI_CLAUDE_PROMPT_TOKEN_BUDGET", "150000")
    assert prompt_token_budget("claude", "claude-haiku-4-5-20251001") == 150_000


def test_oversized_program_truncated_under_claude_budget():
    # Regression for B5349: a ~210k-token program (the size that 400'd) must now
    # be truncated to fit the model-aware budget instead of being sent whole.
    budget = prompt_token_budget("claude", "claude-haiku-4-5-20251001")
    big = "HEAD" + ("M" * (210_000 * 4)) + "TAIL"
    fc, _ = fit_to_budget(file_content=big, context_text="",
                          base_tokens=2000, budget=budget)
    assert "[TRUNCATED" in fc
    assert len(fc) < len(big)
    assert (len(fc) // 4) + 2000 <= budget


def test_budget_default_is_conservative(monkeypatch):
    # The deployment may override DEEPWIKI_PROMPT_TOKEN_BUDGET (read at
    # import time); clear it and reload to assert the shipped default.
    monkeypatch.delenv("DEEPWIKI_PROMPT_TOKEN_BUDGET", raising=False)
    import api.prompt_fit as pf
    importlib.reload(pf)
    try:
        assert pf.prompt_token_budget("vllm") == 24_000
        assert pf.prompt_token_budget("unknown-provider") == 24_000
    finally:
        monkeypatch.undo()
        importlib.reload(pf)


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
