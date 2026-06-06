"""Server-side wiki generation engine.

Runs one generation job end-to-end: prepare retriever, fetch the repo tree,
determine the wiki structure, generate every page (with optional self-review),
and save the wiki cache incrementally after every page so partial runs survive.

Prompt fidelity is the acceptance criterion: every dispatch sends the SAME
double-wrapped envelope today's websocket flow sends — the code-analyst system
prompt around the frontend-ported page/structure prompt. Retrieval follows the
websocket's 8000-token gate: messages at or under it retrieve (standard page
prompts, small-repo structure prompts; deep-dives use a filePath-focused
query), oversized messages go without (the envelope carries the no-RAG note);
self-review always retrieves via its explicit rag_query. Do not "improve"
this without changing the tests.
"""
import asyncio
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional
from urllib.parse import unquote

from api.api import (RepoInfo, WikiCacheRequest, WikiPage, WikiStructureModel,
                     get_wiki_cache_path, save_wiki_cache)
from api.data_pipeline import count_tokens, get_file_content
from api.prompt_assembly import (assemble_envelope, format_context_text,
                                 select_generation_system_prompt)
from api.rag import RAG
from api.repo_tree import fetch_repo_tree
from api.wiki_prompts import (build_page_prompt, build_page_rag_query,
                              build_self_review_prompt, build_structure_prompt,
                              get_clone_default_branch, parse_revised_content)

logger = logging.getLogger(__name__)

MAX_STRUCTURE_ATTEMPTS = 3       # same loop as determineWikiStructure
MAX_CONSECUTIVE_PAGE_FAILURES = 3


class JobCancelled(Exception):
    """Raised between dispatches when job.cancel_requested is set."""


class GenerationError(Exception):
    """A job-fatal generation failure (bad structure, repeated page errors)."""


@dataclass
class PhaseStats:
    input_tokens: int = 0
    output_tokens: int = 0
    ms: float = 0.0

    def as_dict(self) -> Dict[str, int]:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "seconds": round(self.ms / 1000)}


@dataclass
class JobProgress:
    phase: str = "queued"        # queued|embedding|structure|pages|saving|done
    pages_total: int = 0
    pages_done: int = 0
    current_page_title: str = ""


def get_repo_url(repo: RepoInfo) -> str:
    """Port of src/utils/getRepoUrl.tsx."""
    if repo.type == "local" and repo.localPath:
        return repo.localPath
    if repo.repoUrl:
        return repo.repoUrl
    if repo.owner and repo.repo:
        return "http://example/" + repo.owner + "/" + repo.repo
    return ""


def split_filter(value: Optional[str]) -> Optional[List[str]]:
    """The websocket's newline-split + unquote of the filter fields."""
    if not value:
        return None
    return [unquote(part) for part in value.split("\n") if part.strip()]


def _first_text(el, tag: str) -> str:
    """querySelector(tag).textContent equivalent: first descendant's text."""
    found = el.find(f".//{tag}")
    return (found.text or "") if found is not None else ""


