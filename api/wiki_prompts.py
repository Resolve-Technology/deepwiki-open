"""Generation prompts, ported verbatim from the frontend (single source now).

Any wording change here changes generation quality — edit deliberately.
"""
import re
import subprocess
import os
from typing import List, Optional

LANGUAGE_NAMES = {
    "en": "English",
    "ja": "Japanese (日本語)",
    "zh": "Mandarin Chinese (中文)",
    "zh-tw": "Traditional Chinese (繁體中文)",
    "es": "Spanish (Español)",
    "kr": "Korean (한국어)",
    "vi": "Vietnamese (Tiếng Việt)",
    "pt-br": "Brazilian Portuguese (Português Brasileiro)",
    "fr": "Français (French)",
    "ru": "Русский (Russian)",
}

NO_CHANGES_TOKEN = "NO_CHANGES"


def language_clause(language: str) -> str:
    """Return the human-readable language name for the given code."""
    return LANGUAGE_NAMES.get(language, "English")


def generate_file_url(repo_url: str, repo_type: str, file_path: str,
                      default_branch: str) -> str:
    """Port of page.tsx generateFileUrl (github blob / gitlab -/blob / bitbucket src)."""
    if repo_type == "local":
        return file_path
    if not repo_url:
        return file_path
    try:
        base = repo_url.rstrip("/")
        if base.endswith(".git"):
            base = base[:-len(".git")]
        # Detect by hostname substrings, same as TS
        if "github" in base:
            return f"{base}/blob/{default_branch}/{file_path}"
        elif "gitlab" in base:
            return f"{base}/-/blob/{default_branch}/{file_path}"
        elif "bitbucket" in base:
            return f"{base}/src/{default_branch}/{file_path}"
    except Exception:
        pass
    return file_path


