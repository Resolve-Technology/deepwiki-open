"""Tests for api/wiki_generator.py — fake dispatch, no network.

The prompt-envelope assertions lock the load-bearing behavior: generation is
retrieval-FREE (no-RAG note in the envelope) and self-review is
retrieval-grounded, exactly like today's websocket flow.
"""
import asyncio
import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Optional

import pytest

import api.api as api_module
import api.wiki_generator as wiki_generator
from api.api import RepoInfo
from api.llm_dispatch import LLMResult
from api.wiki_generator import (GenerationError, JobCancelled, JobProgress,
                                parse_structure_xml, run_generation)


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


STRUCTURE_XML = """<wiki_structure>
  <title>Test Wiki</title>
  <description>A wiki for tests</description>
  <sections>
    <section id="s1">
      <title>Root Section</title>
      <page_ref>p1</page_ref>
      <section_ref>s2</section_ref>
    </section>
    <section id="s2">
      <title>Child Section</title>
      <page_ref>p2</page_ref>
      <page_ref>p3</page_ref>
    </section>
  </sections>
  <pages>
    <page id="p1">
      <title>Page One</title>
      <importance>high</importance>
      <relevant_files><file_path>a.py</file_path><file_path>b.py</file_path></relevant_files>
      <related_pages><related>p2</related></related_pages>
    </page>
    <page id="p2">
      <title>Page Two</title>
      <importance>silly</importance>
      <relevant_files><file_path>c.py</file_path></relevant_files>
    </page>
    <page id="p3">
      <title>Page Three</title>
      <relevant_files><file_path>d.py</file_path></relevant_files>
    </page>
  </pages>
</wiki_structure>"""

PAGE_BODY = "# A Page\n\nGenerated body content that is long enough for the review guards."


@dataclass
class FakeJob:
    repo: RepoInfo
    language: str = "en"
    provider: str = "vllm"
    model: str = "test-model"
    comprehensive: bool = True
    self_review: bool = True
    force_regenerate: bool = False
    excluded_dirs: Optional[str] = None
    excluded_files: Optional[str] = None
    included_dirs: Optional[str] = None
    included_files: Optional[str] = None
    cancel_requested: bool = False
    progress: JobProgress = field(default_factory=JobProgress)
    stats: dict = field(default_factory=dict)


def make_job(**kw):
    repo = kw.pop("repo", None) or RepoInfo(
        owner="o", repo="r", type="github", token="secret-token",
        repoUrl="https://github.com/o/r")
    return FakeJob(repo=repo, **kw)


class FakeDispatch:
    """Pops scripted responses; str, Exception, or callable(prompt) -> str."""

    def __init__(self, script):
        self.script = list(script)
        self.prompts = []

    async def __call__(self, provider, model, prompt):
        self.prompts.append(prompt)
        item = self.script.pop(0) if self.script else PAGE_BODY
        if isinstance(item, Exception):
            raise item
        if callable(item):
            item = item(prompt)
        return LLMResult(text=item, input_tokens=10, output_tokens=20)


class FakeRAG:
    instances = []

    def __init__(self, provider=None, model=None):
        self.init = (provider, model)
        self.prepare_args = None
        self.queries = []
        FakeRAG.instances.append(self)

    def prepare_retriever(self, *args):
        self.prepare_args = args

    def __call__(self, query, language=None):
        self.queries.append((query, language))
        doc = SimpleNamespace(meta_data={"file_path": "a.py"},
                              text="retrieved grounding snippet")
        return [SimpleNamespace(documents=[doc])]


@pytest.fixture(autouse=True)
def engine_env(tmp_path, monkeypatch):
    """Temp cache dir + stubbed externals (RAG, repo tree, clone branch)."""
    FakeRAG.instances = []
    monkeypatch.setattr(api_module, "WIKI_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(wiki_generator, "RAG", FakeRAG)
    monkeypatch.setattr(wiki_generator, "get_clone_default_branch",
                        lambda *a, **k: "main")

    async def fake_tree(repo):
        return "src/a.py\nsrc/b.py", "# Readme"
    monkeypatch.setattr(wiki_generator, "fetch_repo_tree", fake_tree)
    monkeypatch.setattr(wiki_generator, "get_file_content",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("unused")))
    return tmp_path


