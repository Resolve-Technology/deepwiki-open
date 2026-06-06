"""Tests for vLLM server discovery (api/vllm_discovery.py)."""
from api.vllm_discovery import build_inventory, parse_ports, parse_vllm_models, scan_targets


def test_parse_vllm_models():
    payload = {"object": "list", "data": [
        {"id": "google/gemma-4-26B-A4B-it", "object": "model"},
        {"id": "Qwen/Qwen3-32B", "object": "model"},
        {"object": "model"},          # no id -> skipped
        "garbage",                     # not a dict -> skipped
    ]}
    assert parse_vllm_models(payload) == ["google/gemma-4-26B-A4B-it", "Qwen/Qwen3-32B"]
    assert parse_vllm_models({"data": "nope"}) == []
    assert parse_vllm_models(None) == []


def test_parse_ports_ranges_and_lists():
    assert parse_ports("8000-8010") == list(range(8000, 8011))
    assert parse_ports("8000,8005") == [8000, 8005]
    assert parse_ports("8005, 8000-8002") == [8000, 8001, 8002, 8005]
    assert parse_ports("junk,80x0,9000000") == []
    assert parse_ports("") == []


def test_scan_targets_from_scan_vars(monkeypatch):
    monkeypatch.setenv("VLLM_SCAN_HOSTS", "192.168.114.188, 192.168.96.135")
    monkeypatch.setenv("VLLM_SCAN_PORTS", "8000-8001")
    assert scan_targets() == [
        "http://192.168.114.188:8000/v1",
        "http://192.168.114.188:8001/v1",
        "http://192.168.96.135:8000/v1",
        "http://192.168.96.135:8001/v1",
    ]


def test_scan_targets_falls_back_to_single_base_url(monkeypatch):
    monkeypatch.delenv("VLLM_SCAN_HOSTS", raising=False)
    monkeypatch.delenv("VLLM_SCAN_PORTS", raising=False)
    monkeypatch.setenv("VLLM_API_BASE_URL", "http://10.0.0.1:8005")
    assert scan_targets() == ["http://10.0.0.1:8005/v1"]


def test_build_inventory_dedupes_and_excludes_embedder(monkeypatch):
    monkeypatch.setenv("VLLM_EMBEDDER_MODEL", "BAAI/bge-m3")
    results = [
        ("http://h1:8000/v1", ["BAAI/bge-m3"]),                 # embedder -> excluded
        ("http://h1:8005/v1", ["google/gemma-4-26B-A4B-it"]),
        ("http://h2:8005/v1", ["google/gemma-4-26B-A4B-it",     # dup -> first route wins
                               "Qwen/Qwen3-32B"]),
        ("http://h2:8006/v1", []),                               # dead port
    ]
    models, routes = build_inventory(results)
    assert models == ["google/gemma-4-26B-A4B-it", "Qwen/Qwen3-32B"]
    assert routes == {
        "google/gemma-4-26B-A4B-it": "http://h1:8005/v1",
        "Qwen/Qwen3-32B": "http://h2:8005/v1",
    }
