"""Parity tests for api/prompt_assembly.py.

The envelope must be byte-identical to what websocket_wiki.py assembles —
these tests quote the websocket's format exactly (leading ``/no_think ``, the
``<note>`` variant when no context, the ``Assistant: `` tail).
"""
import pytest

from api.prompt_assembly import (assemble_envelope, format_context_text,
                                 select_generation_system_prompt)


class FakeDoc:
    def __init__(self, file_path, text):
        self.meta_data = {"file_path": file_path}
        self.text = text


class FakeRetrieverOutput:
    def __init__(self, documents):
        self.documents = documents


def test_envelope_without_context_matches_websocket_format():
    out = assemble_envelope("SYSTEM", "QUERY")
    expected = (
        "/no_think SYSTEM\n\n"
        "<note>Answering without retrieval augmentation.</note>\n\n"
        "<query>\nQUERY\n</query>\n\nAssistant: "
    )
    assert out == expected


def test_envelope_with_context_and_file_content():
    out = assemble_envelope(
        "SYSTEM", "QUERY",
        file_content="FILE BODY", file_path="src/a.py",
        context_text="CTX",
    )
    expected = (
        "/no_think SYSTEM\n\n"
        "<currentFileContent path=\"src/a.py\">\nFILE BODY\n</currentFileContent>\n\n"
        "<START_OF_CONTEXT>\nCTX\n<END_OF_CONTEXT>\n\n"
        "<query>\nQUERY\n</query>\n\nAssistant: "
    )
    assert out == expected


def test_envelope_with_conversation_history():
    out = assemble_envelope("S", "Q", conversation_history="<turn>t</turn>\n")
    assert "<conversation_history>\n<turn>t</turn>\n</conversation_history>\n\n" in out
    assert out.startswith("/no_think S\n\n")
    assert out.endswith("<query>\nQ\n</query>\n\nAssistant: ")


def test_envelope_whitespace_context_falls_back_to_note():
    out = assemble_envelope("S", "Q", context_text="   \n  ")
    assert "<note>Answering without retrieval augmentation.</note>" in out
    assert "<START_OF_CONTEXT>" not in out


def test_envelope_applies_budget_fit():
    # A non-claude provider gets the small default budget; a huge file body
    # must come back truncated with the marker, exactly like the websocket.
    huge = "x" * 1_000_000
    out = assemble_envelope("S", "Q", file_content=huge, file_path="big.cbl",
                            provider="vllm")
    assert "[TRUNCATED: middle of file omitted" in out
    assert len(out) < len(huge)


def test_format_context_text_groups_by_file_path():
    docs = [FakeDoc("a.py", "first chunk"), FakeDoc("b.py", "other file"),
            FakeDoc("a.py", "second chunk")]
    out = format_context_text([FakeRetrieverOutput(docs)])
    # Same quirk as the websocket: separator dashes appear once, at the start.
    expected = (
        "\n\n" + "-" * 10 +
        "## File Path: a.py\n\nfirst chunk\n\nsecond chunk"
        "\n\n## File Path: b.py\n\nother file"
    )
    assert out == expected


def test_format_context_text_empty_inputs():
    assert format_context_text(None) == ""
    assert format_context_text([FakeRetrieverOutput([])]) == ""


def test_system_prompt_anchors():
    p = select_generation_system_prompt("github", "https://github.com/o/r", "r", "en")
    assert p.startswith("<role>\nYou are an expert code analyst examining the github repository: https://github.com/o/r (r).")
    assert "IMPORTANT:You MUST respond in English language." in p
    assert "JUST START with the direct answer to the question" in p
    assert p.endswith("</style>")


def test_system_prompt_language_lookup():
    p = select_generation_system_prompt("github", "u", "r", "ja")
    assert "respond in Japanese (日本語) language." in p
    # Unknown code falls back to English, like the websocket's .get(..., "English")
    p = select_generation_system_prompt("github", "u", "r", "klingon")
    assert "respond in English language." in p
