"""Read the sibling completeness report files for a wikicache path.

Kept separate from api.api so it imports no FastAPI/app state (and no file
logger), which makes it unit-testable in isolation. Also provides atomic writes
and read-time self-heal: a missing report is rebuilt from the sibling wikicache
(a pure function of its generated_pages) so the latest report never disappears.
"""
import json
import os
import tempfile
from typing import Optional, Tuple

from api.tsd_brd_completeness import (build_report_payload,
                                      completeness_report_paths,
                                      render_markdown_report)

_MD_MEDIA = "text/markdown; charset=utf-8"


def atomic_write_text(path: str, text: str) -> None:
    """Write text atomically: temp file in the same dir, fsync, os.replace."""
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _rebuild_from_wikicache(cache_path: str, json_path: str, md_path: str):
    """Rebuild the report from the sibling wikicache and persist it atomically.
    Returns the parsed payload dict, or None if the wikicache is unusable."""
    if not os.path.isfile(cache_path):
        return None
    with open(cache_path, encoding="utf-8") as f:
        wc = json.load(f)
    repo = wc.get("repo") or {}
    # Old-format caches may lack the repo dict (repo is Optional, falls back to
    # repo_url) — avoid a "None/None" repo string in the rebuilt report.
    repo_str = (f"{repo.get('owner')}/{repo.get('repo')}"
                if (repo.get("owner") or repo.get("repo"))
                else (wc.get("repo_url") or ""))
    payload = build_report_payload(
        list((wc.get("generated_pages") or {}).values()),
        repo_str, wc.get("provider"), wc.get("model"), wc.get("generated_at"))
    atomic_write_text(json_path, json.dumps(payload, ensure_ascii=False, indent=2))
    atomic_write_text(md_path, render_markdown_report(payload))
    return payload


def read_completeness_report(cache_path: str,
                             fmt: str) -> Optional[Tuple[object, Optional[str]]]:
    """Return (content, media_type) for the sibling report, or None if absent.

    fmt="md" -> (markdown_text, "text/markdown; charset=utf-8").
    Any other fmt -> (parsed_json, None).
    If the report file is missing but the wikicache exists, rebuild + persist it
    from the cached pages (self-heal). Never raises.
    """
    json_path, md_path = completeness_report_paths(cache_path)
    target = md_path if fmt == "md" else json_path
    try:
        if os.path.isfile(target):
            if fmt == "md":
                with open(md_path, encoding="utf-8") as f:
                    return f.read(), _MD_MEDIA
            with open(json_path, encoding="utf-8") as f:
                return json.load(f), None
        # Report missing — self-heal from the wikicache if it exists.
        payload = _rebuild_from_wikicache(cache_path, json_path, md_path)
        if payload is None:
            return None
        if fmt == "md":
            return render_markdown_report(payload), _MD_MEDIA
        return payload, None
    except Exception:
        return None
