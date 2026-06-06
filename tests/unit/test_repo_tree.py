"""Tests for api/repo_tree.py — URL construction per provider (no network)."""
import asyncio
from types import SimpleNamespace

import pytest

import api.repo_tree as repo_tree
from api.repo_tree import (RepoTreeError, extract_url_domain, extract_url_path,
                           fetch_repo_tree, read_local_repo_structure)


def make_repo(**kw):
    base = dict(owner="o", repo="r", type="github", token=None,
                localPath=None, repoUrl=None)
    base.update(kw)
    return SimpleNamespace(**base)


class FakeResponse:
    def __init__(self, json_data=None, status_code=200, text="", headers=None):
        self._json = json_data
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._json


class FakeHttp:
    """Routes requests.get by URL substring; records every URL."""
    def __init__(self, routes):
        self.routes = routes  # list of (substring, FakeResponse)
        self.urls = []

    def get(self, url, headers=None, timeout=None):
        self.urls.append((url, headers))
        for substring, resp in self.routes:
            if substring in url:
                return resp
        return FakeResponse(status_code=404, text="not found")


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# --- url helpers (ports of urlDecoder.tsx) ----------------------------------

def test_extract_url_domain():
    assert extract_url_domain("https://gitlab.example.com/g/p") == "https://gitlab.example.com"
    assert extract_url_domain("gitlab.com/g/p") == "https://gitlab.com"
    assert extract_url_domain("http://host:8080/x") == "http://host:8080"
    assert extract_url_domain("") is None


def test_extract_url_path():
    assert extract_url_path("https://gitlab.com/group/sub/project") == "group/sub/project"
    assert extract_url_path("https://gitlab.com/g/p/") == "g/p"
    assert extract_url_path("") is None


# --- github ------------------------------------------------------------------

def test_github_tree_url_and_default_branch(monkeypatch):
    http = FakeHttp([
        ("/repos/o/r/git/trees/dev?recursive=1",
         FakeResponse({"tree": [{"type": "blob", "path": "a.py"},
                                {"type": "tree", "path": "dir"},
                                {"type": "blob", "path": "dir/b.py"}]})),
        ("/repos/o/r/readme", FakeResponse({"content": "aGVsbG8="})),  # "hello"
        ("/repos/o/r", FakeResponse({"default_branch": "dev"})),
    ])
    monkeypatch.setattr(repo_tree, "requests", http)
    repo = make_repo(token="tok", repoUrl="https://github.com/o/r")

    file_tree, readme = run(fetch_repo_tree(repo))

    assert file_tree == "a.py\ndir/b.py"
    assert readme == "hello"
    # default branch from repo info is tried first
    tree_urls = [u for u, _ in http.urls if "git/trees" in u]
    assert tree_urls == ["https://api.github.com/repos/o/r/git/trees/dev?recursive=1"]
    # token flows into the Authorization header (Bearer, like createGithubHeaders)
    assert all(h.get("Authorization") == "Bearer tok" for _, h in http.urls)


def test_github_enterprise_api_base(monkeypatch):
    http = FakeHttp([
        ("git/trees/main?recursive=1",
         FakeResponse({"tree": [{"type": "blob", "path": "x"}]})),
        ("/readme", FakeResponse(status_code=404)),
        ("/repos/o/r", FakeResponse({"default_branch": "main"})),
    ])
    monkeypatch.setattr(repo_tree, "requests", http)
    repo = make_repo(repoUrl="https://github.corp.com/o/r")
    run(fetch_repo_tree(repo))
    assert http.urls[0][0].startswith("https://github.corp.com/api/v3/repos/o/r")


def test_github_branch_fallback_and_error(monkeypatch):
    http = FakeHttp([])  # everything 404s
    monkeypatch.setattr(repo_tree, "requests", http)
    with pytest.raises(RepoTreeError, match="API Error"):
        run(fetch_repo_tree(make_repo()))
    tree_urls = [u for u, _ in http.urls if "git/trees" in u]
    assert tree_urls == [
        "https://api.github.com/repos/o/r/git/trees/main?recursive=1",
        "https://api.github.com/repos/o/r/git/trees/master?recursive=1",
    ]


# --- gitlab -------------------------------------------------------------------

