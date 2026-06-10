"""Parity tests — verify that load-bearing anchor strings survived the Python port.

The anchor strings are taken verbatim from the TypeScript source files:
  src/app/[owner]/[repo]/page.tsx  (structure prompt, standard/deep-dive prompts)
  src/utils/wikiRevision.ts        (self-review prompt, parseRevisedContent)
"""
from api.wiki_prompts import (
    build_page_prompt,
    build_self_review_prompt,
    build_structure_prompt,
    language_clause,
    generate_file_url,
    parse_revised_content,
)


# ---------------------------------------------------------------------------
# language_clause helper
# ---------------------------------------------------------------------------

def test_language_clause_known():
    assert language_clause("zh-tw") == "Traditional Chinese (繁體中文)"
    assert language_clause("en") == "English"
    assert language_clause("ja") == "Japanese (日本語)"
    assert language_clause("pt-br") == "Brazilian Portuguese (Português Brasileiro)"


def test_language_clause_unknown_falls_back():
    assert language_clause("xx") == "English"


# ---------------------------------------------------------------------------
# Structure prompt
# ---------------------------------------------------------------------------

def test_structure_prompt_anchors():
    p = build_structure_prompt("file_tree", "readme", "o", "r", "zh-tw", True)
    assert "Return ONLY the valid XML structure" in p
    assert "18-30 pages total" in p
    assert "Traditional Chinese (繁體中文)" in p
    assert "<wiki_structure>" in p


def test_structure_prompt_concise_branch():
    p = build_structure_prompt("t", "r", "o", "r", "en", False)
    assert "4-6 pages" in p


def test_structure_prompt_comprehensive_has_sections_xml():
    p = build_structure_prompt("t", "r", "o", "r", "en", True)
    assert "<sections>" in p
    assert "<section id=" in p


def test_structure_prompt_concise_no_sections():
    p = build_structure_prompt("t", "r", "o", "r", "en", False)
    assert "<sections>" not in p


def test_structure_prompt_korean_mixed_script():
    # The structure prompt verbatim uses mixed-script "한国語" (not pure Hangul)
    # — this matches the TS source exactly.
    p = build_structure_prompt("t", "r", "o", "r", "kr", True)
    assert "Korean (한国語)" in p


def test_structure_prompt_interpolates_owner_repo():
    p = build_structure_prompt("ft", "rm", "myowner", "myrepo", "en", False)
    assert "myowner/myrepo" in p


def test_structure_prompt_interpolates_file_tree_and_readme():
    p = build_structure_prompt("THE_TREE", "THE_README", "o", "r", "en", False)
    assert "THE_TREE" in p
    assert "THE_README" in p


# ---------------------------------------------------------------------------
# Page prompts (standard + deep-dive)
# ---------------------------------------------------------------------------

REPO = ("https://github.com/o/r", "github", "main")  # repo_url, repo_type, default_branch


def test_page_prompt_anchors():
    p = build_page_prompt("Core Features", ["a.py", "b.py"], "en", False, *REPO)
    assert "expert technical writer and software architect" in p
    assert "<details>" in p
    assert "NEVER use flowchart-style labels" in p
    # Standard prompt mentions the title in the H1 instruction
    assert "# Core Features" in p


def test_page_prompt_deep_dive_wiki_page_topic():
    # [WIKI_PAGE_TOPIC]: <title> appears in the deep-dive prompt
    p = build_page_prompt("Core Features", ["a.py", "b.py"], "en", True, *REPO)
    assert "[WIKI_PAGE_TOPIC]: Core Features" in p
    assert "[CURRENT_FILE_CONTENT]: provided in the request context." in p


def test_page_prompt_five_plus_files_keeps_original_clauses():
    paths = [f"src/f{i}.py" for i in range(5)]
    p = build_page_prompt("P", paths, "en", False, *REPO)
    assert "You MUST use AT LEAST 5 relevant source files" in p
    assert "There MUST be AT LEAST 5 source files listed" in p
    assert "<!-- Add additional relevant files if fewer than 5 were provided -->" in p
    assert "You MUST cite AT LEAST 5 different source files" in p


