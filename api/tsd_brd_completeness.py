"""Completeness check for the template-driven TSD/BRD wiki pages.

Compares the generated markdown for each fixed-template page against the
required heading structure in ``TSD_BRD_OUTLINES`` and classifies every field
as present-with-content, legitimately empty (``None``), or dropped (MISSING).
Pure and I/O-free so it is fully unit-testable; the caller does all file I/O.
"""
import re

from api.wiki_prompts import TSD_BRD_OUTLINES

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_TRAILING_PARENS = re.compile(r"\(([^)]*)\)\s*$")
# Outline headings are H2/H3 only; generated pages may use H1-H6.
_OUTLINE_H = re.compile(r"^(#{2,3})\s+(.+?)\s*$")


def _normalize(text: str) -> str:
    """Lowercase and drop every non-alphanumeric character (incl. CJK/space)."""
    return _NON_ALNUM.sub("", (text or "").lower())


def _anchor_from_heading(text: str) -> str:
    """Match anchor for a heading: the English wording in trailing parens if
    present (the prompt mandates ``翻譯 (English)``), else the whole text."""
    m = _TRAILING_PARENS.search((text or "").strip())
    return _normalize(m.group(1) if m else text)


def parse_required_headings(outline: str) -> list:
    """Parse an outline block into its ordered required headings."""
    out = []
    for line in (outline or "").splitlines():
        m = _OUTLINE_H.match(line)
        if m:
            out.append({"label": m.group(2).strip(), "level": len(m.group(1))})
    return out