def get_clone_default_branch(owner: str, repo: str, repo_type: str,
                             local_path: Optional[str] = None) -> str:
    """Branch name from the clone's HEAD; falls back to 'main'."""
    try:
        if local_path:
            clone_dir = local_path
        else:
            from adalflow.utils import get_adalflow_default_root_path
            root = get_adalflow_default_root_path()
            clone_dir = os.path.join(root, "repos", f"{owner}_{repo}")
        result = subprocess.run(
            ["git", "-C", clone_dir, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        branch = result.stdout.strip()
        if branch:
            return branch
    except Exception:
        pass
    return "main"


# ---------------------------------------------------------------------------
# Structure prompt (ported from determineWikiStructure in page.tsx)
# NOTE: the structure prompt uses "한国語" (mixed script) for Korean — verbatim
# from the TS source at that location.
# ---------------------------------------------------------------------------

def build_structure_prompt(file_tree: str, readme: str, owner: str, repo: str,
                           language: str, comprehensive: bool) -> str:
    lang_name = (
        "English" if language == "en" else
        "Japanese (日本語)" if language == "ja" else
        "Mandarin Chinese (中文)" if language == "zh" else
        "Traditional Chinese (繁體中文)" if language == "zh-tw" else
        "Spanish (Español)" if language == "es" else
        "Korean (한国語)" if language == "kr" else
        "Vietnamese (Tiếng Việt)" if language == "vi" else
        "Brazilian Portuguese (Português Brasileiro)" if language == "pt-br" else
        "Français (French)" if language == "fr" else
        "Русский (Russian)" if language == "ru" else
        "English"
    )

    if comprehensive:
        body_section = (
            "\n"
            "Produce a SINGLE wiki that contains THREE top-level documents, each as its own top-level section (use subsections for the document's internal structure). Document the EXISTING system as-is (these are documentation views, not a change request). Only include a page/section if the repository content supports it. The three top-level sections, in this order:\n"
            "\n"
            "=== Top-level section 1: \"📘 Wiki\" (general technical wiki) ===\n"
            "A conventional developer wiki of the codebase:\n"
            "- Overview (general information about the project)\n"
            "- System Architecture (how the system is designed)\n"
            "- Core Features (key functionality)\n"
            "- Data Management/Flow (how data is stored, processed, accessed, and managed)\n"
            "- Components / Programs (the main programs or modules and what they do)\n"
            "- Deployment/Infrastructure (how it runs / is deployed, if evident)\n"
            "\n"
            "=== Top-level section 2: \"📐 TSD\" (Technical Specification Document) ===\n"
            "A formal technical spec of the existing system, with these subsections:\n"
            "- Introduction (purpose and high-level overview)\n"
            "- Scope (Assumptions; Inclusions — Inputs, Outputs, Interfaces, Online/Batch Processing, Scheduling, Error Handling, Access Restrictions; Exclusions; Constraints)\n"
            "- Functional Specification (functional and non-functional behavior — what each program does)\n"
            "- System Overview (System Platform e.g. AS400/LifeAsia/DB2-400/OS400 if evident; Impact Analysis — a table of impact items, all \"None\" for an as-is system unless evident; Program Flow; Security Control — Identity and Access Management, Log and Event Management, Encryption, Network, Database Security, Application Security, General Security; System Interface). Keep ALL these headings even when empty (write \"None\").\n"
            "- Database Design (Physical files [PF], Logical files [LF], copybooks and their record/data structures, Table definitions)\n"
            "- Program Inventory (one page per major program/group: business function and key logic, e.g. \"CAL101\", \"GETPLONREC\")\n"
            "- Schedule / Batch Processing (batch jobs and scheduling)\n"
            "- Appendix\n"
            "\n"
            "=== Top-level section 3: \"📋 BRD\" (Business and Functional Requirement) ===\n"
            "A business-oriented requirements document inferred from the system's behavior:\n"
            "- Background (business context and purpose of the system)\n"
            "- Boundaries (Scope — Inclusions/Exclusions; Assumptions; Constraints)\n"
            "- Business Requirements (Current Processing; Requirement Specification — list as BR# items; Business Flow Diagram; Data Archive and Housekeeping)\n"
            "- Functional Requirements (list as FR# items, each mapped to a BR#)\n"
            "- Non-Functional Requirements (Performance, Capacity, Availability, Reliability, Usability, Other)\n"
            "- Security Control (IAM, Log and Event Management, Encryption, Network, Database Security, Application Security, General Security)\n"
            "- Reference (Definition of Terminologies; Attachments)\n"
            "\n"
            "For the TSD and BRD pages above, use these EXACT page ids: page-tsd-introduction, "
            "page-tsd-scope, page-tsd-functional-spec, page-tsd-system-overview, "
            "page-tsd-database-design, page-tsd-program-inventory, page-tsd-batch-processing, "
            "page-tsd-appendix, page-brd-background, page-brd-boundaries, "
            "page-brd-business-requirements, page-brd-functional-requirements, "
            "page-brd-non-functional-requirements, page-brd-security-control, page-brd-reference.\n"
            "\n"
            "=== Top-level section 4: \"🔬 Program Analysis\" (per-program deep dive) ===\n"
            "EXACTLY ONE page per program source file in the repository (a program source file is any COBOL/RPG/source member — e.g. *.cbl, *.cob, *.rpg, or *.txt files whose content is program source). Rules for these pages:\n"
            "- The page id MUST follow the pattern \"page-analysis-<program-name-lowercase>\" (e.g. \"page-analysis-bv401\").\n"
            f"- The page title MUST be the {lang_name} translation of \"Program Deep Dive: <PROGRAM-NAME>\", keeping <PROGRAM-NAME> in its original Latin form (translate only the \"Program Deep Dive\" label).\n"
            "- relevant_files MUST contain EXACTLY the one source file for that program (plus its copybook files if they exist as separate files in the repository).\n"
            "- importance MUST be \"high\".\n"
            "- Do NOT create analysis pages for non-program files (READMEs, JCL listings, data files), and do NOT merge multiple programs into one page.\n"
            "\n"
            "Each top-level section should contain its own pages/subsections. The same underlying source files may be cited across all three documents — that is expected, since each presents the system from a different angle (developer wiki / technical spec / business requirements).\n"
            "\n"
            "Return your analysis in the following XML format:\n"
            "\n"
            "<wiki_structure>\n"
            "  <title>[Overall title for the wiki]</title>\n"
            "  <description>[Brief description of the repository]</description>\n"
            "  <sections>\n"
            "    <section id=\"section-1\">\n"
            "      <title>[Section title]</title>\n"
            "      <pages>\n"
            "        <page_ref>page-1</page_ref>\n"
            "        <page_ref>page-2</page_ref>\n"
            "      </pages>\n"
            "      <subsections>\n"
            "        <section_ref>section-2</section_ref>\n"
            "      </subsections>\n"
            "    </section>\n"
            "    <!-- More sections as needed -->\n"
            "  </sections>\n"
            "  <pages>\n"
            "    <page id=\"page-1\">\n"
            "      <title>[Page title]</title>\n"
            "      <description>[Brief description of what this page will cover]</description>\n"
            "      <importance>high|medium|low</importance>\n"
            "      <relevant_files>\n"
            "        <file_path>[Path to a relevant file]</file_path>\n"
            "        <!-- More file paths as needed -->\n"
            "      </relevant_files>\n"
            "      <related_pages>\n"
            "        <related>page-2</related>\n"
            "        <!-- More related page IDs as needed -->\n"
            "      </related_pages>\n"
            "      <parent_section>section-1</parent_section>\n"
            "    </page>\n"
            "    <!-- More pages as needed -->\n"
            "  </pages>\n"
            "</wiki_structure>\n"
        )
        pages_count = '18-30 pages total spread across the Wiki, TSD and BRD documents, PLUS exactly one "🔬 Program Analysis" page per program source file (these do not count toward the 18-30)'
        wiki_type = "comprehensive"
    else:
        body_section = (
            "\n"
            "Return your analysis in the following XML format:\n"
            "\n"
            "<wiki_structure>\n"
            "  <title>[Overall title for the wiki]</title>\n"
            "  <description>[Brief description of the repository]</description>\n"
            "  <pages>\n"
            "    <page id=\"page-1\">\n"
            "      <title>[Page title]</title>\n"
            "      <description>[Brief description of what this page will cover]</description>\n"
            "      <importance>high|medium|low</importance>\n"
            "      <relevant_files>\n"
            "        <file_path>[Path to a relevant file]</file_path>\n"
            "        <!-- More file paths as needed -->\n"
            "      </relevant_files>\n"
            "      <related_pages>\n"
            "        <related>page-2</related>\n"
            "        <!-- More related page IDs as needed -->\n"
            "      </related_pages>\n"
            "    </page>\n"
            "    <!-- More pages as needed -->\n"
            "  </pages>\n"
            "</wiki_structure>\n"
        )
        pages_count = "4-6 pages"
        wiki_type = "concise"

    return (
        f"Analyze this GitHub repository {owner}/{repo} and create a wiki structure for it.\n"
        "\n"
        "1. The complete file tree of the project:\n"
        "<file_tree>\n"
        f"{file_tree}\n"
        "</file_tree>\n"
        "\n"
        "2. The README file of the project:\n"
        "<readme>\n"
        f"{readme}\n"
        "</readme>\n"
        "\n"
        "I want to create a wiki for this repository. Determine the most logical structure for a wiki based on the repository's content.\n"
        "\n"
        f"IMPORTANT: The wiki content will be generated in {lang_name} language.\n"
        f"IMPORTANT: Write EVERY <title> and <description> in the XML — all section titles AND all "
        f"page titles — in {lang_name}, since they appear in the navigation menu. Do NOT leave any "
        f"title in English. The four top-level section titles MUST keep their emoji prefix and be "
        f"written in {lang_name}: 📘 for the developer Wiki, 📐 for the Technical Specification "
        f"Document (TSD), 📋 for the Business Requirements Document (BRD), 🔬 for the per-program "
        f"analysis — translate the words 'Wiki', 'Technical Specification Document', 'Business "
        f"Requirements Document' and 'Program Analysis' into {lang_name} (you MAY keep the short "
        f"acronym TSD/BRD in parentheses).\n"
        "\n"
        "When designing the wiki structure, include pages that would benefit from visual diagrams, such as:\n"
        "- Architecture overviews\n"
        "- Data flow descriptions\n"
        "- Component relationships\n"
        "- Process workflows\n"
        "- State machines\n"
        "- Class hierarchies\n"
        "\n"
        + body_section +
        "\n\n"
        "IMPORTANT FORMATTING INSTRUCTIONS:\n"
        "- Return ONLY the valid XML structure specified above\n"
        "- DO NOT wrap the XML in markdown code blocks (no ``` or ```xml)\n"
        "- DO NOT include any explanation text before or after the XML\n"
        "- Ensure the XML is properly formatted and valid\n"
        "- Start directly with <wiki_structure> and end with </wiki_structure>\n"
        "\n"
        "IMPORTANT:\n"
        f"1. Create {pages_count} that would make a {wiki_type} wiki for this repository\n"
        "2. Each page should focus on a specific aspect of the codebase (e.g., architecture, key features, setup)\n"
        "3. The relevant_files should be actual files from the repository that would be used to generate that page\n"
        "4. Return ONLY valid XML with the structure specified above, with no markdown code block delimiters"
    )


# ---------------------------------------------------------------------------
# Page prompts (ported from generatePageContent in page.tsx)
# ---------------------------------------------------------------------------

# Canonical sub-structure for the template-driven TSD/BRD pages. The source
# templates (PCALT_TSD / PCALT_BRD) prescribe these headings; they MUST appear
# even when the repository has nothing relevant (the page then states "None").
# Keyed by the fixed page ids mandated in build_structure_prompt; a page without
# an entry here is generated freely as before. `## ` -> H2, `### ` -> H3.
TSD_BRD_OUTLINES = {
    # ============================ TSD ============================
    "page-tsd-introduction": (
        "## Purpose\n"
        "## Document Overview\n"
    ),
    "page-tsd-scope": (
        "## Assumptions\n"
        "## Inclusions\n"
        "## Exclusions\n"
        "## Constraints\n"
    ),
    "page-tsd-functional-spec": (
        "## Functional Requirements\n"
        "## Non-Functional Requirements\n"
    ),
    "page-tsd-system-overview": (
        "## System Platform\n"
        "(Operating system, hardware/midrange, database, and language/runtime evident in the "
        "source — e.g. IBM AS/400 (iSeries), OS/400, DB2/400, LifeAsia, COBOL. None if not evident.)\n"
        "\n"
        "## Impact Analysis\n"
        "Present as a Markdown table with columns: Item | Impact (H / L / None / N/A) | Remarks. "
        "This is an EXISTING system documented as-is, so set Impact to \"None\" for every item "
        "unless the source clearly indicates otherwise. Include one row for EACH of:\n"
        "- Existing applications\n"
        "- Existing batch jobs and schedulers\n"
        "- Existing reports\n"
        "- Existing correspondence or statements\n"
        "- Existing system-wide parameters\n"
        "- Existing code libraries\n"
        "- Existing configuration files\n"
        "- Existing utility programs and scripts\n"
        "- Existing internal interfaces\n"
        "- Existing external interfaces\n"
        "- Existing data extraction, transformation or loading scripts\n"
        "- Existing database designs\n"
        "- Existing hardware (web servers)\n"
        "- Existing hardware (application servers)\n"
        "- Existing hardware (database servers)\n"
        "- Other existing hardware (e.g. load balancers, HA)\n"
        "- Existing network design\n"
        "- Existing network bandwidth\n"
        "- Existing system security policies\n"
        "- Enterprise user access matrix\n"
        "- Other system areas\n"
        "- Existing transaction management module\n"
        "\n"
        "## Program Flow\n"
        "(Overall program / batch control flow; include a Mermaid `graph TD` if evident. None if not evident.)\n"
        "\n"
        "## Security Control\n"
        "### Identity and Access Management\n"
        "### Log and Event Management\n"
        "### Encryption\n"
        "### Network\n"
        "### Database Security\n"
        "### Application Security\n"
        "### General Security\n"
        "\n"
        "## System Interface\n"
        "(Inbound / outbound interfaces: called/calling programs, files exchanged, external systems. None if not evident.)\n"
    ),
    "page-tsd-database-design": (
        "## Physical Files (PF)\n"
        "(One field table per physical file: field name, PIC/type, length, key, description.)\n"
        "## Logical Files (LF)\n"
        "(Each logical file: based-on physical file, key fields, access path.)\n"
        "## Table Changes\n"
    ),
    "page-tsd-program-inventory": (
        "## Program Inventory\n"
        "(One ### subsection per program/module: business function and key logic. "
        "May also summarise as a table: Program | Business Function | Key Logic.)\n"
    ),
    "page-tsd-batch-processing": (
        "## Schedule / Batch Processing\n"
        "(Batch jobs, scheduling, run frequency, dependencies and trigger conditions. None if not evident.)\n"
    ),
    "page-tsd-appendix": (
        "## Appendix\n"
    ),
    # ============================ BRD ============================
    "page-brd-background": (
        "## Background\n"
    ),
    "page-brd-boundaries": (
        "## Scope\n"
        "### Inclusions\n"
        "### Exclusions\n"
        "## Assumptions\n"
        "## Constraints\n"
    ),
    "page-brd-business-requirements": (
        "## Current Processing\n"
        "## Requirement Specification\n"
        "(List as BR# items: BR-0001, BR-0002, ...)\n"
        "## Business Flow Diagram\n"
        "(Mermaid graph TD of the business flow. None if not evident.)\n"
        "## Data Archive and Housekeeping\n"
    ),
    "page-brd-functional-requirements": (
        "## Functional Requirements\n"
        "(Present as a table with columns: BR ID | FR ID | Functional Requirement Description. "
        "Each FR# is mapped to a BR#. None if not evident.)\n"
    ),
    "page-brd-non-functional-requirements": (
        "## Performance Requirements\n"
        "## Capacity Requirements\n"
        "## Availability Requirements\n"
        "## Reliability Requirements\n"
        "## Usability Requirements\n"
        "## Other Requirements\n"
    ),
    "page-brd-security-control": (
        "## Identity and Access Management\n"
        "## Log and Event Management\n"
        "## Encryption\n"
        "## Network\n"
        "## Database Security\n"
        "## Application Security\n"
        "## General Security\n"
    ),
    "page-brd-reference": (
        "## Definition of Terminologies\n"
        "## Attachment\n"
    ),
}


def build_page_prompt(page_title: str, file_paths: List[str], language: str,
                      deep_dive: bool, repo_url: str, repo_type: str,
                      default_branch: str,
                      required_outline: Optional[str] = None) -> str:
    """Build the standard or deep-dive page generation prompt.

    file_paths are linked via generate_file_url so the <details> block
    contains clickable URLs (same as the frontend does).
    """
    lang_name = (
        "English" if language == "en" else
        "Japanese (日本語)" if language == "ja" else
        "Mandarin Chinese (中文)" if language == "zh" else
        "Traditional Chinese (繁體中文)" if language == "zh-tw" else
        "Spanish (Español)" if language == "es" else
        "Korean (한국어)" if language == "kr" else
        "Vietnamese (Tiếng Việt)" if language == "vi" else
        "Brazilian Portuguese (Português Brasileiro)" if language == "pt-br" else
        "Français (French)" if language == "fr" else
        "Русский (Russian)" if language == "ru" else
        "English"
    )

    file_links = "\n".join(
        f"- [{path}]({generate_file_url(repo_url, repo_type, path, default_branch)})"
        for path in file_paths
    )
    first_file = file_paths[0] if file_paths else "source"

    # Deliberate deviation from the verbatim frontend port: the original
    # prompt hard-requires ">= 5 source files" and tells the model to "search
    # the codebase" for more — but generation runs retrieval-free, so on small
    # repos (pages get 1-2 filePaths) that demand is unsatisfiable. Claude
    # shrugs it off; stricter models (gpt-oss-120b) refuse the page outright
    # ("Insufficient source files were provided..."). Relax the clauses when
    # fewer than 5 files were assigned.
    many_files = len(file_paths) >= 5
    given_files_clause = (
        "You MUST use AT LEAST 5 relevant source files for comprehensive coverage - if fewer are provided, search for additional related files in the codebase."
        if many_files else
        "Use ALL of the provided source files; this repository is small, so fewer than 5 files is expected and is NOT a problem."
    )
    details_clause = (
        "There MUST be AT LEAST 5 source files listed - if fewer were provided, you MUST find additional related files to include."
        if many_files else
        "List ALL of the provided source files; do NOT invent or add files that were not provided."
    )
    details_comment = (
        "<!-- Add additional relevant files if fewer than 5 were provided -->\n"
        if many_files else ""
    )
    citation_clause = (
        "    *   IMPORTANT: You MUST cite AT LEAST 5 different source files throughout the wiki page to ensure comprehensive coverage.\n"
        if many_files else
        "    *   IMPORTANT: Cite the provided source files extensively throughout the wiki page — every section must trace back to them. Do NOT refuse, apologize, or truncate the page because fewer than 5 files were provided; write the complete page from the files given.\n"
    )

    # When this page maps to a fixed document-template section, force its exact
    # heading structure and keep empty headings (filled with "None") rather than
    # letting the model drop them.
    outline_clause = ""
    if required_outline:
        outline_clause = (
            "REQUIRED TEMPLATE OUTLINE — this page follows a fixed document template.\n"
            "Reproduce EVERY heading in the [TEMPLATE] block below, in the SAME order and nesting "
            "(`## ` = H2, `### ` = H3). For each heading, write the heading text as the "
            f"{lang_name} translation FOLLOWED BY the exact English template wording in parentheses, "
            "e.g. `## 可用性需求 (Availability Requirements)` and `## 可靠性需求 (Reliability "
            "Requirements)` — this keeps every heading distinct and exactly matching the template. "
            "This documents the EXISTING system as-is (not a change request): you MUST output every "
            "heading even when the source files provide nothing for it — in that case keep the "
            "heading and write exactly \"None\" as its body. A reviewer will check that EVERY "
            "heading below is present, so do NOT drop, merge, rename, reorder, or skip any heading, "
            "even empty or seemingly duplicate ones. You MAY add extra detail or sub-headings under "
            "them where the source supports it.\n"
            "If the template lists REQUIRED TABLE ROWS (e.g. the Impact Analysis "
            "items), write each row's label cell the SAME bilingual way: the "
            f"{lang_name} translation FOLLOWED BY the exact English label in "
            "parentheses, e.g. `現有應用程式 (Existing applications)`, so every "
            "required row is present and matchable.\n"
            "[TEMPLATE]\n"
            + required_outline.strip() + "\n"
            "[/TEMPLATE]\n"
            "\n"
        )

    if deep_dive:
        return (
            "You are a senior mainframe/COBOL systems analyst producing the definitive reference analysis of one program.\n"
            f"You are given the COMPLETE source of the program in [CURRENT_FILE_CONTENT]. Base EVERY statement strictly on that source (plus any copybook files provided). Never invent fields, paragraphs, or behavior. Each line in [CURRENT_FILE_CONTENT] is prefixed with its line number in the form `<number> | <code>`. Cite those exact line numbers for every claim using the format [{first_file}:start-end](); do NOT guess or renumber. Only cite files that were actually provided to you (the file(s) in the 'Relevant source files' list and any copybooks in context). Never fabricate a filename: a CALL target such as `CAL101` is a program name, not a file — write it as `CAL101`, never as a citation link like [CAL101.txt:1-10]().\n"
            "\n"
            "CRITICAL STARTING INSTRUCTION:\n"
            "The very first thing on the page MUST be a `<details>` block listing the source file(s) analyzed:\n"
            "<details>\n"
            "<summary>Relevant source files</summary>\n"
            "\n"
            + file_links + "\n"
            "</details>\n"
            "\n"
            f"Immediately after, the H1 title: `# {page_title}`\n"
            "\n"
            "Then produce ALL of the following numbered sections (every one is REQUIRED; if a section is genuinely not applicable to this program, keep the heading and state in one line why it does not apply):\n"
            "\n"
            "## 1. Program Identification\n"
            "Table: program name, platform (infer from source style, e.g. IBM AS/400), version/date stamps found in source, change/work-unit references found in comments, one-paragraph business purpose.\n"
            "\n"
            "## 2. Environment & File Definitions\n"
            "For EVERY file in SELECT/ASSIGN and FD entries: logical name, physical file/member, record format name, organization, access mode, key fields, open mode used (INPUT/OUTPUT/I-O/EXTEND), and its role (primary input / primary output / update-in-place / control / reference lookup). Group into Input / Output-Update / Reference tables.\n"
            "\n"
            "## 3. Copybooks & Record Layouts\n"
            "Every COPY member and inline record layout: where used, full field table (level, field name, PIC, computed byte length, description inferred from usage). Do not skip filler fields.\n"
            "\n"
            "## 4. Working-Storage Inventory (EXHAUSTIVE)\n"
            "EVERY 01/77-level item and its subordinate fields — no exceptions. That includes the \"boring\" ones: plain CALL-linkage buffer areas (e.g. `01 LSAA-XXX PIC X(1024).`), status/return-code fields, copybook-shaped record buffers, counters, flags, constants, timestamps and work areas, plus any LINKAGE SECTION items. Table columns: field, PIC, length, initial value, purpose, and the paragraphs that read or write it. Group logically (constants / flags / counters / timestamps / record & linkage buffers / work fields). A reviewer will mechanically grep the source for every 01/77 level and fail this page if even one name is absent.\n"
            "\n"
            "## 5. Procedure Division — Complete Paragraph Inventory\n"
            "First a table of EVERY paragraph/SECTION in source order: name, one-line purpose, performed-by (callers), performs (callees), files touched.\n"
            "Then a Mermaid call-graph (graph TD) of the PERFORM structure covering EVERY paragraph.\n"
            "\n"
            "## 6. Paragraph-by-Paragraph Analysis (THE CORE — be exhaustive)\n"
            "One ### subsection PER PARAGRAPH, in source order. Do NOT group or summarize multiple paragraphs together. For each: purpose; trigger/caller; numbered step-by-step logic; every file operation (verb, file, key used, status handling); every condition/branch and what each path does; data transformations (source field → target field); a Mermaid flowchart (graph TD) for any paragraph with branching or loops.\n"
            "\n"
            "## 7. Control & Restart Mechanisms\n"
            "Any checkpoint/timestamp/incremental-processing/commit logic: which fields and files implement it, the exact sequence (Mermaid sequenceDiagram), what happens on abnormal termination, rerun/restart safety analysis.\n"
            "\n"
            "## 8. End-to-End Data Flow\n"
            "Mermaid flowchart (graph TD): every input file → the transformations/decision points → every output/updated file. Follow with a field-level mapping table (output field ← source field/derivation) for the primary output record.\n"
            "\n"
            "## 9. Error Handling Inventory\n"
            "EVERY file-status check, INVALID KEY clause, error flag set/test, error display/abend path: table of location (paragraph + lines), condition detected, and the program's response.\n"
            "\n"
            "## 10. External Dependencies & Cross-Program Relationships\n"
            "Called programs (CALL statements) — list each as its bare program identifier in `backticks` (e.g. `CAL101`); do NOT turn a called program into a file citation unless that program's own source file was actually provided. Callers if inferable from comments, shared files that couple this program to others, JCL/scheduling hints found in comments.\n"
            "\n"
            "## 11. Operational Notes & Gotchas\n"
            "Concrete, evidence-based warnings: rerun/duplicate-processing risks, sort-order assumptions, REWRITE-after-READ requirements, counter overflow limits (compute the actual limit from the PIC), locking/contention, hard-coded values that look like configuration.\n"
            "\n"
            "## 12. Glossary\n"
            "Business and technical terms appearing in the source (field prefixes, file names, domain abbreviations) with their meanings as evidenced by usage.\n"
            "\n"
            "COMPLETENESS RULES (these override brevity):\n"
            "- Section 6 MUST contain one subsection for EVERY paragraph listed in section 5 — a reviewer will diff the two lists.\n"
            "- Section 4 MUST contain EVERY working-storage item — a reviewer will grep the source for 01/77 levels and check.\n"
            "- Prefer tables over prose. Cite line numbers everywhere. This document must exceed the detail of a human-written 8-page program analysis; length is NOT a concern, completeness is.\n"
            "\n"
            "CRITICAL: All diagrams MUST follow strict vertical orientation:\n"
            "       - Use \"graph TD\" (top-down) directive for flow diagrams\n"
            "       - NEVER use \"graph LR\" (left-right)\n"
            "       - Maximum node width should be 3-4 words\n"
            "       - For sequence diagrams:\n"
            "         - Start with \"sequenceDiagram\" directive on its own line\n"
            "         - Define ALL participants at the beginning using \"participant\" keyword\n"
            "         - Optionally specify participant types: actor, boundary, control, entity, database, collections, queue\n"
            "         - Use descriptive but concise participant names, or use aliases: \"participant A as Alice\"\n"
            "         - Use the correct Mermaid arrow syntax (8 types available):\n"
            "           - -> solid line without arrow (rarely used)\n"
            "           - --> dotted line without arrow (rarely used)\n"
            "           - ->> solid line with arrowhead (most common for requests/calls)\n"
            "           - -->> dotted line with arrowhead (most common for responses/returns)\n"
            "           - ->x solid line with X at end (failed/error message)\n"
            "           - -->x dotted line with X at end (failed/error response)\n"
            "           - -) solid line with open arrow (async message, fire-and-forget)\n"
            "           - --) dotted line with open arrow (async response)\n"
            "           - Examples: A->>B: Request, B-->>A: Response, A->xB: Error, A-)B: Async event\n"
            "         - Use +/- suffix for activation boxes: A->>+B: Start (activates B), B-->>-A: End (deactivates B)\n"
            "         - Group related participants using \"box\": box GroupName ... end\n"
            "         - Use structural elements for complex flows:\n"
            "           - loop LoopText ... end (for iterations)\n"
            "           - alt ConditionText ... else ... end (for conditionals)\n"
            "           - opt OptionalText ... end (for optional flows)\n"
            "           - par ParallelText ... and ... end (for parallel actions)\n"
            "           - critical CriticalText ... option ... end (for critical regions)\n"
            "           - break BreakText ... end (for breaking flows/exceptions)\n"
            "         - Add notes for clarification: \"Note over A,B: Description\", \"Note right of A: Detail\"\n"
            "         - Use autonumber directive to add sequence numbers to messages\n"
            "         - NEVER use flowchart-style labels like A--|label|-->B. Always use a colon for labels: A->>B: My Label\n"
            "\n"
            f"IMPORTANT: Generate the content in {lang_name} language.\n"
            "\n"
            f"[WIKI_PAGE_TOPIC]: {page_title}\n"
            "[CURRENT_FILE_CONTENT]: provided in the request context.\n"
        )
    else:
        return (
            "You are an expert technical writer and software architect.\n"
            "Your task is to generate a comprehensive and accurate technical wiki page in Markdown format about a specific feature, system, or module within a given software project.\n"
            "Write in a formal, precise, structured technical-documentation style appropriate to the page's role within its document (a developer Wiki, a Technical Specification Document, or a Business and Functional Requirement document). Where the page documents data structures (copybooks/record layouts, physical/logical files), present them as field tables (field name, type/PIC, length, description); where it documents a program, summarize its business function, inputs/outputs, called modules, and key logic; where it documents a business or functional requirement, state it as numbered BR#/FR# items.\n"
            "\n"
            "You will be given:\n"
            "1. The \"[WIKI_PAGE_TOPIC]\" for the page you need to create.\n"
            f"2. A list of \"[RELEVANT_SOURCE_FILES]\" from the project that you MUST use as the sole basis for the content. You have access to the full content of these files. {given_files_clause}\n"
            "\n"
            "CRITICAL STARTING INSTRUCTION:\n"
            f"The very first thing on the page MUST be a `<details>` block listing ALL the `[RELEVANT_SOURCE_FILES]` you used to generate the content. {details_clause}\n"
            "Format it exactly like this:\n"
            "<details>\n"
            "<summary>Relevant source files</summary>\n"
            "\n"
            "Remember, do not provide any acknowledgements, disclaimers, apologies, or any other preface before the `<details>` block. JUST START with the `<details>` block.\n"
            "The following files were used as context for generating this wiki page:\n"
            "\n"
            + file_links + "\n"
            + details_comment +
            "</details>\n"
            "\n"
            f"Immediately after the `<details>` block, the main title of the page should be a H1 Markdown heading: `# {page_title}`.\n"
            "\n"
            + outline_clause +
            "Based ONLY on the content of the `[RELEVANT_SOURCE_FILES]`:\n"
            "\n"
            f"1.  **Introduction:** Start with a concise introduction (1-2 paragraphs) explaining the purpose, scope, and high-level overview of \"{page_title}\" within the context of the overall project. If relevant, and if information is available in the provided files, link to other potential wiki pages using the format `[Link Text](#page-anchor-or-id)`.\n"
            "\n"
            f"2.  **Detailed Sections:** Break down \"{page_title}\" into logical sections using H2 (`##`) and H3 (`###`) Markdown headings. For each section:\n"
            "    *   Explain the architecture, components, data flow, or logic relevant to the section's focus, as evidenced in the source files.\n"
            "    *   Identify key functions, classes, data structures, API endpoints, or configuration elements pertinent to that section.\n"
            "\n"
            "3.  **Mermaid Diagrams:**\n"
            "    *   EXTENSIVELY use Mermaid diagrams (e.g., `flowchart TD`, `sequenceDiagram`, `classDiagram`, `erDiagram`, `graph TD`) to visually represent architectures, flows, relationships, and schemas found in the source files.\n"
            "    *   Ensure diagrams are accurate and directly derived from information in the `[RELEVANT_SOURCE_FILES]`.\n"
            "    *   Provide a brief explanation before or after each diagram to give context.\n"
            "    *   CRITICAL: All diagrams MUST follow strict vertical orientation:\n"
            "       - Use \"graph TD\" (top-down) directive for flow diagrams\n"
            "       - NEVER use \"graph LR\" (left-right)\n"
            "       - Maximum node width should be 3-4 words\n"
            "       - For sequence diagrams:\n"
            "         - Start with \"sequenceDiagram\" directive on its own line\n"
            "         - Define ALL participants at the beginning using \"participant\" keyword\n"
            "         - Optionally specify participant types: actor, boundary, control, entity, database, collections, queue\n"
            "         - Use descriptive but concise participant names, or use aliases: \"participant A as Alice\"\n"
            "         - Use the correct Mermaid arrow syntax (8 types available):\n"
            "           - -> solid line without arrow (rarely used)\n"
            "           - --> dotted line without arrow (rarely used)\n"
            "           - ->> solid line with arrowhead (most common for requests/calls)\n"
            "           - -->> dotted line with arrowhead (most common for responses/returns)\n"
            "           - ->x solid line with X at end (failed/error message)\n"
            "           - -->x dotted line with X at end (failed/error response)\n"
            "           - -) solid line with open arrow (async message, fire-and-forget)\n"
            "           - --) dotted line with open arrow (async response)\n"
            "           - Examples: A->>B: Request, B-->>A: Response, A->xB: Error, A-)B: Async event\n"
            "         - Use +/- suffix for activation boxes: A->>+B: Start (activates B), B-->>-A: End (deactivates B)\n"
            "         - Group related participants using \"box\": box GroupName ... end\n"
            "         - Use structural elements for complex flows:\n"
            "           - loop LoopText ... end (for iterations)\n"
            "           - alt ConditionText ... else ... end (for conditionals)\n"
            "           - opt OptionalText ... end (for optional flows)\n"
            "           - par ParallelText ... and ... end (for parallel actions)\n"
            "           - critical CriticalText ... option ... end (for critical regions)\n"
            "           - break BreakText ... end (for breaking flows/exceptions)\n"
            "         - Add notes for clarification: \"Note over A,B: Description\", \"Note right of A: Detail\"\n"
            "         - Use autonumber directive to add sequence numbers to messages\n"
            "         - NEVER use flowchart-style labels like A--|label|-->B. Always use a colon for labels: A->>B: My Label\n"
            "\n"
            "4.  **Tables:**\n"
            "    *   Use Markdown tables to summarize information such as:\n"
            "        *   Key features or components and their descriptions.\n"
            "        *   API endpoint parameters, types, and descriptions.\n"
            "        *   Configuration options, their types, and default values.\n"
            "        *   Data model fields, types, constraints, and descriptions.\n"
            "        *   COBOL copybook / record-layout fields (field name, PIC/type, length, description).\n"
            "        *   Program inventory entries (program name, business function, key modifications/logic).\n"
            "\n"
            "5.  **Code Snippets (ENTIRELY OPTIONAL):**\n"
            "    *   Include short, relevant code snippets (e.g., Python, Java, JavaScript, SQL, JSON, YAML) directly from the `[RELEVANT_SOURCE_FILES]` to illustrate key implementation details, data structures, or configurations.\n"
            "    *   Ensure snippets are well-formatted within Markdown code blocks with appropriate language identifiers.\n"
            "\n"
            "6.  **Source Citations (EXTREMELY IMPORTANT):**\n"
            "    *   For EVERY piece of significant information, explanation, diagram, table entry, or code snippet, you MUST cite the specific source file(s) and relevant line numbers from which the information was derived.\n"
            "    *   Place citations at the end of the paragraph, under the diagram/table, or after the code snippet.\n"
            "    *   Use the exact format: `Sources: [filename.ext:start_line-end_line]()` for a range, or `Sources: [filename.ext:line_number]()` for a single line. Multiple files can be cited: `Sources: [file1.ext:1-10](), [file2.ext:5](), [dir/file3.ext]()` (if the whole file is relevant and line numbers are not applicable or too broad).\n"
            "    *   If an entire section is overwhelmingly based on one or two files, you can cite them under the section heading in addition to more specific citations within the section.\n"
            + citation_clause +
            "\n"
            "7.  **Technical Accuracy:** All information must be derived SOLELY from the `[RELEVANT_SOURCE_FILES]`. Do not infer, invent, or use external knowledge about similar systems or common practices unless it's directly supported by the provided code. If information is not present in the provided files, do not include it or explicitly state its absence if crucial to the topic.\n"
            "\n"
            "8.  **Clarity and Conciseness:** Use clear, professional, and concise technical language suitable for other developers working on or learning about the project. Avoid unnecessary jargon, but use correct technical terms where appropriate.\n"
            "\n"
            f"9.  **Conclusion/Summary:** End with a brief summary paragraph if appropriate for \"{page_title}\", reiterating the key aspects covered and their significance within the project.\n"
            "\n"
            f"IMPORTANT: Generate the content in {lang_name} language.\n"
            "\n"
            "Remember:\n"
            "- Ground every claim in the provided source files.\n"
            "- Prioritize accuracy and direct representation of the code's functionality and structure.\n"
            "- Structure the document logically for easy understanding by other developers.\n"
        )


# ---------------------------------------------------------------------------
# RAG query builder (ported from wikiRevision.ts buildPageRagQuery)
# ---------------------------------------------------------------------------

def build_page_rag_query(page_title: str, file_paths: List[str]) -> str:
    """Port of wikiRevision.ts buildPageRagQuery (title + first 30 paths, 4000 cap)."""
    files = ", ".join(file_paths[:30])
    query = f'Source code relevant to documentation page "{page_title}". Key files: {files}'
    return query[:4000]


# ---------------------------------------------------------------------------
# Self-review prompt (ported from wikiRevision.ts buildSelfReviewPrompt)
# ---------------------------------------------------------------------------

def build_self_review_prompt(page_title: str, file_paths: List[str],
                             content: str, repo_url: str) -> str:
    files_joined = ", ".join(file_paths)
    return (
        f"You are reviewing a documentation page that was just generated for the repository {repo_url}. You have access to the repository's actual source code through the provided context — verify the page against it with fresh eyes.\n"
        "\n"
        "Correct any factual errors: wrong claims about behavior, invented functions/APIs/files, incorrect file references, broken mermaid syntax, or missing critical caveats. Keep the page's original structure, level of detail, and language.\n"
        "\n"
        f"If the page is accurate as written, reply with exactly: {NO_CHANGES_TOKEN}\n"
        "Otherwise reply with the COMPLETE corrected page in markdown — no preamble, no explanation of what you changed, no code fence around the whole page.\n"
        "\n"
        f"<page title=\"{page_title}\" files=\"{files_joined}\">\n"
        f"{content}\n"
        "</page>"
    )


# ---------------------------------------------------------------------------
# Citation-fix prompt (correct or remove claims whose citations didn't verify)
# ---------------------------------------------------------------------------

def build_citation_fix_prompt(page_title: str, file_paths: List[str],
                             content: str, broken: List[tuple],
                             repo_url: str) -> str:
    """Prompt the model to fix or remove ONLY the claims whose citations could
    not be found in the repository's actual source.

    ``broken`` is a list of ``(citation_label, reason)`` pairs, e.g.
    ``("ghost.py:9-9", "file not provided")``.
    """
    files_joined = ", ".join(file_paths)
    listed = "\n".join(f"- {label} ({reason})" for label, reason in broken)
    return (
        f"You are correcting a documentation page generated for the repository {repo_url}. "
        "You have access to the repository's actual source code through the provided context.\n"
        "\n"
        "The citations listed below could NOT be found in the repository's actual source — "
        "the file is missing or the cited lines do not exist:\n"
        f"{listed}\n"
        "\n"
        "For EACH listed citation, either correct the claim so it matches the real code (and cite "
        "the correct file and line numbers), or remove the claim entirely. Do NOT add any new claim "
        "that is not directly supported by the provided source. Keep the page's structure, level of "
        "detail, and language.\n"
        "\n"
        "Reply with the COMPLETE corrected page in markdown — no preamble, no explanation, no code "
        "fence around the whole page.\n"
        "\n"
        f"<page title=\"{page_title}\" files=\"{files_joined}\">\n"
        f"{content}\n"
        "</page>"
    )


# ---------------------------------------------------------------------------
# Revised-content safety gate (ported from wikiRevision.ts parseRevisedContent)
# ---------------------------------------------------------------------------

def parse_revised_content(original: str, response: str) -> tuple:
    """Port of wikiRevision.ts parseRevisedContent — returns (content, changed).

    Same guards: ```markdown-pair-only unwrap (never a lone trailing fence),
    NO_CHANGES regex, Error: prefix, <30% length, identical -> unchanged.
    """
    cleaned = response.strip()
    if not cleaned:
        return (original, False)
    # Unwrap a whole-page ```markdown fence (models sometimes wrap the entire
    # reply). ONLY the explicit "markdown" language tag is treated as a wrapper:
    # a bare leading ``` could be the page's own first code block, and stripping
    # a lone trailing fence corrupts pages that end with a mermaid/code block —
    # the resulting unbalanced page would then be saved as a "correction".
    if re.match(r"^```markdown\s*\n", cleaned, re.IGNORECASE):
        cleaned = re.sub(r"^```markdown\s*\n", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned)
        cleaned = cleaned.strip()
    if (
        not cleaned
        or cleaned.startswith("Error:")
        or re.match(r"^NO_CHANGES\b.{0,10}$", cleaned)
    ):
        return (original, False)
    if len(cleaned) < len(original) * 0.3:
        return (original, False)
    if cleaned == original.strip():
        return (original, False)
    return (cleaned, True)