def test_page_prompt_few_files_relaxes_clauses():
    # Small repos: pages get 1-2 filePaths and generation is retrieval-free,
    # so the >=5 demand is unsatisfiable; strict models (gpt-oss-120b) refused
    # the page outright. Deliberate deviation from the verbatim port.
    p = build_page_prompt("P", ["a.py", "b.py"], "en", False, *REPO)
    assert "AT LEAST 5" not in p
    assert "Use ALL of the provided source files" in p
    assert "do NOT invent or add files that were not provided" in p
    assert "Do NOT refuse, apologize, or truncate the page" in p
    assert "<!-- Add additional relevant files" not in p


def test_deep_dive_prompt_differs():
    assert build_page_prompt("P", ["x"], "en", True, *REPO) != \
           build_page_prompt("P", ["x"], "en", False, *REPO)


def test_deep_dive_prompt_anchors():
    p = build_page_prompt("My Program", ["prog.cbl"], "en", True, *REPO)
    assert "senior mainframe/COBOL systems analyst" in p
    assert "## 1. Program Identification" in p
    assert "## 6. Paragraph-by-Paragraph Analysis" in p
    assert "NEVER use flowchart-style labels" in p
    assert "NEVER use \"graph LR\"" in p


def test_deep_dive_prompt_describes_line_numbering():
    p = build_page_prompt("My Program", ["prog.cbl"], "en", True, *REPO)
    # The model is told the source is pre-numbered and to cite those numbers.
    assert "Each line in [CURRENT_FILE_CONTENT] is prefixed with its line number" in p
    assert "Cite those exact line numbers" in p


def test_deep_dive_prompt_forbids_fabricated_file_citations():
    p = build_page_prompt("My Program", ["prog.cbl"], "en", True, *REPO)
    assert "Only cite files that were actually provided" in p
    # CALL targets must be written as program identifiers, not invented files.
    assert "is a program name, not a file" in p
    assert "Never fabricate a filename" in p
    # Section 10 reinforces the rule at the point of use.
    assert "do NOT turn a called program into a file citation" in p


def test_page_prompt_language_clause():
    p = build_page_prompt("T", ["f.py"], "zh-tw", False, *REPO)
    assert "Traditional Chinese (繁體中文)" in p


def test_page_prompt_language_clause_deep_dive():
    p = build_page_prompt("T", ["f.py"], "ja", True, *REPO)
    assert "Japanese (日本語)" in p


# ---------------------------------------------------------------------------
# File-URL builder
# ---------------------------------------------------------------------------

def test_file_url_per_provider():
    assert generate_file_url("https://github.com/o/r", "github", "a/b.py", "main") \
        == "https://github.com/o/r/blob/main/a/b.py"
    assert "/-/blob/" in generate_file_url("https://gitlab.x/o/r", "gitlab", "a.py", "dev")
    assert "/src/" in generate_file_url("https://bitbucket.org/o/r", "bitbucket", "a.py", "main")


def test_file_url_local_returns_path():
    assert generate_file_url("", "local", "some/file.cbl", "main") == "some/file.cbl"


def test_file_url_gitlab_format():
    url = generate_file_url("https://gitlab.com/o/r", "gitlab", "src/main.py", "dev")
    assert url == "https://gitlab.com/o/r/-/blob/dev/src/main.py"


def test_file_url_bitbucket_format():
    url = generate_file_url("https://bitbucket.org/o/r", "bitbucket", "src/main.py", "main")
    assert url == "https://bitbucket.org/o/r/src/main/src/main.py"


# ---------------------------------------------------------------------------
# Page prompt embeds file links
# ---------------------------------------------------------------------------

def test_page_prompt_embeds_file_links():
    p = build_page_prompt("P", ["a/b.py"], "en", False, *REPO)
    assert "[a/b.py](https://github.com/o/r/blob/main/a/b.py)" in p


