"""Remove wiki-page claims whose citations all failed verification.

The grounding pass (citation_grounding.py) marks each `[file:lines]()` citation
verified or broken. After the correction loop has done its best, this module
deletes the markdown blocks that cite ONLY broken citations, so the saved page
carries no claim we could not ground. A block with at least one verified
citation, or no citations at all, is kept untouched. Pure module: no I/O.
"""
import re
from typing import Dict, List

from api.citation_grounding import parse_citation_label

# Same empty-href citation pattern as citation_grounding._EMPTY_LINK_RE.
_EMPTY_LINK_RE = re.compile(r"\[([^\]]+)\]\(\)")


def _citation_labels(block: str) -> List[str]:
    """Labels in a block that parse as real file:line citations."""
    return [m.strip() for m in _EMPTY_LINK_RE.findall(block)
            if parse_citation_label(m.strip()) is not None]


def _is_sources_only(block: str) -> bool:
    """True when a block is just a trailing `Sources: ...` line (no prose)."""
    return block.strip().lower().startswith("sources:")


def strip_unverified_claims(content: str, citations: Dict[str, dict]) -> str:
    """Drop every markdown block whose citations are all broken.

    ``citations`` is the {label: {status, ...}} map from verify_page_citations.
    A block is dropped iff it contains at least one citation present in
    ``citations`` and EVERY such citation has status "broken". Blocks with a
    verified citation, or with no citations, are kept. A standalone `Sources:`
    block is first merged into the preceding block so a claim and its citations
    are kept or dropped together (never orphaned).
    """
    raw_blocks = re.split(r"\n\s*\n", content)

    blocks: List[str] = []
    for b in raw_blocks:
        if blocks and _is_sources_only(b):
            blocks[-1] = blocks[-1].rstrip() + "\n" + b.strip()
        else:
            blocks.append(b)

    kept: List[str] = []
    for b in blocks:
        labels = _citation_labels(b)
        statuses = [citations[label]["status"] for label in labels
                    if label in citations]
        drop = bool(statuses) and all(s == "broken" for s in statuses)
        if not drop:
            kept.append(b.strip())

    return "\n\n".join(k for k in kept if k)
