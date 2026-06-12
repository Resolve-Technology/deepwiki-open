"""Verify wiki-page source citations against the source the model was given.

A page's claims cite `[file.ext:start-end]()`. At generation time we hold the
exact source we showed the model — the line-numbered deep-dive file and the
retrieved RAG chunks — so we can check each citation mechanically: does the file
exist in what we provided, and do the cited lines fall within it? Verified
citations become inline source text in the UI; broken ones are flagged as
possibly fabricated. Pure module: no I/O, no network.
"""
import os
import re
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

# Mirror of src/utils/citationUrl.ts CITATION_RE: a path with a file extension,
# optional :line or :start-end. Requires the extension so prose can't match.
_CITATION_RE = re.compile(r"^([^:]+\.[A-Za-z0-9]+)(?::(\d+)(?:-(\d+))?)?$")

# The prefix number_source_lines adds: "{n:>6} | {code}".
_NUMBERED_RE = re.compile(r"^ *(\d+) \| (.*)$")


@dataclass
class FileSource:
    """Source we provided for one file: real line number -> raw line text.

    ``lines`` is empty when the file was present in context but carried no line
    information (e.g. an old RAG chunk without a span) — the file is then known
    only at whole-file granularity.
    """
    lines: Dict[int, str] = field(default_factory=dict)


def parse_citation_label(label: str) -> Optional[Tuple[str, Optional[int], Optional[int]]]:
    """(file_path, start_line, end_line) for a citation label, or None.

    None means the label is not a citation (no file extension) and should be
    left alone.
    """
    m = _CITATION_RE.match(label.strip())
    if not m:
        return None
    path, start, end = m.group(1), m.group(2), m.group(3)
    return path, (int(start) if start else None), (int(end) if end else None)


def _ingest_numbered(file_path: str, numbered: str, smap: Dict[str, FileSource]) -> None:
    fs = smap.setdefault(file_path, FileSource())
    for line in numbered.splitlines():
        m = _NUMBERED_RE.match(line)
        if m:
            fs.lines[int(m.group(1))] = m.group(2)


def _ingest_chunk(doc, smap: Dict[str, FileSource]) -> None:
    meta = getattr(doc, "meta_data", None) or {}
    file_path = meta.get("file_path")
    if not file_path:
        return
    fs = smap.setdefault(file_path, FileSource())
    start = meta.get("start_line")
    if start is None:
        return  # whole-file presence only
    for offset, text in enumerate(doc.text.splitlines()):
        fs.lines[start + offset] = text


def build_source_map(file_content: str, file_path: str, rag_documents) -> Dict[str, FileSource]:
    """Map file_path -> FileSource of the source we GAVE the model for one page.

    ``file_content`` is the line-numbered deep-dive program source (empty for
    standard pages); ``file_path`` is its path. ``rag_documents`` are the
    retrieved chunk documents (each with ``.text`` and ``.meta_data``).
    """
    smap: Dict[str, FileSource] = {}
    if file_content and file_path:
        _ingest_numbered(file_path, file_content, smap)
    for doc in (rag_documents or []):
        _ingest_chunk(doc, smap)
    return smap


def build_repo_source_map(repo_files: Dict[str, str]) -> Dict[str, FileSource]:
    """Map file_path -> FileSource of the FULL repo file content.

    Used as a fallback when verifying citations: a citation to a real file and a
    real line range should ground even if those exact lines were not among the
    retrieved chunks the model saw. ``repo_files`` is ``{relative_path: text}``.
    """
    smap: Dict[str, FileSource] = {}
    for path, content in (repo_files or {}).items():
        if not path:
            continue
        fs = FileSource()
        for n, line in enumerate((content or "").splitlines(), start=1):
            fs.lines[n] = line
        smap[path] = fs
    return smap


def _lookup(source_map: Dict[str, FileSource], file_path: str) -> Optional[FileSource]:
    """Find ``file_path`` in a source map, falling back to a UNIQUE basename
    match so a citation that dropped the directory prefix (``CLNTSKM.txt`` for
    ``copybook/CLNTSKM.txt``) still resolves. Ambiguous basenames don't match."""
    fs = source_map.get(file_path)
    if fs is not None:
        return fs
    base = os.path.basename(file_path)
    matches = [v for k, v in source_map.items() if os.path.basename(k) == base]
    return matches[0] if len(matches) == 1 else None


# Markdown citations are empty-href links: [label](). Real links have an href
# and are skipped. Mirrors Markdown.tsx, which only treats empty-href links as
# citation candidates.
_EMPTY_LINK_RE = re.compile(r"\[([^\]]+)\]\(\)")


def resolve_citation(label: str, source_map: Dict[str, FileSource],
                     repo_map: Optional[Dict[str, FileSource]] = None) -> Optional[dict]:
    """Resolve one citation label against the provided source.

    Tries ``source_map`` first — the exact source the model was shown, so a hit
    there is the strongest grounding and its snippet is what the model read —
    then falls back to ``repo_map``, the full repo files, so a citation to a
    real file and real line range still verifies when those lines simply were
    not among the retrieved chunks. Only genuine fabrications (no such file, or
    lines past the end of the real file) remain broken.

    Returns a dict {status, filePath, startLine, endLine, snippet, reason}, or
    None if ``label`` is not a citation at all.
    """
    parsed = parse_citation_label(label)
    if parsed is None:
        return None
    file_path, start, end = parsed
    info = {"status": "broken", "filePath": file_path, "startLine": start,
            "endLine": end, "snippet": None, "reason": None}

    for smap in (source_map, repo_map):
        if not smap:
            continue
        fs = _lookup(smap, file_path)
        if fs is None:
            continue
        if start is None:  # whole-file citation: presence is enough
            info["status"] = "verified"
            info["reason"] = None
            return info
        needed = list(range(start, (end or start) + 1))
        if needed and all(n in fs.lines for n in needed):
            info["status"] = "verified"
            info["snippet"] = "\n".join(fs.lines[n] for n in needed)
            info["reason"] = None
            return info
        # File is present here but these lines are not — record why and let the
        # next map (the full file) get a chance before giving up.
        info["reason"] = "lines not in provided source"

    if info["reason"] is None:
        info["reason"] = "file not provided"
    return info


def verify_page_citations(content: str, source_map: Dict[str, FileSource],
                          repo_map: Optional[Dict[str, FileSource]] = None) -> Dict[str, dict]:
    """Resolve every `[label]()` citation in the page markdown.

    Returns {label: resolved-info}. Non-citation empty links are skipped;
    repeated labels collapse to one entry.
    """
    out: Dict[str, dict] = {}
    for label in _EMPTY_LINK_RE.findall(content or ""):
        label = label.strip()
        if label in out:
            continue
        info = resolve_citation(label, source_map, repo_map)
        if info is not None:
            out[label] = info
    return out