def test_deep_dive_prompt_embeds_file_links():
    p = build_page_prompt("P", ["prog.cbl"], "en", True,
                          "https://github.com/o/r", "github", "main")
    assert "[prog.cbl](https://github.com/o/r/blob/main/prog.cbl)" in p


# ---------------------------------------------------------------------------
# Self-review prompt
# ---------------------------------------------------------------------------

def test_self_review_prompt_contract():
    p = build_self_review_prompt("P", ["x.py"], "page body", "https://g/o/r")
    assert "NO_CHANGES" in p
    assert "COMPLETE corrected page" in p


def test_self_review_prompt_anchors():
    p = build_self_review_prompt("MyPage", ["a.py", "b.py"], "content here",
                                 "https://github.com/o/r")
    # Verbatim from wikiRevision.ts
    assert "verify the page against it with fresh eyes" in p
    assert "Correct any factual errors" in p
    assert "<page title=" in p
    assert "https://github.com/o/r" in p
    assert "MyPage" in p
    assert "a.py, b.py" in p
    assert "content here" in p


# ---------------------------------------------------------------------------
# RAG query builder
# ---------------------------------------------------------------------------

def test_rag_query_basic():
    from api.wiki_prompts import build_page_rag_query
    q = build_page_rag_query("Overview", ["a.py", "b.py"])
    assert "Overview" in q
    assert "a.py" in q
    assert "b.py" in q


def test_rag_query_caps_at_4000():
    from api.wiki_prompts import build_page_rag_query
    many_files = [f"file_{i}.py" for i in range(100)]
    q = build_page_rag_query("T", many_files)
    assert len(q) <= 4000


def test_rag_query_uses_first_30_files():
    from api.wiki_prompts import build_page_rag_query
    files = [f"f{i}.py" for i in range(50)]
    q = build_page_rag_query("T", files)
    assert "f29.py" in q
    # File 31 may or may not be present (cap might include it as comma text),
    # but the join is only the first 30.
    assert "f30.py" not in q


# ---------------------------------------------------------------------------
# parseRevisedContent safety guards
# ---------------------------------------------------------------------------

def test_parse_revised_content_guards():
    page = "# T\n\n```mermaid\ngraph TD\n```"
    assert parse_revised_content(page, "NO_CHANGES") == (page, False)
    assert parse_revised_content(page, "Error: boom") == (page, False)
    assert parse_revised_content(page, "tiny") == (page, False)        # <30%
    fixed = page + "\n\nExtra corrected paragraph for length purposes."
    assert parse_revised_content(page, fixed) == (fixed, True)
    # a response ending in a fence with no ```markdown wrapper keeps its fence
    content, changed = parse_revised_content("x" * 10, "y" * 20 + "\n```mermaid\na\n```")
    assert content.endswith("```")


def test_parse_revised_content_markdown_unwrap():
    original = "# Hello\n\nSome content here that is long enough to pass the 30% guard."
    wrapped = "```markdown\n# Hello\n\nSome content here that is long enough to pass the 30% guard.\n```"
    content, changed = parse_revised_content(original, wrapped)
    assert not content.startswith("```")
    assert not content.endswith("```")


def test_parse_revised_content_empty_response():
    page = "some content"
    assert parse_revised_content(page, "") == (page, False)
    assert parse_revised_content(page, "   ") == (page, False)


def test_parse_revised_content_no_changes_variants():
    page = "some content"
    # NO_CHANGES with trailing period or space — matches .{0,10} regex
    assert parse_revised_content(page, "NO_CHANGES.") == (page, False)
    assert parse_revised_content(page, "NO_CHANGES ") == (page, False)


def test_parse_revised_content_identical():
    page = "  some content  "
    # cleaned == original.strip() → unchanged
    assert parse_revised_content(page, "some content") == (page, False)


def test_parse_revised_content_returns_cleaned():
    original = "short"
    # Response is long enough (>30%) and different
    response = "a" * 100
    content, changed = parse_revised_content(original, response)
    assert changed
    assert content == response
