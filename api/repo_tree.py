"""Repo file-tree + README fetch for server-side generation.

Port of the frontend's ``fetchRepositoryStructure`` (page.tsx): the tree comes
from the provider APIs (GitHub trees / GitLab repository tree / Bitbucket src),
NOT from a clone walk, so the structure prompt's input stays identical to what
the browser-driven flow sends today. The ``local`` branch reuses the same
directory walk as ``/local_repo/structure`` (factored here, imported by both).
"""
import asyncio
import base64
import logging
import os
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse

import requests

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30  # seconds per provider-API request


class RepoTreeError(Exception):
    """Raised when the repository structure cannot be fetched."""


# --- ports of src/utils/urlDecoder.tsx -------------------------------------

def extract_url_domain(input_url: str) -> Optional[str]:
    """Port of extractUrlDomain: protocol + hostname (+ port)."""
    try:
        normalized = input_url if input_url.startswith("http") else f"https://{input_url}"
        url = urlparse(normalized)
        if not url.hostname:
            return None
        port = f":{url.port}" if url.port else ""
        return f"{url.scheme}://{url.hostname}{port}"
    except (ValueError, AttributeError):
        return None


def extract_url_path(input_url: str) -> Optional[str]:
    """Port of extractUrlPath: pathname without leading/trailing slashes."""
    try:
        normalized = input_url if input_url.startswith("http") else f"https://{input_url}"
        url = urlparse(normalized)
        if not url.hostname:
            return None
        return url.path.strip("/")
    except (ValueError, AttributeError):
        return None


# --- ports of the page.tsx header builders ---------------------------------

