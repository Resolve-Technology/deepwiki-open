#!/usr/bin/env python3
"""Score a generated Program Analysis page against its COBOL source.

Usage: python3 scripts/check_deep_dive.py <wiki_cache.json> <page_id> <source_file>

Checks (exceed-the-PDF criteria):
1. Every PROCEDURE DIVISION paragraph name appears in the page.
2. Every 01/77-level working-storage name appears in the page.
3. All 12 required section headings are present.
4. Page length comfortably exceeds the 8-page reference (~8.6k chars).
Exit code 0 = all pass, 1 = any failure (prints a coverage report).
"""
import json
import re
import sys


REQUIRED_HEADINGS = [
    "Program Identification", "Environment & File Definitions",
    "Copybooks", "Working-Storage Inventory", "Paragraph Inventory",
    "Paragraph-by-Paragraph", "Control & Restart", "Data Flow",
    "Error Handling", "Cross-Program", "Gotchas", "Glossary",
]
NUMERIC_HEADINGS = [f"## {i}." for i in range(1, 13)]
MIN_CHARS = 25_000  # ~3x the reference PDF's text volume


def cobol_paragraphs(src: str) -> list[str]:
    """Paragraph/section labels in the PROCEDURE DIVISION (area-A labels ending '.')."""
    m = re.search(r"PROCEDURE\s+DIVISION", src, re.IGNORECASE)
    body = src[m.end():] if m else src
    # Fixed-column COBOL: cols 1-6 sequence area (spaces in this codebase),
    # col 7 indicator, Area A starts at col 8 -> labels carry ~7 leading spaces.
    names = re.findall(r"^ {6,8}([A-Z0-9][A-Z0-9-]{2,30})\s*(?:SECTION\s*)?\.\s*$",
                       body, re.MULTILINE)
    seen, out = set(), []
    for n in names:
        if n not in seen and n not in {"EXIT", "GOBACK"}:
            seen.add(n)
            out.append(n)
    return out


def ws_items(src: str) -> dict[str, list[str]]:
    """01/77-level names in WORKING-STORAGE, mapped to their child field names.

    A bare group container (e.g. ``01 CONTROL-TOTALS.``) counts as covered
    when every non-FILLER child appears in the page, even if the umbrella
    name itself was rendered as descriptive prose (common in zh output).
    """
    m = re.search(r"WORKING-STORAGE\s+SECTION", src, re.IGNORECASE)
    n = re.search(r"PROCEDURE\s+DIVISION", src, re.IGNORECASE)
    body = src[m.end(): n.start() if n else None] if m else ""
    items: dict[str, list[str]] = {}
    current = None
    for line in body.split("\n"):
        top = re.match(r"^\s*(?:01|77)\s+([A-Z0-9][A-Z0-9-]+)", line)
        if top:
            current = top.group(1)
            items.setdefault(current, [])
            continue
        child = re.match(r"^\s*\d\d\s+([A-Z0-9][A-Z0-9-]+)", line)
        if child and current and child.group(1) != "FILLER":
            items[current].append(child.group(1))
    return items


def ws_item_covered(name: str, children: list[str], content: str) -> bool:
    """An item is covered if its name appears, or all its children do."""
    if name in content:
        return True
    return bool(children) and all(c in content for c in children)


def check_headings(content: str) -> list[str]:
    """Return the list of missing required headings.

    Language-sensitive: if fewer than half the English headings match (e.g. the
    page was generated in zh-TW), fall back to the language-neutral numeric
    section prefixes ``## 1.`` … ``## 12.`` before declaring a failure.
    """
    lower = content.lower()
    missing = [h for h in REQUIRED_HEADINGS if h.lower() not in lower]
    if len(missing) > len(REQUIRED_HEADINGS) / 2:
        # Probably non-English output: score against numeric headings instead.
        numeric_missing = [h for h in NUMERIC_HEADINGS if h not in content]
        if len(numeric_missing) < len(missing):
            return numeric_missing
    return missing


def main():
    cache_path, page_id, source_path = sys.argv[1], sys.argv[2], sys.argv[3]
    cache = json.load(open(cache_path))
    page = cache["generated_pages"].get(page_id)
    if not page:
        print(f"FAIL: page {page_id} not in cache; pages: {list(cache['generated_pages'])}")
        sys.exit(1)
    content = page["content"]
    src = open(source_path, encoding="utf-8", errors="replace").read()

    ok = True
    paras = cobol_paragraphs(src)
    missing_p = [p for p in paras if p not in content]
    print(f"paragraph coverage: {len(paras) - len(missing_p)}/{len(paras)}"
          + (f"  MISSING: {missing_p}" if missing_p else ""))
    ok &= not missing_p

    items = ws_items(src)
    missing_w = [w for w, kids in items.items() if not ws_item_covered(w, kids, content)]
    print(f"working-storage 01/77 coverage: {len(items) - len(missing_w)}/{len(items)}"
          + (f"  MISSING: {missing_w}" if missing_w else ""))
    ok &= not missing_w

    missing_h = check_headings(content)
    total_h = len(REQUIRED_HEADINGS)
    print(f"required headings: {total_h - len(missing_h)}/{total_h}"
          + (f"  MISSING: {missing_h}" if missing_h else ""))
    ok &= not missing_h

    print(f"page length: {len(content)} chars (minimum {MIN_CHARS})")
    ok &= len(content) >= MIN_CHARS

    print("RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
