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


NONE_TOKENS = {"none", "無", "無相關", "無相關資訊", "n/a", "na",
               "not applicable", "-"}


def _is_none_body(body: str) -> bool:
    """True when a section body is empty or one of the none-tokens."""
    s = (body or "").strip().strip("*_`> ").strip().rstrip(".。 ").strip()
    if not s:
        return True
    return s.casefold() in {t.casefold() for t in NONE_TOKENS}


def classify_page(page_id: str, content: str, outline: str) -> dict:
    """Classify every required heading (and enumerated row) of one page."""
    gen = _parse_generated_headings(content)
    by_anchor = {}
    for h in gen:
        by_anchor.setdefault(h["anchor"], h)  # first match wins

    headings = []
    for req in parse_required_headings(outline):
        gh = by_anchor.get(_normalize(req["label"]))
        entry = {"label": req["label"], "level": req["level"]}
        if gh is None:
            entry["status"] = "missing"
        else:
            entry["status"] = "empty_none" if _is_none_body(gh["body"]) else "content"
        if (page_id, req["label"]) in ENUMERATED_SECTIONS:
            body_norm = _normalize(gh["body"]) if gh else ""
            # Guard against an empty-normalized label (e.g. a future CJK-only
            # row) matching everything via "" in body_norm.
            entry["rows"] = [
                {"label": r,
                 "status": "present" if _normalize(r) and _normalize(r) in body_norm
                           else "missing"}
                for r in parse_required_rows(outline, req["label"])]
        headings.append(entry)
    return {"id": page_id, "present": True, "headings": headings}


def _document_of(page_id: str) -> "str | None":
    """Which template document a page id belongs to."""
    if page_id.startswith("page-tsd-"):
        return "TSD"
    if page_id.startswith("page-brd-"):
        return "BRD"
    return None


# Canonical intended headers, extracted once from the PCALT TSD/BRD template
# TOCs (PCALT_TSD_Template / PCALT_BRD_Template). Committed here so the coverage
# guard does not depend on the untracked .docx files. Example-placeholder rows
# (Physical file XXXXPF, Program1/2/3, FR-0001/0002) are intentionally excluded.
TSD_BRD_TEMPLATE_HEADERS = {
    "TSD": [
        "Introduction", "Scope", "Assumptions", "Inclusions", "Exclusions",
        "Constraints", "Functional Specification", "Functional Requirements",
        "Non-Functional Requirements", "System Overview", "System Platform",
        "Impact Analysis", "Program Flow", "Security Control",
        "Identity and Access Management", "Log and Event Management",
        "Encryption", "Network", "Database Security", "Application Security",
        "General Security", "System Interface", "Database Design/Change",
        "Table Change", "Program Change", "Schedule Change", "Appendix",
    ],
    "BRD": [
        "Background", "Boundaries", "Business Requirements",
        "Functional Requirements", "Non-Functional Requirements",
        "Security Control", "Reference", "Scope", "Assumptions", "Constraints",
        "Current Processing", "Requirement Specification",
        "Business Flow Diagram", "Data Archive and Housekeeping",
        "Performance Requirements", "Capacity Requirements",
        "Availability Requirements", "Reliability Requirements",
        "Usability Requirements", "Other Requirements",
        "Identity and Access Management", "Log and Event Management",
        "Encryption", "Network", "Database Security", "Application Security",
        "General Security", "Definition of Terminologies", "Attachment",
    ],
}

# A few template headers use different wording than the outline/page ids they map
# to. Map template header -> a term the outline uses, so coverage matches.
_AUDIT_ALIASES = {
    "Program Change": "program inventory",
    "Schedule Change": "schedule batch processing",
}


def _coverage_tokens(document: str, outlines: dict) -> set:
    """Normalized tokens that can satisfy a template header for a document:
    every H2/H3 label plus each page-id tail."""
    toks = set()
    for pid, outline in outlines.items():
        if _document_of(pid) != document:
            continue
        toks.add(_normalize(pid.replace("page-tsd-", "").replace("page-brd-", "")))
        for h in parse_required_headings(outline):
            toks.add(_normalize(h["label"]))
    return {t for t in toks if t}


