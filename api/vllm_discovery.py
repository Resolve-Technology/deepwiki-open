"""Discovery of local vLLM chat servers.

Scans a configurable set of hosts/ports for OpenAI-compatible vLLM servers
(GET /v1/models) and keeps a model -> base_url routing table so chat requests
go to the server that actually serves the chosen model.

Configuration (env):

    VLLM_SCAN_HOSTS=192.168.114.188,192.168.96.135   # comma-separated hosts
    VLLM_SCAN_PORTS=8000-8010                        # range ("a-b") and/or list
    VLLM_API_BASE_URL=http://192.168.96.135:8005     # fallback when scan vars unset
    VLLM_EMBEDDER_MODEL=BAAI/bge-m3                  # excluded from chat dropdown

Probes run concurrently with a short timeout; results are cached for a minute
and the last good inventory is kept when servers are temporarily unreachable.
"""

import asyncio
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

SCAN_TTL_SECONDS = 60
PROBE_TIMEOUT_SECONDS = 2.0
_MAX_PROBE_WORKERS = 12

# expires: monotonic deadline; models: ordered ids; routes: model id -> base url
_cache: Dict[str, Any] = {"expires": 0.0, "models": [], "routes": {}}


def parse_vllm_models(payload: Any) -> List[str]:
    """Extracts model ids from an OpenAI-style GET /v1/models response."""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", [])
    if not isinstance(data, list):
        return []
    return [m["id"] for m in data if isinstance(m, dict) and m.get("id")]


def parse_ports(spec: str) -> List[int]:
    """Parses "8000-8010" / "8000,8005" / mixes of both into a port list."""
    ports: List[int] = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            try:
                start_s, end_s = part.split("-", 1)
                start, end = int(start_s), int(end_s)
            except ValueError:
                logger.warning(f"Ignoring invalid port range: {part!r}")
                continue
            if start <= end and end - start <= 1000:  # sanity cap
                ports.extend(range(start, end + 1))
        else:
            try:
                ports.append(int(part))
            except ValueError:
                logger.warning(f"Ignoring invalid port: {part!r}")
    return sorted(set(p for p in ports if 0 < p < 65536))


def scan_targets() -> List[str]:
    """Base URLs (".../v1") to probe, from scan vars or the single configured URL."""
    hosts = [h.strip() for h in os.getenv("VLLM_SCAN_HOSTS", "").split(",") if h.strip()]
    ports = parse_ports(os.getenv("VLLM_SCAN_PORTS", ""))
    if hosts and ports:
        return [f"http://{host}:{port}/v1" for host in hosts for port in ports]
    base = os.getenv("VLLM_API_BASE_URL", "").rstrip("/")
    if not base:
        return []
    if not base.endswith("/v1"):
        base += "/v1"
    return [base]


def _probe(base_url: str) -> Tuple[str, List[str]]:
    """Asks one candidate server which models it serves ([] when unreachable)."""
    try:
        headers = {"Authorization": f"Bearer {os.getenv('VLLM_API_KEY', 'dummy')}"}
        resp = requests.get(f"{base_url}/models", headers=headers, timeout=PROBE_TIMEOUT_SECONDS)
        resp.raise_for_status()
        return base_url, parse_vllm_models(resp.json())
    except Exception:
        # Closed ports are the normal case for a range scan — stay quiet.
        return base_url, []


def build_inventory(results: List[Tuple[str, List[str]]]) -> Tuple[List[str], Dict[str, str]]:
    """Merges per-server results into (ordered model ids, model -> base_url).

    The first server that serves a model wins; the embedder's model is excluded
    from the chat dropdown (it can't do completions).
    """
    embedder_model = os.getenv("VLLM_EMBEDDER_MODEL", "").strip()
    models: List[str] = []
    routes: Dict[str, str] = {}
    for base_url, ids in results:
        for model_id in ids:
            if embedder_model and model_id == embedder_model:
                continue
            if model_id in routes:
                if routes[model_id] != base_url:
                    logger.info(
                        f"vLLM model {model_id} served by both {routes[model_id]} and "
                        f"{base_url}; routing to the former"
                    )
                continue
            routes[model_id] = base_url
            models.append(model_id)
    return models, routes


def _scan_all_blocking() -> Tuple[List[str], Dict[str, str]]:
    targets = scan_targets()
    if not targets:
        return [], {}
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=min(_MAX_PROBE_WORKERS, len(targets))) as pool:
        results = list(pool.map(_probe, targets))
    models, routes = build_inventory(results)
    alive = [base for base, ids in results if ids]
    logger.info(
        f"vLLM scan: {len(targets)} endpoints in {time.monotonic() - started:.1f}s, "
        f"{len(alive)} responding ({', '.join(alive) or 'none'}), "
        f"{len(models)} chat model(s): {models}"
    )
    return models, routes


async def get_vllm_models() -> List[str]:
    """Cached scan; keeps the last good inventory while servers are down."""
    now = time.monotonic()
    if now <= _cache["expires"]:
        return _cache["models"]
    models, routes = await asyncio.to_thread(_scan_all_blocking)
    if models:
        _cache["models"] = models
        _cache["routes"] = routes
    # Back off even on total failure so a dead network doesn't add latency
    # to every /models/config call.
    _cache["expires"] = now + SCAN_TTL_SECONDS
    return _cache["models"]


def get_vllm_route(model: Optional[str]) -> Optional[str]:
    """Base URL serving `model`, from the last scan (None -> caller's default)."""
    if not model:
        return None
    return _cache["routes"].get(model)
