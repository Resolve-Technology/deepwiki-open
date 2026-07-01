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
_MD_H = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


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


# (page_id, section English label) blocks whose outline bullets are required
# table rows. Extend this set to row-check more sections — the labels already
# live in TSD_BRD_OUTLINES, so no new data is needed.
ENUMERATED_SECTIONS = {("page-tsd-system-overview", "Impact Analysis")}


def parse_required_rows(outline: str, section_label: str) -> list:
    """The ``- `` bullet labels directly under ``## <section_label>`` (up to the
    next heading). Prose lines and bullets in other sections are ignored."""
    target = _normalize(section_label)
    rows, in_section = [], False
    for line in (outline or "").splitlines():
        m = _OUTLINE_H.match(line)
        if m:
            in_section = _normalize(m.group(2)) == target
            continue
        if in_section and line.strip().startswith("- "):
            rows.append(line.strip()[2:].strip())
    return rows


def _parse_generated_headings(content: str) -> list:
    """Headings (H1-H6) of a generated page, each with its section body.

    Body = lines after the heading up to the next heading of level <= its own
    (or end of page). Lines inside ``` code fences are not treated as headings.
    """
    lines = (content or "").splitlines()
    heads, in_fence = [], False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _MD_H.match(line)
        if m:
            heads.append({"level": len(m.group(1)),
                          "anchor": _anchor_from_heading(m.group(2)),
                          "line": i})
    for idx, h in enumerate(heads):
        end = len(lines)
        for nxt in heads[idx + 1:]:
            if nxt["level"] <= h["level"]:
                end = nxt["line"]
                break
        h["body"] = "\n".join(lines[h["line"] + 1:end]).strip()
        del h["line"]
    return heads