def cache_file(tmp_path, job):
    return api_module.get_wiki_cache_path(
        job.repo.owner, job.repo.repo, job.repo.type, job.language,
        job.provider, job.model)


def read_cache(tmp_path, job):
    with open(cache_file(tmp_path, job), encoding="utf-8") as f:
        return json.load(f)


# --- the happy path -----------------------------------------------------------


def test_three_pages_generated_and_cached(tmp_path):
    job = make_job(self_review=False)
    dispatch = FakeDispatch([STRUCTURE_XML] + [PAGE_BODY] * 3)

    run(run_generation(job, dispatch))

    data = read_cache(tmp_path, job)
    assert list(data["generated_pages"]) == ["p1", "p2", "p3"]
    assert data["generated_pages"]["p1"]["content"] == PAGE_BODY
    assert data["wiki_structure"]["title"] == "Test Wiki"
    assert [s["id"] for s in data["wiki_structure"]["sections"]] == ["s1", "s2"]
    assert data["wiki_structure"]["rootSections"] == ["s1"]
    assert data["self_reviewed"] is False
    # 4 dispatches x (10 in / 20 out); review untouched
    assert data["stats"]["generation"] == {"input_tokens": 40, "output_tokens": 80,
                                           "seconds": data["stats"]["generation"]["seconds"]}
    assert data["stats"]["review"]["input_tokens"] == 0
    assert job.progress.phase == "done"
    assert job.progress.pages_done == 3
    # token never persisted
    assert "secret-token" not in json.dumps(data)


def test_incremental_save_after_each_page(tmp_path):
    job = make_job(self_review=False)
    pages_seen = []

    def snoop(prompt):
        # Runs while serving page 2/3 generations — cache must already exist
        pages_seen.append(len(read_cache(tmp_path, job)["generated_pages"]))
        return PAGE_BODY

    dispatch = FakeDispatch([STRUCTURE_XML, PAGE_BODY, snoop, snoop])
    run(run_generation(job, dispatch))
    assert pages_seen == [1, 2]


def test_self_review_flow_and_stats(tmp_path):
    job = make_job(self_review=True)
    rewrite = PAGE_BODY + "\n\nA corrected paragraph appended by the reviewer."
    dispatch = FakeDispatch([
        STRUCTURE_XML,
        PAGE_BODY, "NO_CHANGES",          # p1: unchanged
        PAGE_BODY, rewrite,               # p2: rewritten
        PAGE_BODY, "tiny",                # p3: <30% guard keeps original
    ])

    run(run_generation(job, dispatch))

    data = read_cache(tmp_path, job)
    assert data["generated_pages"]["p1"]["content"] == PAGE_BODY
    assert data["generated_pages"]["p2"]["content"] == rewrite
    assert data["generated_pages"]["p3"]["content"] == PAGE_BODY
    assert data["self_reviewed"] is True
    assert data["stats"]["generation"]["input_tokens"] == 40
    assert data["stats"]["review"]["input_tokens"] == 30


def test_self_review_dispatch_failure_keeps_page(tmp_path):
    job = make_job(self_review=True)
    dispatch = FakeDispatch([
        STRUCTURE_XML,
        PAGE_BODY, RuntimeError("review exploded"),
        PAGE_BODY, "NO_CHANGES",
        PAGE_BODY, "NO_CHANGES",
    ])
    run(run_generation(job, dispatch))
    data = read_cache(tmp_path, job)
    assert data["generated_pages"]["p1"]["content"] == PAGE_BODY
    assert job.progress.phase == "done"


# --- prompt-envelope parity (review findings C1/C2) ----------------------------


