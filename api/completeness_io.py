"""Read the sibling completeness report files for a wikicache path.

Kept separate from api.api so it imports no FastAPI/app state (and no file
logger), which makes it unit-testable in isolation. Pure file I/O over the
paths derived by api.tsd_brd_completeness.completeness_report_paths.
"""
import json
import os
from typing import Optional, Tuple

from api.tsd_brd_completeness import completeness_report_paths

_MD_MEDIA = "text/markdown; charset=utf-8"


def read_completeness_report(cache_path: str,
                             fmt: str) -> Optional[Tuple[object, Optional[str]]]:
    """Return (content, media_type) for the sibling report, or None if absent.

    fmt="md" -> (markdown_text, "text/markdown; charset=utf-8").
    Any other fmt (incl. "json") -> (parsed_json, None).
    Missing or unreadable file -> None (never raises).
    """
    json_path, md_path = completeness_report_paths(cache_path)
    try:
        if fmt == "md":
            if not os.path.isfile(md_path):
                return None
            with open(md_path, encoding="utf-8") as f:
                return f.read(), _MD_MEDIA
        if not os.path.isfile(json_path):
            return None
        with open(json_path, encoding="utf-8") as f:
            return json.load(f), None
    except Exception:
        return None