def uncovered_template_headers(headers: dict = TSD_BRD_TEMPLATE_HEADERS,
                               outlines: dict = TSD_BRD_OUTLINES) -> dict:
    """Per document, the template headers NOT represented in the outlines.
    A header is covered when its normalized form (or its alias's) is a substring
    of some coverage token, or vice versa. Empty dict => fully covered.

    The bidirectional substring match is intentionally lenient so template
    wording like "Table Change" matches the outline's "Table Changes" and
    page-id tails ("functionalspec" ⊂ "functionalspecification") count. This
    trades some strictness for tolerance of minor wording drift; it is verified
    to return {} against the current outlines. If a future edit needs a tighter
    guard, switch to exact-match + an expanded alias table."""
    out = {}
    for doc, hs in headers.items():
        toks = _coverage_tokens(doc, outlines)
        missing = []
        for h in hs:
            cand = _normalize(_AUDIT_ALIASES.get(h, h))
            if not any(cand in t or t in cand for t in toks):
                missing.append(h)
        if missing:
            out[doc] = missing
    return out


def check_tsd_brd_completeness(pages: list, outlines: dict = TSD_BRD_OUTLINES) -> dict:
    """Build the completeness report over the template TSD/BRD pages."""
    by_id = {p.get("id"): p for p in (pages or [])}
    headings_count = {"content": 0, "empty_none": 0, "missing": 0}
    rows_count = {"present": 0, "missing": 0}
    by_document = {
        "TSD": {"headings": {"content": 0, "empty_none": 0, "missing": 0},
                "rows": {"present": 0, "missing": 0}},
        "BRD": {"headings": {"content": 0, "empty_none": 0, "missing": 0},
                "rows": {"present": 0, "missing": 0}},
    }
    pages_missing, page_reports = [], []

    for pid, outline in outlines.items():
        doc = _document_of(pid)
        page = by_id.get(pid)
        if page is None:
            pages_missing.append(pid)
            reqs = parse_required_headings(outline)
            page_reports.append({
                "id": pid, "title": None, "present": False,
                "headings": [{"label": r["label"], "level": r["level"],
                              "status": "missing"} for r in reqs]})
            headings_count["missing"] += len(reqs)
            if doc:
                by_document[doc]["headings"]["missing"] += len(reqs)
            continue
        report = classify_page(pid, page.get("content") or "", outline)
        report["title"] = page.get("title")
        page_reports.append(report)
        for h in report["headings"]:
            headings_count[h["status"]] += 1
            if doc:
                by_document[doc]["headings"][h["status"]] += 1
            for row in h.get("rows", []):
                rows_count[row["status"]] += 1
                if doc:
                    by_document[doc]["rows"][row["status"]] += 1

    return {"summary": {"headings": headings_count, "rows": rows_count,
                        "pages_missing": pages_missing, "by_document": by_document},
            "pages": page_reports}


_MARK = {"content": "✓", "empty_none": "○", "missing": "✗",
         "present": "✓"}


def completeness_report_paths(cache_path: str) -> tuple:
    """(json, md) report paths beside a wikicache path ending in .json."""
    base = cache_path[:-5] if cache_path.endswith(".json") else cache_path
    return base + ".completeness.json", base + ".completeness.md"


def _render_page(lines: list, page: dict) -> None:
    title = page.get("title") or page["id"]
    lines.append(f"### {title} (`{page['id']}`)")
    if not page.get("present", True):
        lines.append("_Page missing from generated wiki._")
    for hd in page["headings"]:
        indent = "  " * (hd["level"] - 2) if hd["level"] >= 2 else ""
        lines.append(f"- {indent}{_MARK[hd['status']]} {hd['label']}")
        for row in hd.get("rows", []):
            lines.append(f"    - {_MARK[row['status']]} {row['label']}")
    lines.append("")


def render_markdown_report(report: dict) -> str:
    """Human-readable Markdown grouped into TSD and BRD sections."""
    bd = report["summary"]["by_document"]
    lines = ["# TSD/BRD Completeness Report", ""]
    missing_pages = report["summary"]["pages_missing"]
    if missing_pages:
        lines += ["**Pages entirely missing:** " + ", ".join(missing_pages), ""]
    for doc in ("TSD", "BRD"):
        h = bd[doc]["headings"]
        r = bd[doc]["rows"]
        lines.append(f"## {doc} — {h['content']} ok / {h['empty_none']} None / "
                     f"{h['missing']} MISSING (rows {r['present']}/"
                     f"{r['present'] + r['missing']})")
        lines.append("")
        for page in report["pages"]:
            if _document_of(page["id"]) == doc:
                _render_page(lines, page)
    return "\n".join(lines)