def test_generation_is_retrieval_free_and_review_is_grounded():
    job = make_job(self_review=True)
    dispatch = FakeDispatch([STRUCTURE_XML] + [PAGE_BODY, "NO_CHANGES"] * 3)

    run(run_generation(job, dispatch))

    structure_prompt = dispatch.prompts[0]
    page_prompts = dispatch.prompts[1::2][:3]
    review_prompts = dispatch.prompts[2::2][:3]

    for p in [structure_prompt] + page_prompts + review_prompts:
        # The double wrapper: websocket envelope around the frontend prompt
        assert p.startswith("/no_think <role>\nYou are an expert code analyst examining the github repository:")
        assert p.endswith("</query>\n\nAssistant: ")
        assert "<query>\n" in p

    # Generation (structure + pages) runs with NO retrieval
    for p in [structure_prompt] + page_prompts:
        assert "<note>Answering without retrieval augmentation.</note>" in p
        assert "<START_OF_CONTEXT>" not in p

    # The frontend prompt is embedded inside <query>...</query>
    assert "Return ONLY the valid XML structure" in structure_prompt
    assert "a H1 Markdown heading: `# Page One`" in page_prompts[0]
    assert "expert technical writer and software architect" in page_prompts[0]

    # Self-review IS retrieval-grounded
    for p in review_prompts:
        assert "<START_OF_CONTEXT>" in p
        assert "retrieved grounding snippet" in p
        assert "NO_CHANGES" in p

    # RAG was prepared with the repo URL and token, queried per page
    rag = FakeRAG.instances[0]
    assert rag.prepare_args[0] == "https://github.com/o/r"
    assert rag.prepare_args[2] == "secret-token"
    assert len(rag.queries) == 3


def test_deep_dive_file_injection(monkeypatch):
    xml = """<wiki_structure><title>T</title><description>D</description><pages>
      <page id="page-analysis-prog"><title>Prog Analysis</title>
        <relevant_files><file_path>prog.cbl</file_path></relevant_files></page>
    </pages></wiki_structure>"""
    monkeypatch.setattr(wiki_generator, "get_file_content",
                        lambda *a, **k: "COBOL SOURCE LINES")
    job = make_job(self_review=True)
    dispatch = FakeDispatch([xml, PAGE_BODY, "NO_CHANGES"])

    run(run_generation(job, dispatch))

    gen_prompt, review_prompt = dispatch.prompts[1], dispatch.prompts[2]
    for p in (gen_prompt, review_prompt):  # file content carries into review too
        assert '<currentFileContent path="prog.cbl">\nCOBOL SOURCE LINES\n</currentFileContent>' in p
    assert "senior mainframe/COBOL systems analyst" in gen_prompt


def test_deep_dive_file_fetch_failure_proceeds(monkeypatch):
    xml = """<wiki_structure><title>T</title><description>D</description><pages>
      <page id="page-analysis-prog"><title>Prog Analysis</title>
        <relevant_files><file_path>prog.cbl</file_path></relevant_files></page>
    </pages></wiki_structure>"""

    def boom(*a, **k):
        raise ValueError("local repos unsupported")
    monkeypatch.setattr(wiki_generator, "get_file_content", boom)
    job = make_job(self_review=False)
    dispatch = FakeDispatch([xml, PAGE_BODY])

    run(run_generation(job, dispatch))

    assert "<currentFileContent" not in dispatch.prompts[1]
    assert job.progress.phase == "done"


# --- structure handling ---------------------------------------------------------


def test_structure_retry_then_success(tmp_path):
    job = make_job(self_review=False)
    dispatch = FakeDispatch(["no xml here", "```xml\n" + STRUCTURE_XML + "\n```"]
                            + [PAGE_BODY] * 3)
    run(run_generation(job, dispatch))
    assert len(read_cache(tmp_path, job)["generated_pages"]) == 3


def test_structure_invalid_three_times_fails():
    job = make_job(self_review=False)
    dispatch = FakeDispatch(["garbage"] * 3)
    with pytest.raises(GenerationError, match="No valid XML found in response"):
        run(run_generation(job, dispatch))