def parse_structure_xml(xml_text: str, comprehensive: bool) -> Dict[str, Any]:
    """Port of page.tsx's DOMParser block in determineWikiStructure.

    Returns the dict shape the frontend caches today: pages carry
    {id,title,content:"",filePaths,importance,relatedPages} (importance
    defaults to 'medium'; parent_section in the XML is ignored, matching
    today); duplicate page ids get the '-dup' suffix; sections/rootSections
    are extracted only in comprehensive view.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.error(f"Wiki structure XML failed to parse: {e}")
        raise GenerationError(
            "Failed to parse the generated wiki structure. Please try regenerating the wiki.")

    title = root.findtext("title", default="") or ""
    description = root.findtext("description", default="") or ""

    pages: List[Dict[str, Any]] = []
    seen_page_ids = set()
    for index, page_el in enumerate(root.iter("page")):
        page_id = page_el.get("id") or f"page-{index + 1}"
        # Guarantee uniqueness so a fallback id can't collide with an explicit one
        while page_id in seen_page_ids:
            page_id = f"{page_id}-dup"
        seen_page_ids.add(page_id)

        importance_el = page_el.find(".//importance")
        if importance_el is not None:
            importance = ("high" if importance_el.text == "high"
                          else "medium" if importance_el.text == "medium" else "low")
        else:
            importance = "medium"

        pages.append({
            "id": page_id,
            "title": _first_text(page_el, "title"),
            "content": "",  # Will be generated later
            "filePaths": [el.text for el in page_el.iter("file_path") if el.text],
            "importance": importance,
            "relatedPages": [el.text for el in page_el.iter("related") if el.text],
        })

    if not pages:
        # The browser flow would silently produce an empty wiki here; a failed
        # job with a clear error is the server-side mapping of that dead end.
        raise GenerationError(
            "Failed to parse the generated wiki structure. Please try regenerating the wiki.")

    sections: List[Dict[str, Any]] = []
    root_sections: List[str] = []
    if comprehensive:
        section_els = list(root.iter("section"))
        for section_el in section_els:
            section_id = section_el.get("id") or f"section-{len(sections) + 1}"
            subsections = [el.text for el in section_el.iter("section_ref") if el.text]
            sections.append({
                "id": section_id,
                "title": _first_text(section_el, "title"),
                "pages": [el.text for el in section_el.iter("page_ref") if el.text],
                "subsections": subsections if subsections else None,
            })
            # Root section = not referenced by any other section's section_ref
            is_referenced = any(
                ref.text == section_id
                for other in section_els for ref in other.iter("section_ref"))
            if not is_referenced:
                root_sections.append(section_id)

    return {"id": "wiki", "title": title, "description": description,
            "pages": pages, "sections": sections, "rootSections": root_sections}


async def run_generation(
    job,
    dispatch: Callable[[str, str, str], Awaitable[Any]],
    on_progress: Optional[Callable[[Any], None]] = None,
) -> None:
    """Run one wiki generation job to completion (raises on failure/cancel).

    ``job`` is an api.wiki_jobs.WikiJob (duck-typed for tests). ``dispatch``
    is (provider, model, prompt) -> LLMResult — production passes
    api.llm_dispatch.generate, tests pass fakes.
    """
    repo: RepoInfo = job.repo
    progress: JobProgress = job.progress
    stats_generation = PhaseStats()
    stats_review = PhaseStats()

    def notify() -> None:
        job.stats = {"generation": stats_generation.as_dict(),
                     "review": stats_review.as_dict()}
        if on_progress:
            on_progress(job)

    async def checkpoint() -> None:
        await asyncio.sleep(0)
        if job.cancel_requested:
            raise JobCancelled()

    async def timed_dispatch(prompt: str, phase_stats: PhaseStats) -> str:
        await checkpoint()
        start = time.monotonic()
        try:
            result = await dispatch(job.provider, job.model, prompt)
        finally:
            phase_stats.ms += (time.monotonic() - start) * 1000
        phase_stats.input_tokens += result.input_tokens
        phase_stats.output_tokens += result.output_tokens
        return result.text

    async def retrieve_for_generation(inner_prompt: str,
                                      file_path: str = "") -> str:
        """The websocket's retrieval gate, replicated for generation calls.

        The websocket retrieves whenever the message is <= 8000 tokens (so
        standard page prompts DID get RAG context in the browser flow — only
        oversized messages like big structure prompts went without). The
        retrieval query mirrors its fallback chain: a filePath-focused query
        when filePath is set (deep-dive pages), else the message itself.
        """
        tokens = count_tokens(inner_prompt,
                              is_ollama_embedder=(job.provider == "ollama"))
        logger.info(f"Request size: {tokens} tokens")
        if tokens > 8000:
            logger.warning(f"Request exceeds recommended token limit ({tokens} > 7500)")
            return ""
        rag_query = f"Contexts related to {file_path}" if file_path else inner_prompt
        try:
            retrieved = await asyncio.to_thread(rag, rag_query, language=job.language)
            return format_context_text(retrieved)
        except Exception as e:
            # Continue without RAG if there's an error (websocket behavior)
            logger.error(f"Error in RAG retrieval: {str(e)}")
            return ""

    async def save_partial(generated: Dict[str, Dict[str, Any]],
                           structure: Dict[str, Any]) -> None:
        request = WikiCacheRequest(
            # A copy, NOT job.repo: save_wiki_cache nulls the token on its
            # argument, and the job still needs it for deep-dive file fetches.
            repo=RepoInfo(owner=repo.owner, repo=repo.repo, type=repo.type,
                          localPath=repo.localPath, repoUrl=repo.repoUrl),
            language=job.language,
            wiki_structure=WikiStructureModel(
                id=structure["id"], title=structure["title"],
                description=structure["description"],
                pages=[WikiPage(**p) for p in structure["pages"]],
                sections=structure["sections"], rootSections=structure["rootSections"]),
            generated_pages={pid: WikiPage(**p) for pid, p in generated.items()},
            provider=job.provider, model=job.model,
            self_reviewed=job.self_review,
            stats={"generation": stats_generation.as_dict(),
                   "review": stats_review.as_dict()},
        )
        await save_wiki_cache(request)

    # 1. Force-regenerate: delete the target cache version only
    if job.force_regenerate:
        cache_path = get_wiki_cache_path(repo.owner, repo.repo, repo.type,
                                         job.language, job.provider, job.model)
        try:
            os.remove(cache_path)
            logger.info(f"force_regenerate: removed {cache_path}")
        except FileNotFoundError:
            pass

    repo_url = get_repo_url(repo)
    repo_name = repo_url.split("/")[-1] if "/" in repo_url else repo_url

    # 2. Prepare the retriever (embeddings); only self-review uses it, but
    # this matches the websocket flow, which prepares it for every request.
    progress.phase = "embedding"
    notify()
    rag = RAG(provider=job.provider, model=job.model)
    await asyncio.to_thread(
        rag.prepare_retriever, repo_url, repo.type, repo.token,
        split_filter(job.excluded_dirs), split_filter(job.excluded_files),
        split_filter(job.included_dirs), split_filter(job.included_files))
    logger.info(f"Retriever prepared for {repo_url}")

    # 3. File tree + README via provider APIs (identical input to today's flow)
    progress.phase = "structure"
    notify()
    file_tree, readme = await fetch_repo_tree(repo)
    default_branch = get_clone_default_branch(
        repo.owner, repo.repo, repo.type,
        repo.localPath if repo.type == "local" else None)

    system_prompt = select_generation_system_prompt(
        repo.type, repo_url, repo_name, job.language)

    # 4. Structure call — retry loop ported from determineWikiStructure.
    # Unlike the browser's lenient DOMParser, ElementTree is strict, so a
    # response that parses badly also consumes an attempt (fresh dispatch).
    structure_inner = build_structure_prompt(
        file_tree, readme, repo.owner, repo.repo, job.language, job.comprehensive)
    # Big structure prompts (full file tree) exceed the 8000-token gate and go
    # without retrieval; small repos fall under it and retrieve, like today.
    structure_context = await retrieve_for_generation(structure_inner)
    structure_prompt = assemble_envelope(system_prompt, structure_inner,
                                         context_text=structure_context,
                                         provider=job.provider)
    structure: Optional[Dict[str, Any]] = None
    last_parse_error: Optional[GenerationError] = None
    saw_xml = False
    for attempt in range(1, MAX_STRUCTURE_ATTEMPTS + 1):
        response_text = await timed_dispatch(structure_prompt, stats_generation)
        notify()
        # Clean up markdown delimiters
        response_text = re.sub(r"^```(?:xml)?\s*", "", response_text, flags=re.IGNORECASE)
        response_text = re.sub(r"```\s*$", "", response_text, flags=re.IGNORECASE)
        match = re.search(r"<wiki_structure>[\s\S]*?</wiki_structure>", response_text)
        if not match:
            logger.warning(
                f"Wiki structure attempt {attempt}/{MAX_STRUCTURE_ATTEMPTS}: no valid XML "
                f"in response (received {len(response_text)} chars; has opening tag: "
                f"{'<wiki_structure>' in response_text})")
            continue
        saw_xml = True
        xml_text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", match.group(0))
        # Escape bare ampersands (LLMs emit "A & B" freely; browsers' DOMParser
        # shrugged it off, ElementTree refuses) — lossless for valid XML.
        xml_text = re.sub(r"&(?!#?[A-Za-z0-9]+;)", "&amp;", xml_text)
        try:
            structure = parse_structure_xml(xml_text, job.comprehensive)
            break
        except GenerationError as e:
            last_parse_error = e
            logger.warning(
                f"Wiki structure attempt {attempt}/{MAX_STRUCTURE_ATTEMPTS}: "
                f"XML matched but did not parse: {e}")
    if structure is None:
        if saw_xml and last_parse_error is not None:
            raise last_parse_error
        raise GenerationError("No valid XML found in response")
    pages = structure["pages"]

    # 5. Per-page generation (sequential, like the frontend's MAX_CONCURRENT=1)
    progress.phase = "pages"
    progress.pages_total = len(pages)
    notify()

    generated: Dict[str, Dict[str, Any]] = {}
    consecutive_failures = 0
    for page in pages:
        await checkpoint()
        progress.current_page_title = page["title"]
        notify()

        is_deep_dive = page["id"].startswith("page-analysis-")
        file_content, file_path = "", ""
        try:
            page_inner = build_page_prompt(
                page["title"], page["filePaths"], job.language, is_deep_dive,
                repo_url, repo.type, default_branch)
            # Deep-dive pages get the full program source injected — the same
            # provider-API fetch the websocket does for request.filePath (it
            # raises for local repos; proceed without injection, like today).
            requested_file_path = ""
            if is_deep_dive and page["filePaths"]:
                requested_file_path = page["filePaths"][0]
                file_path = requested_file_path
                try:
                    file_content = await asyncio.to_thread(
                        get_file_content, repo_url, file_path, repo.type, repo.token)
                except Exception as e:
                    logger.error(f"Error retrieving file content: {str(e)}")
                    file_content, file_path = "", ""
            # Page prompts are usually under the 8000-token gate, so the
            # browser flow retrieved for them (filePath-focused query on
            # deep-dives, the message itself otherwise) — reproduce that.
            page_context = await retrieve_for_generation(
                page_inner, file_path=requested_file_path)
            prompt = assemble_envelope(system_prompt, page_inner,
                                       file_content=file_content, file_path=file_path,
                                       context_text=page_context,
                                       provider=job.provider)
            content = await timed_dispatch(prompt, stats_generation)
            # Clean up markdown delimiters (same regexes as generatePageContent)
            content = re.sub(r"^```markdown\s*", "", content, flags=re.IGNORECASE)
            content = re.sub(r"```\s*$", "", content, flags=re.IGNORECASE)
            consecutive_failures = 0
        except JobCancelled:
            raise
        except Exception as e:
            logger.error(f"Error generating content for page {page['id']}: {e}")
            content = f"Error generating content: {e}"
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_PAGE_FAILURES:
                raise GenerationError(
                    f"{consecutive_failures} consecutive page failures; last error: {e}")

        # 6. Self-review — the retrieval-grounded pass; failure keeps the page
        if job.self_review and content.strip() and not content.startswith("Error"):
            await checkpoint()
            try:
                context_text = ""
                try:
                    retrieved = await asyncio.to_thread(
                        rag, build_page_rag_query(page["title"], page["filePaths"]),
                        language=job.language)
                    context_text = format_context_text(retrieved)
                except Exception as e:
                    logger.error(f"Error in RAG retrieval: {str(e)}")
                review_inner = build_self_review_prompt(
                    page["title"], page["filePaths"], content, repo_url)
                review_prompt = assemble_envelope(
                    system_prompt, review_inner, context_text=context_text,
                    file_content=file_content, file_path=file_path,
                    provider=job.provider)
                response = await timed_dispatch(review_prompt, stats_review)
                content, changed = parse_revised_content(content, response)
                if changed:
                    logger.info(f"Self-review corrected {page['title']}")
            except JobCancelled:
                raise
            except Exception as e:
                logger.warning(f"Self-review failed for {page['title']}, keeping original: {e}")

        # 7. Incremental save after every page
        generated[page["id"]] = {**page, "content": content}
        progress.pages_done += 1
        progress.current_page_title = ""
        notify()
        await save_partial(generated, structure)

    progress.phase = "saving"
    notify()
    await save_partial(generated, structure)
    progress.phase = "done"
    notify()