def _github_headers(token: Optional[str]) -> Dict[str, str]:
    headers = {"Accept": "application/vnd.github.v3+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _gitlab_headers(token: Optional[str]) -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["PRIVATE-TOKEN"] = token
    return headers


def _bitbucket_headers(token: Optional[str]) -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


# --- local walk (shared with /local_repo/structure) -------------------------

MAX_README_BYTES = 1_000_000


def read_local_repo_structure(real_path: str) -> Tuple[str, str]:
    """Walk a local repository directory; returns (file_tree, readme).

    Mirrors the exclusions of ``/local_repo/structure``: hidden dirs/files,
    ``__pycache__``, ``node_modules``, ``.venv``, ``__init__.py``, ``.DS_Store``.
    """
    file_tree_lines: List[str] = []
    readme_content = ""

    # followlinks=False prevents symlinks from escaping the tree.
    for root, dirs, files in os.walk(real_path, followlinks=False):
        # Exclude hidden dirs/files and virtual envs
        dirs[:] = [d for d in dirs if not d.startswith('.') and d != '__pycache__' and d != 'node_modules' and d != '.venv']
        for file in files:
            if file.startswith('.') or file == '__init__.py' or file == '.DS_Store':
                continue
            rel_dir = os.path.relpath(root, real_path)
            rel_file = os.path.join(rel_dir, file) if rel_dir != '.' else file
            file_tree_lines.append(rel_file)
            # Find README.md (case-insensitive)
            if file.lower() == 'readme.md' and not readme_content:
                try:
                    with open(os.path.join(root, file), 'r', encoding='utf-8') as f:
                        readme_content = f.read(MAX_README_BYTES)
                except Exception as e:
                    logger.warning(f"Could not read README.md: {str(e)}")
                    readme_content = ""

    return '\n'.join(sorted(file_tree_lines)), readme_content


# --- per-provider tree fetches ----------------------------------------------

def _github_api_base(repo_url: Optional[str]) -> str:
    """Port of getGithubApiUrl: public API or GitHub Enterprise /api/v3."""
    if not repo_url:
        return "https://api.github.com"
    try:
        url = urlparse(repo_url)
        hostname = url.hostname
        if not hostname:
            return "https://api.github.com"
        if hostname == "github.com":
            return "https://api.github.com"
        # GitHub Enterprise API URL format: https://github.company.com/api/v3
        return f"{url.scheme}://{hostname}/api/v3"
    except ValueError:
        return "https://api.github.com"


def _fetch_github_tree(owner: str, repo: str, repo_url: Optional[str],
                       token: Optional[str]) -> Tuple[str, str]:
    headers = _github_headers(token)
    api_base = _github_api_base(repo_url)

    # First, try to get the default branch from the repository info
    default_branch = None
    try:
        resp = requests.get(f"{api_base}/repos/{owner}/{repo}",
                            headers=headers, timeout=REQUEST_TIMEOUT)
        if resp.ok:
            default_branch = resp.json().get("default_branch")
            logger.info(f"Found default branch: {default_branch}")
    except requests.RequestException as e:
        logger.warning(f"Could not fetch repository info for default branch: {e}")

    # Try the actual default branch first, then the common names
    branches_to_try = []
    for branch in ([default_branch, "main", "master"] if default_branch else ["main", "master"]):
        if branch and branch not in branches_to_try:
            branches_to_try.append(branch)

    tree_data = None
    api_error_details = ""
    for branch in branches_to_try:
        api_url = f"{api_base}/repos/{owner}/{repo}/git/trees/{branch}?recursive=1"
        logger.info(f"Fetching repository structure from branch: {branch}")
        try:
            resp = requests.get(api_url, headers=headers, timeout=REQUEST_TIMEOUT)
            if resp.ok:
                tree_data = resp.json()
                break
            api_error_details = f"Status: {resp.status_code}, Response: {resp.text}"
            logger.error(f"Error fetching repository structure: {api_error_details}")
        except requests.RequestException as e:
            logger.error(f"Network error fetching branch {branch}: {e}")

    if not tree_data or "tree" not in tree_data:
        if api_error_details:
            raise RepoTreeError(f"Could not fetch repository structure. API Error: {api_error_details}")
        raise RepoTreeError("Could not fetch repository structure. Repository might not exist, be empty or private.")

    file_tree = "\n".join(
        item["path"] for item in tree_data["tree"] if item.get("type") == "blob"
    )

    readme = ""
    try:
        resp = requests.get(f"{api_base}/repos/{owner}/{repo}/readme",
                            headers=headers, timeout=REQUEST_TIMEOUT)
        if resp.ok:
            readme = base64.b64decode(resp.json()["content"]).decode("utf-8", errors="replace")
        else:
            logger.warning(f"Could not fetch README.md, status: {resp.status_code}")
    except (requests.RequestException, KeyError, ValueError) as e:
        logger.warning(f"Could not fetch README.md, continuing with empty README: {e}")

    return file_tree, readme


def _fetch_gitlab_tree(owner: str, repo: str, repo_url: Optional[str],
                       token: Optional[str]) -> Tuple[str, str]:
    headers = _gitlab_headers(token)
    project_path = (extract_url_path(repo_url or "") or f"{owner}/{repo}")
    if project_path.endswith(".git"):
        project_path = project_path[: -len(".git")]
    project_domain = extract_url_domain(repo_url or "https://gitlab.com")
    if not project_domain:
        raise RepoTreeError(f"Invalid project domain URL: {project_domain}")
    encoded_project_path = quote(project_path, safe="")
    project_info_url = f"{project_domain}/api/v4/projects/{encoded_project_path}"

    resp = requests.get(project_info_url, headers=headers, timeout=REQUEST_TIMEOUT)
    if not resp.ok:
        raise RepoTreeError(f"GitLab project info error: Status {resp.status_code}, Response: {resp.text}")

    # Paginate to fetch the full file tree
    files_data: List[dict] = []
    page = 1
    more_pages = True
    while more_pages:
        api_url = f"{project_info_url}/repository/tree?recursive=true&per_page=100&page={page}"
        resp = requests.get(api_url, headers=headers, timeout=REQUEST_TIMEOUT)
        if not resp.ok:
            raise RepoTreeError(f"Error fetching GitLab repository structure (page {page}): {resp.text}")
        files_data.extend(resp.json())
        next_page = resp.headers.get("x-next-page")
        more_pages = bool(next_page)
        page = int(next_page) if next_page else page + 1

    if not files_data:
        raise RepoTreeError("Could not fetch repository structure. Repository might be empty or inaccessible.")

    file_tree = "\n".join(
        item["path"] for item in files_data if item.get("type") == "blob"
    )

    readme = ""
    readme_url = f"{project_info_url}/repository/files/README.md/raw"
    try:
        resp = requests.get(readme_url, headers=headers, timeout=REQUEST_TIMEOUT)
        if resp.ok:
            readme = resp.text
            logger.info("Successfully fetched GitLab README.md")
        else:
            logger.warning(f"Could not fetch GitLab README.md status: {resp.status_code}")
    except requests.RequestException as e:
        logger.warning(f"Error fetching GitLab README.md: {e}")

    return file_tree, readme


def _fetch_bitbucket_tree(owner: str, repo: str, repo_url: Optional[str],
                          token: Optional[str]) -> Tuple[str, str]:
    headers = _bitbucket_headers(token)
    repo_path = extract_url_path(repo_url or "") or f"{owner}/{repo}"
    encoded_repo_path = quote(repo_path, safe="")

    api_error_details = ""
    default_branch = ""
    values: List[dict] = []

    project_info_url = f"https://api.bitbucket.org/2.0/repositories/{encoded_repo_path}"
    try:
        resp = requests.get(project_info_url, headers=headers, timeout=REQUEST_TIMEOUT)
        if resp.ok:
            default_branch = resp.json()["mainbranch"]["name"]
            # Bitbucket paginates /src; follow the `next` cursor to get the full tree
            api_url: Optional[str] = (
                f"https://api.bitbucket.org/2.0/repositories/{encoded_repo_path}"
                f"/src/{default_branch}/?recursive=true&per_page=100"
            )
            try:
                while api_url:
                    resp = requests.get(api_url, headers=headers, timeout=REQUEST_TIMEOUT)
                    if not resp.ok:
                        api_error_details = f"Status: {resp.status_code}, Response: {resp.text}"
                        break
                    page_data = resp.json()
                    if isinstance(page_data.get("values"), list):
                        values.extend(page_data["values"])
                    api_url = page_data.get("next") or None
            except requests.RequestException as e:
                logger.error(f"Network error fetching Bitbucket branch {default_branch}: {e}")
        else:
            api_error_details = f"Status: {resp.status_code}, Response: {resp.text}"
    except requests.RequestException as e:
        logger.error(f"Network error fetching Bitbucket project info: {e}")

    if not values:
        if api_error_details:
            raise RepoTreeError(f"Could not fetch repository structure. Bitbucket API Error: {api_error_details}")
        raise RepoTreeError("Could not fetch repository structure. Repository might not exist, be empty or private.")

    file_tree = "\n".join(
        item["path"] for item in values if item.get("type") == "commit_file"
    )

    readme = ""
    try:
        resp = requests.get(
            f"https://api.bitbucket.org/2.0/repositories/{encoded_repo_path}/src/{default_branch}/README.md",
            headers=headers, timeout=REQUEST_TIMEOUT)
        if resp.ok:
            readme = resp.text
        else:
            logger.warning(f"Could not fetch Bitbucket README.md, status: {resp.status_code}")
    except requests.RequestException as e:
        logger.warning(f"Could not fetch Bitbucket README.md, continuing with empty README: {e}")

    return file_tree, readme


# --- entry point -------------------------------------------------------------

def _fetch_repo_tree_sync(repo) -> Tuple[str, str]:
    """``repo`` is an api.api.RepoInfo (duck-typed to avoid a circular import)."""
    repo_type = repo.type
    if repo_type == "local":
        if not repo.localPath:
            raise RepoTreeError("Local repository has no localPath")
        real_path = os.path.realpath(repo.localPath)
        if not os.path.isdir(real_path):
            raise RepoTreeError(f"Local repository directory not found: {repo.localPath}")
        return read_local_repo_structure(real_path)
    if repo_type == "github":
        return _fetch_github_tree(repo.owner, repo.repo, repo.repoUrl, repo.token)
    if repo_type == "gitlab":
        return _fetch_gitlab_tree(repo.owner, repo.repo, repo.repoUrl, repo.token)
    if repo_type == "bitbucket":
        return _fetch_bitbucket_tree(repo.owner, repo.repo, repo.repoUrl, repo.token)
    raise RepoTreeError(f"Unsupported repository type: {repo_type!r}")


async def fetch_repo_tree(repo) -> Tuple[str, str]:
    """Fetch (file_tree, readme) for a repo via its provider API (async wrapper)."""
    return await asyncio.to_thread(_fetch_repo_tree_sync, repo)
