import json
from pathlib import Path


def _providers():
    p = Path(__file__).resolve().parents[2] / "api" / "config" / "generator.json"
    return json.loads(p.read_text())["providers"]


def test_claude_api_provider_registered():
    prov = _providers()
    assert "claude_api" in prov
    models = prov["claude_api"]["models"]
    assert "claude-sonnet-5" in models
    assert "claude-opus-4-8" in models
    assert prov["claude_api"]["default_model"] == "claude-sonnet-5"


def test_claude_api_does_not_disturb_claude():
    prov = _providers()
    # existing OAuth provider still present and unchanged in shape
    assert "claude-haiku-4-5-20251001" in prov["claude"]["models"]
