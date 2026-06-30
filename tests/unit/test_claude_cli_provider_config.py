"""The claude_cli provider must be registered and resolvable in config."""
from api.config import configs, get_model_config


def test_claude_cli_provider_registered():
    assert "claude_cli" in configs["providers"]
    provider = configs["providers"]["claude_cli"]
    assert provider["default_model"] == "claude-sonnet-4-6"
    assert "claude-sonnet-4-6" in provider["models"]


def test_get_model_config_resolves_claude_cli():
    cfg = get_model_config("claude_cli", "claude-sonnet-4-6")
    # client_class: "ClaudeClient" maps to a real model_client so this never raises
    assert cfg["model_client"].__name__ == "ClaudeClient"
    assert cfg["model_kwargs"]["model"] == "claude-sonnet-4-6"


def test_get_model_config_claude_cli_default_model():
    cfg = get_model_config("claude_cli")  # None model -> default_model
    assert cfg["model_kwargs"]["model"] == "claude-sonnet-4-6"