def test_gitlab_urls_paginated(monkeypatch):
    project = "group%2Fsub%2Fproj"
    http = FakeHttp([
        (f"/api/v4/projects/{project}/repository/tree?recursive=true&per_page=100&page=1",
         FakeResponse([{"type": "blob", "path": "a.py"}], headers={"x-next-page": "2"})),
        (f"/api/v4/projects/{project}/repository/tree?recursive=true&per_page=100&page=2",
         FakeResponse([{"type": "blob", "path": "b.py"}, {"type": "tree", "path": "d"}])),
        (f"/api/v4/projects/{project}/repository/files/README.md/raw",
         FakeResponse(text="gitlab readme")),
        (f"/api/v4/projects/{project}", FakeResponse({"default_branch": "main"})),
    ])
    monkeypatch.setattr(repo_tree, "requests", http)
    repo = make_repo(type="gitlab", token="glt",
                     repoUrl="https://gitlab.example.com/group/sub/proj.git")

    file_tree, readme = run(fetch_repo_tree(repo))

    assert file_tree == "a.py\nb.py"
    assert readme == "gitlab readme"
    assert http.urls[0][0] == f"https://gitlab.example.com/api/v4/projects/{project}"
    # PRIVATE-TOKEN header, like createGitlabHeaders
    assert all(h.get("PRIVATE-TOKEN") == "glt" for _, h in http.urls)


def test_gitlab_project_info_error(monkeypatch):
    http = FakeHttp([("/api/v4/projects/", FakeResponse(status_code=403, text="forbidden"))])
    monkeypatch.setattr(repo_tree, "requests", http)
    with pytest.raises(RepoTreeError, match="GitLab project info error: Status 403"):
        run(fetch_repo_tree(make_repo(type="gitlab", repoUrl="https://gitlab.com/o/r")))


# --- bitbucket ------------------------------------------------------------------

def test_bitbucket_urls_follow_next_cursor(monkeypatch):
    http = FakeHttp([
        ("/src/trunk/?recursive=true&per_page=100",
         FakeResponse({"values": [{"type": "commit_file", "path": "a.py"}],
                       "next": "https://api.bitbucket.org/2.0/repositories/o%2Fr/src/trunk/?page=2"})),
        ("/src/trunk/?page=2",
         FakeResponse({"values": [{"type": "commit_file", "path": "b.py"},
                                  {"type": "commit_directory", "path": "d"}]})),
        ("/src/trunk/README.md", FakeResponse(text="bb readme")),
        ("/2.0/repositories/o%2Fr", FakeResponse({"mainbranch": {"name": "trunk"}})),
    ])
    monkeypatch.setattr(repo_tree, "requests", http)
    repo = make_repo(type="bitbucket", token="bbt",
                     repoUrl="https://bitbucket.org/o/r")

    file_tree, readme = run(fetch_repo_tree(repo))

    assert file_tree == "a.py\nb.py"
    assert readme == "bb readme"
    assert http.urls[0][0] == "https://api.bitbucket.org/2.0/repositories/o%2Fr"
    assert all(h.get("Authorization") == "Bearer bbt" for _, h in http.urls)


def test_bitbucket_error_when_empty(monkeypatch):
    http = FakeHttp([("/2.0/repositories/", FakeResponse(status_code=404, text="nope"))])
    monkeypatch.setattr(repo_tree, "requests", http)
    with pytest.raises(RepoTreeError, match="Bitbucket API Error"):
        run(fetch_repo_tree(make_repo(type="bitbucket")))


# --- local ---------------------------------------------------------------------

def test_local_walk_exclusions(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print()")
    (tmp_path / "src" / "__init__.py").write_text("")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("x")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pkg.js").write_text("x")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "main.cpython-311.pyc").write_text("x")
    (tmp_path / ".hidden").write_text("x")
    (tmp_path / "README.md").write_text("# Title")

    file_tree, readme = read_local_repo_structure(str(tmp_path))

    assert file_tree == "README.md\nsrc/main.py"
    assert readme == "# Title"


def test_local_via_fetch_repo_tree(tmp_path):
    (tmp_path / "a.py").write_text("x")
    repo = make_repo(type="local", localPath=str(tmp_path))
    file_tree, readme = run(fetch_repo_tree(repo))
    assert file_tree == "a.py"
    assert readme == ""


def test_local_missing_dir():
    repo = make_repo(type="local", localPath="/definitely/not/here")
    with pytest.raises(RepoTreeError, match="not found"):
        run(fetch_repo_tree(repo))


def test_unsupported_type():
    with pytest.raises(RepoTreeError, match="Unsupported repository type"):
        run(fetch_repo_tree(make_repo(type="svn")))