def test_structure_unparseable_xml_fails():
    job = make_job(self_review=False)
    truncated = "<wiki_structure><title>T</title><pages><page></wiki_structure>"
    dispatch = FakeDispatch([truncated] * 3)
    with pytest.raises(GenerationError, match="Failed to parse"):
        run(run_generation(job, dispatch))


def test_duplicate_page_ids_get_dup_suffix():
    xml = """<wiki_structure><title>T</title><description>D</description><pages>
      <page id="p1"><title>A</title></page>
      <page id="p1"><title>B</title></page>
      <page><title>C</title></page>
    </pages></wiki_structure>"""
    structure = parse_structure_xml(xml, comprehensive=True)
    assert [p["id"] for p in structure["pages"]] == ["p1", "p1-dup", "page-3"]


def test_parse_structure_importance_defaults():
    structure = parse_structure_xml(STRUCTURE_XML, comprehensive=True)
    importances = {p["id"]: p["importance"] for p in structure["pages"]}
    assert importances == {"p1": "high", "p2": "low", "p3": "medium"}


def test_parse_structure_concise_skips_sections():
    structure = parse_structure_xml(STRUCTURE_XML, comprehensive=False)
    assert structure["sections"] == []
    assert structure["rootSections"] == []


# --- failure policy ----------------------------------------------------------------


def test_page_failure_stores_error_and_continues(tmp_path):
    job = make_job(self_review=False)
    dispatch = FakeDispatch([STRUCTURE_XML,
                             RuntimeError("boom1"), RuntimeError("boom2"), PAGE_BODY])
    run(run_generation(job, dispatch))
    data = read_cache(tmp_path, job)
    assert data["generated_pages"]["p1"]["content"] == "Error generating content: boom1"
    assert data["generated_pages"]["p2"]["content"] == "Error generating content: boom2"
    assert data["generated_pages"]["p3"]["content"] == PAGE_BODY


def test_three_consecutive_failures_abort(tmp_path):
    job = make_job(self_review=False)
    dispatch = FakeDispatch([STRUCTURE_XML, RuntimeError("b1"), RuntimeError("b2"),
                             RuntimeError("b3")])
    with pytest.raises(GenerationError, match="3 consecutive page failures"):
        run(run_generation(job, dispatch))
    # the first two error pages were still saved incrementally
    assert len(read_cache(tmp_path, job)["generated_pages"]) == 2


def test_error_pages_skip_self_review(tmp_path):
    job = make_job(self_review=True)
    dispatch = FakeDispatch([STRUCTURE_XML,
                             RuntimeError("boom"),          # p1 fails -> no review call
                             PAGE_BODY, "NO_CHANGES",       # p2
                             PAGE_BODY, "NO_CHANGES"])      # p3
    run(run_generation(job, dispatch))
    assert len(dispatch.prompts) == 6  # 1 structure + 3 gens + only 2 reviews


# --- cancellation -----------------------------------------------------------------


def test_cancel_mid_run_keeps_partial_cache(tmp_path):
    job = make_job(self_review=False)

    def cancel_after(prompt):
        job.cancel_requested = True
        return PAGE_BODY

    dispatch = FakeDispatch([STRUCTURE_XML, cancel_after])
    with pytest.raises(JobCancelled):
        run(run_generation(job, dispatch))
    assert len(read_cache(tmp_path, job)["generated_pages"]) == 1


# --- force_regenerate ----------------------------------------------------------------


def test_force_regenerate_deletes_target_cache(tmp_path):
    job = make_job(self_review=False, force_regenerate=True)
    path = cache_file(tmp_path, job)
    with open(path, "w") as f:
        f.write("{}")
    dispatch = FakeDispatch(["garbage"] * 3)
    with pytest.raises(GenerationError):
        run(run_generation(job, dispatch))
    import os
    assert not os.path.exists(path)
