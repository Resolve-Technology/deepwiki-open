import os
import re
import glob
import logging
import subprocess
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from typing import List, Optional, Dict, Any, Literal
import json
from datetime import datetime, timezone
from pydantic import BaseModel, Field
import google.generativeai as genai
import asyncio

# Configure logging
from api.logging_config import setup_logging

setup_logging()
logger = logging.getLogger(__name__)


# Initialize FastAPI app
app = FastAPI(
    title="Streaming API",
    description="API for streaming chat completions"
)

# Configure CORS — restrict to an allow-list (override via DEEPWIKI_ALLOWED_ORIGINS,
# a comma-separated list). Defaults to the local frontend origin.
_allowed_origins_env = os.environ.get("DEEPWIKI_ALLOWED_ORIGINS")
ALLOWED_ORIGINS = (
    [o.strip() for o in _allowed_origins_env.split(",") if o.strip()]
    if _allowed_origins_env
    else ["http://localhost:3000"]
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)

# Helper function to get adalflow root path
def get_adalflow_default_root_path():
    return os.path.expanduser(os.path.join("~", ".adalflow"))

# --- Pydantic Models ---
class WikiPage(BaseModel):
    """
    Model for a wiki page.
    """
    id: str
    title: str
    content: str
    filePaths: List[str]
    importance: str # Should ideally be Literal['high', 'medium', 'low']
    relatedPages: List[str]

class ProcessedProjectEntry(BaseModel):
    id: str  # Filename
    owner: str
    repo: str
    name: str  # owner/repo
    repo_type: str # Renamed from type to repo_type for clarity with existing models
    submittedAt: int # Timestamp
    language: str # Extracted from filename
    provider: Optional[str] = None  # Extracted from versioned filenames
    model: Optional[str] = None     # Extracted from versioned filenames
    stats: Optional[Dict[str, Any]] = None  # generation token/time stats from the cache JSON

class RepoInfo(BaseModel):
    owner: str
    repo: str
    type: str
    token: Optional[str] = None
    localPath: Optional[str] = None
    repoUrl: Optional[str] = None


class WikiSection(BaseModel):
    """
    Model for the wiki sections.
    """
    id: str
    title: str
    pages: List[str]
    subsections: Optional[List[str]] = None


class WikiStructureModel(BaseModel):
    """
    Model for the overall wiki structure.
    """
    id: str
    title: str
    description: str
    pages: List[WikiPage]
    sections: Optional[List[WikiSection]] = None
    rootSections: Optional[List[str]] = None

class WikiCacheData(BaseModel):
    """
    Model for the data to be stored in the wiki cache.
    """
    wiki_structure: WikiStructureModel
    generated_pages: Dict[str, WikiPage]
    repo_url: Optional[str] = None  #compatible for old cache
    repo: Optional[RepoInfo] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    generated_at: Optional[str] = None  # ISO-8601 UTC, set server-side on save
    repo_commit: Optional[str] = None   # git HEAD of the analyzed clone, when resolvable
    self_reviewed: Optional[bool] = None  # pages went through the self-review pass
    stats: Optional[Dict[str, Any]] = None  # per-phase token/time accounting from generation

class WikiCacheRequest(BaseModel):
    """
    Model for the request body when saving wiki cache.
    """
    repo: RepoInfo
    language: str
    wiki_structure: WikiStructureModel
    generated_pages: Dict[str, WikiPage]
    provider: str
    model: str
    self_reviewed: Optional[bool] = None
    stats: Optional[Dict[str, Any]] = None

class WikiReviewData(BaseModel):
    """A cross-model review of one saved wiki version."""
    repo: RepoInfo
    language: str
    reviewed_provider: str   # provider/model that generated the wiki under review
    reviewed_model: str
    reviewer_provider: str   # provider/model that performed the review
    reviewer_model: str
    content: str             # review markdown produced by the reviewer model
    created_at: Optional[str] = None  # ISO-8601 UTC, set server-side on save

class WikiExportRequest(BaseModel):
    """
    Model for requesting a wiki export.
    """
    repo_url: str = Field(..., description="URL of the repository")
    pages: List[WikiPage] = Field(..., description="List of wiki pages to export")
    format: Literal["markdown", "json"] = Field(..., description="Export format (markdown or json)")
    provider: Optional[str] = Field(None, description="LLM provider that generated the wiki")
    model: Optional[str] = Field(None, description="Model that generated the wiki")
    generated_at: Optional[str] = Field(None, description="When the wiki was generated (ISO-8601)")
    repo_commit: Optional[str] = Field(None, description="Repository commit the wiki was generated against")

# --- Model Configuration Models ---
class Model(BaseModel):
    """
    Model for LLM model configuration
    """
    id: str = Field(..., description="Model identifier")
    name: str = Field(..., description="Display name for the model")

class Provider(BaseModel):
    """
    Model for LLM provider configuration
    """
    id: str = Field(..., description="Provider identifier")
    name: str = Field(..., description="Display name for the provider")
    models: List[Model] = Field(..., description="List of available models for this provider")
    supportsCustomModel: Optional[bool] = Field(False, description="Whether this provider supports custom models")

class ModelConfig(BaseModel):
    """
    Model for the entire model configuration
    """
    providers: List[Provider] = Field(..., description="List of available model providers")
    defaultProvider: str = Field(..., description="ID of the default provider")

class AuthorizationConfig(BaseModel):
    code: str = Field(..., description="Authorization code")

from api.config import configs, WIKI_AUTH_MODE, WIKI_AUTH_CODE

@app.get("/lang/config")
async def get_lang_config():
    return configs["lang_config"]

@app.get("/auth/status")
async def get_auth_status():
    """
    Check if authentication is required for the wiki.
    """
    return {"auth_required": WIKI_AUTH_MODE}

@app.post("/auth/validate")
async def validate_auth_code(request: AuthorizationConfig):
    """
    Check authorization code.
    """
    return {"success": WIKI_AUTH_CODE == request.code}

@app.get("/models/config", response_model=ModelConfig)
async def get_model_config():
    """
    Get available model providers and their models.

    This endpoint returns the configuration of available model providers and their
    respective models that can be used throughout the application.

    Returns:
        ModelConfig: A configuration object containing providers and their models
    """
    try:
        logger.info("Fetching model configurations")

        # Create providers from the config file
        providers = []
        default_provider = configs.get("default_provider", "google")

        # Add provider configuration based on config.py
        for provider_id, provider_config in configs["providers"].items():
            models = []
            # Add models from config
            for model_id in provider_config["models"].keys():
                # Get a more user-friendly display name if possible
                models.append(Model(id=model_id, name=model_id))

            # Add provider with its models
            providers.append(
                Provider(
                    id=provider_id,
                    name=f"{provider_id.capitalize()}",
                    supportsCustomModel=provider_config.get("supportsCustomModel", False),
                    models=models
                )
            )

        # Create and return the full configuration
        config = ModelConfig(
            providers=providers,
            defaultProvider=default_provider
        )
        return config

    except Exception as e:
        logger.error(f"Error creating model configuration: {str(e)}")
        # Return some default configuration in case of error
        return ModelConfig(
            providers=[
                Provider(
                    id="google",
                    name="Google",
                    supportsCustomModel=True,
                    models=[
                        Model(id="gemini-2.5-flash", name="Gemini 2.5 Flash")
                    ]
                )
            ],
            defaultProvider="google"
        )

@app.post("/export/wiki")
async def export_wiki(request: WikiExportRequest):
    """
    Export wiki content as Markdown or JSON.

    Args:
        request: The export request containing wiki pages and format

    Returns:
        A downloadable file in the requested format
    """
    try:
        logger.info(f"Exporting wiki for {request.repo_url} in {request.format} format")

        # Extract repository name from URL for the filename
        repo_parts = request.repo_url.rstrip('/').split('/')
        repo_name = repo_parts[-1] if len(repo_parts) > 0 else "wiki"

        # Get current timestamp for the filename
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        if request.format == "markdown":
            # Generate Markdown content
            content = generate_markdown_export(request.repo_url, request.pages,
                                               provider=request.provider, model=request.model,
                                               generated_at=request.generated_at,
                                               repo_commit=request.repo_commit)
            filename = f"{repo_name}_wiki_{timestamp}.md"
            media_type = "text/markdown"
        else:  # JSON format
            # Generate JSON content
            content = generate_json_export(request.repo_url, request.pages,
                                           provider=request.provider, model=request.model,
                                           generated_at=request.generated_at,
                                           repo_commit=request.repo_commit)
            filename = f"{repo_name}_wiki_{timestamp}.json"
            media_type = "application/json"

        # Create response with appropriate headers for file download
        response = Response(
            content=content,
            media_type=media_type,
            headers={
                "Content-Disposition": f"attachment; filename={filename}"
            }
        )

        return response

    except Exception as e:
        logger.error(f"Error exporting wiki: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error exporting wiki.")

@app.get("/local_repo/structure")
async def get_local_repo_structure(path: str = Query(None, description="Path to local repository")):
    """Return the file tree and README content for a local repository."""
    if not path:
        return JSONResponse(
            status_code=400,
            content={"error": "No path provided. Please provide a 'path' query parameter."}
        )

    # Canonicalize the path and apply security guards.
    real_path = os.path.realpath(path)

    # Optional lockdown: if DEEPWIKI_LOCAL_REPO_BASE is set, restrict reads to that
    # base directory. When unset, behaviour is unchanged (arbitrary local folders work),
    # but symlink escape and unbounded reads below are still prevented.
    allowed_base = os.environ.get("DEEPWIKI_LOCAL_REPO_BASE")
    if allowed_base:
        real_base = os.path.realpath(allowed_base)
        if real_path != real_base and not real_path.startswith(real_base + os.sep):
            logger.warning(f"Rejected local repo path outside allowed base: {real_path}")
            return JSONResponse(
                status_code=403,
                content={"error": "Path is outside the allowed base directory."}
            )

    if not os.path.isdir(real_path):
        return JSONResponse(
            status_code=404,
            content={"error": "Directory not found."}
        )

    MAX_README_BYTES = 1_000_000
    try:
        logger.info(f"Processing local repository at: {real_path}")
        file_tree_lines = []
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

        file_tree_str = '\n'.join(sorted(file_tree_lines))
        return {"file_tree": file_tree_str, "readme": readme_content}
    except Exception as e:
        logger.error(f"Error processing local repository: {str(e)}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"error": "Error processing local repository."}
        )

def generate_markdown_export(repo_url: str, pages: List[WikiPage],
                             provider: Optional[str] = None, model: Optional[str] = None,
                             generated_at: Optional[str] = None,
                             repo_commit: Optional[str] = None) -> str:
    """
    Generate Markdown export of wiki pages.

    Args:
        repo_url: The repository URL
        pages: List of wiki pages

    Returns:
        Markdown content as string
    """
    # Start with metadata
    markdown = f"# Wiki Documentation for {repo_url}\n\n"
    markdown += f"Generated on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    if provider and model:
        markdown += f"Generated by: {provider}/{model}\n\n"
    if generated_at:
        markdown += f"Wiki generated at: {generated_at}\n\n"
    if repo_commit:
        markdown += f"Repository commit: {repo_commit}\n\n"

    # Add table of contents
    markdown += "## Table of Contents\n\n"
    for page in pages:
        markdown += f"- [{page.title}](#{page.id})\n"
    markdown += "\n"

    # Add each page
    for page in pages:
        markdown += f"<a id='{page.id}'></a>\n\n"
        markdown += f"## {page.title}\n\n"



        # Add related pages
        if page.relatedPages and len(page.relatedPages) > 0:
            markdown += "### Related Pages\n\n"
            related_titles = []
            for related_id in page.relatedPages:
                # Find the title of the related page
                related_page = next((p for p in pages if p.id == related_id), None)
                if related_page:
                    related_titles.append(f"[{related_page.title}](#{related_id})")

            if related_titles:
                markdown += "Related topics: " + ", ".join(related_titles) + "\n\n"

        # Add page content
        markdown += f"{page.content}\n\n"
        markdown += "---\n\n"

    return markdown

def generate_json_export(repo_url: str, pages: List[WikiPage],
                         provider: Optional[str] = None, model: Optional[str] = None,
                         generated_at: Optional[str] = None,
                         repo_commit: Optional[str] = None) -> str:
    """
    Generate JSON export of wiki pages.

    Args:
        repo_url: The repository URL
        pages: List of wiki pages

    Returns:
        JSON content as string
    """
    # Create a dictionary with metadata and pages
    export_data = {
        "metadata": {
            "repository": repo_url,
            "generated_at": generated_at or datetime.now().isoformat(),
            "provider": provider,
            "model": model,
            "repo_commit": repo_commit,
            "page_count": len(pages)
        },
        "pages": [page.model_dump() for page in pages]
    }

    # Convert to JSON string with pretty formatting
    return json.dumps(export_data, indent=2)

# Import the simplified chat implementation
from api.simple_chat import chat_completions_stream
from api.websocket_wiki import handle_websocket_chat

# Add the chat_completions_stream endpoint to the main app
app.add_api_route("/chat/completions/stream", chat_completions_stream, methods=["POST"])

# Add the WebSocket endpoint
app.add_websocket_route("/ws/chat", handle_websocket_chat)

def get_repo_commit(repo: RepoInfo) -> Optional[str]:
    """Best-effort git HEAD of the repo clone the wiki was generated from.

    Cloned repos live at ~/.adalflow/repos/{owner}_{repo}; local repos use their
    own path. Returns None when the clone or git metadata is unavailable.
    """
    try:
        if repo.type == "local" and repo.localPath:
            repo_dir = repo.localPath
        else:
            repo_dir = os.path.join(get_adalflow_default_root_path(), "repos",
                                    f"{repo.owner}_{repo.repo}")
        if not os.path.isdir(os.path.join(repo_dir, ".git")):
            return None
        result = subprocess.run(["git", "-C", repo_dir, "rev-parse", "HEAD"],
                                capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception as e:
        logger.warning(f"Could not determine repo commit for {repo.owner}/{repo.repo}: {e}")
    return None


# --- Wiki Cache Helper Functions ---

WIKI_CACHE_DIR = os.path.join(get_adalflow_default_root_path(), "wikicache")
os.makedirs(WIKI_CACHE_DIR, exist_ok=True)

_SAFE_CACHE_SEGMENT = re.compile(r'^[A-Za-z0-9._-]+$')
# Chars allowed in the provider/model filename segments. NOTE: no '~' and no '_',
# so '~' stays an unambiguous separator and parsing the base segments keeps working.
_VERSION_SEGMENT_UNSAFE = re.compile(r'[^A-Za-z0-9.-]')

def sanitize_version_segment(value: Optional[str]) -> str:
    """Sanitizes a provider/model identifier for use in a cache filename.

    Replaces every char outside [A-Za-z0-9.-] with '-' (e.g. 'Qwen/Qwen3-32B'
    -> 'Qwen-Qwen3-32B'). Idempotent, so values parsed back out of filenames can
    be passed in again. Returns '' for empty/None input.
    """
    if not value:
        return ""
    return _VERSION_SEGMENT_UNSAFE.sub('-', value.strip()).strip('-')

def get_wiki_cache_path(owner: str, repo: str, repo_type: str, language: str,
                        provider: Optional[str] = None, model: Optional[str] = None) -> str:
    """Generates the file path for a given wiki cache, with path-traversal guards.

    When both provider and model are given, the filename carries a
    '~{provider}~{model}' suffix so each model's wiki is cached separately.
    Without them it resolves to the legacy (un-versioned) filename.
    """
    for seg in (owner, repo, repo_type, language):
        if not seg or seg in ('.', '..') or not _SAFE_CACHE_SEGMENT.match(seg):
            raise HTTPException(status_code=400, detail="Invalid repository identifier.")
    filename = f"deepwiki_cache_{repo_type}_{owner}_{repo}_{language}"
    provider_seg = sanitize_version_segment(provider)
    model_seg = sanitize_version_segment(model)
    if provider_seg and model_seg:
        filename += f"~{provider_seg}~{model_seg}"
    filename += ".json"
    path = os.path.join(WIKI_CACHE_DIR, filename)
    # Containment backstop: the resolved path must stay inside the cache directory.
    if not os.path.realpath(path).startswith(os.path.realpath(WIKI_CACHE_DIR) + os.sep):
        raise HTTPException(status_code=400, detail="Invalid cache path.")
    return path

def list_wiki_cache_paths(owner: str, repo: str, repo_type: str, language: str) -> List[str]:
    """All cache files for a repo (legacy + per-model versions), newest first."""
    legacy_path = get_wiki_cache_path(owner, repo, repo_type, language)
    base = legacy_path[:-len(".json")]
    # Base segments are restricted to [A-Za-z0-9._-], so they contain no glob metachars.
    paths = glob.glob(f"{base}~*~*.json")
    if os.path.exists(legacy_path):
        paths.append(legacy_path)
    def _mtime(p: str) -> float:
        try:
            return os.path.getmtime(p)
        except OSError:
            return 0.0
    paths.sort(key=_mtime, reverse=True)
    return paths

def get_wiki_review_path(owner: str, repo: str, repo_type: str, language: str,
                         reviewed_provider: str, reviewed_model: str,
                         reviewer_provider: str, reviewer_model: str) -> str:
    """Path for a cross-model review file; same guards as wiki cache paths."""
    for seg in (owner, repo, repo_type, language):
        if not seg or seg in ('.', '..') or not _SAFE_CACHE_SEGMENT.match(seg):
            raise HTTPException(status_code=400, detail="Invalid repository identifier.")
    version_segs = [sanitize_version_segment(s) for s in
                    (reviewed_provider, reviewed_model, reviewer_provider, reviewer_model)]
    if not all(version_segs):
        raise HTTPException(status_code=400, detail="Invalid provider/model identifier.")
    filename = (f"deepwiki_review_{repo_type}_{owner}_{repo}_{language}~"
                + "~".join(version_segs) + ".json")
    path = os.path.join(WIKI_CACHE_DIR, filename)
    # Containment backstop: the resolved path must stay inside the cache directory.
    if not os.path.realpath(path).startswith(os.path.realpath(WIKI_CACHE_DIR) + os.sep):
        raise HTTPException(status_code=400, detail="Invalid cache path.")
    return path

def parse_wiki_cache_filename(filename: str) -> Optional[Dict[str, Optional[str]]]:
    """Parses a wiki cache filename into its components.

    Handles both legacy names (deepwiki_cache_{type}_{owner}_{repo}_{lang}.json)
    and versioned names (...~{provider}~{model}.json). Returns None if the
    filename is not a parseable cache file.
    """
    if not (filename.startswith("deepwiki_cache_") and filename.endswith(".json")):
        return None
    stem = filename[:-len(".json")]
    version_parts = stem.split('~')
    provider = version_parts[1] if len(version_parts) == 3 else None
    model = version_parts[2] if len(version_parts) == 3 else None
    parts = version_parts[0].replace("deepwiki_cache_", "").split('_')
    # Expecting repo_type_owner_repo_language; repo can contain underscores.
    if len(parts) < 4:
        return None
    return {
        "repo_type": parts[0],
        "owner": parts[1],
        "repo": "_".join(parts[2:-1]),
        "language": parts[-1],
        "provider": provider,
        "model": model,
    }

async def read_wiki_cache(owner: str, repo: str, repo_type: str, language: str,
                          provider: Optional[str] = None,
                          model: Optional[str] = None) -> Optional[WikiCacheData]:
    """Reads wiki cache data from the file system.

    With provider+model: reads that exact version (None on miss).
    Without: returns the newest cached version for the repo, skipping
    unparseable files.
    """
    if sanitize_version_segment(provider) and sanitize_version_segment(model):
        candidates = [get_wiki_cache_path(owner, repo, repo_type, language, provider, model)]
    else:
        candidates = list_wiki_cache_paths(owner, repo, repo_type, language)
    for cache_path in candidates:
        if os.path.exists(cache_path):
            try:
                with open(cache_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    cached = WikiCacheData(**data)
                    # Files written before tokens were stripped on save may still
                    # carry one; never serve it back.
                    if cached.repo and cached.repo.token:
                        cached.repo.token = None
                    return cached
            except Exception as e:
                logger.error(f"Error reading wiki cache from {cache_path}: {e}")
                continue
    return None

async def save_wiki_cache(data: WikiCacheRequest) -> bool:
    """Saves wiki cache data to the file system."""
    cache_path = get_wiki_cache_path(data.repo.owner, data.repo.repo, data.repo.type,
                                     data.language, data.provider, data.model)
    logger.info(f"Attempting to save wiki cache. Path: {cache_path}")
    try:
        # Never persist access tokens to disk (the cache JSON is re-served via GET).
        if data.repo and data.repo.token:
            data.repo.token = None
        payload = WikiCacheData(
            wiki_structure=data.wiki_structure,
            generated_pages=data.generated_pages,
            repo=data.repo,
            provider=data.provider,
            model=data.model,
            generated_at=datetime.now(timezone.utc).isoformat(),
            repo_commit=get_repo_commit(data.repo),
            self_reviewed=data.self_reviewed,
            stats=data.stats,
        )
        # Log size of data to be cached for debugging (avoid logging full content if large)
        try:
            payload_json = payload.model_dump_json()
            payload_size = len(payload_json.encode('utf-8'))
            logger.info(f"Payload prepared for caching. Size: {payload_size} bytes.")
        except Exception as ser_e:
            logger.warning(f"Could not serialize payload for size logging: {ser_e}")


        logger.info(f"Writing cache file to: {cache_path}")
        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump(payload.model_dump(), f, indent=2)
        logger.info(f"Wiki cache successfully saved to {cache_path}")
        return True
    except IOError as e:
        logger.error(f"IOError saving wiki cache to {cache_path}: {e.strerror} (errno: {e.errno})", exc_info=True)
        return False
    except Exception as e:
        logger.error(f"Unexpected error saving wiki cache to {cache_path}: {e}", exc_info=True)
        return False

# --- Wiki Cache API Endpoints ---

@app.get("/api/wiki_cache", response_model=Optional[WikiCacheData])
async def get_cached_wiki(
    owner: str = Query(..., description="Repository owner"),
    repo: str = Query(..., description="Repository name"),
    repo_type: str = Query(..., description="Repository type (e.g., github, gitlab)"),
    language: str = Query(..., description="Language of the wiki content"),
    provider: Optional[str] = Query(None, description="LLM provider of the cached version; omit for newest"),
    model: Optional[str] = Query(None, description="Model of the cached version; omit for newest")
):
    """
    Retrieves cached wiki data (structure and generated pages) for a repository.
    With provider+model, returns that exact version; otherwise the newest one.
    """
    # Language validation
    supported_langs = configs["lang_config"]["supported_languages"]
    if not supported_langs.__contains__(language):
        language = configs["lang_config"]["default"]

    logger.info(f"Attempting to retrieve wiki cache for {owner}/{repo} ({repo_type}), lang: {language}, provider: {provider}, model: {model}")
    cached_data = await read_wiki_cache(owner, repo, repo_type, language, provider, model)
    if cached_data:
        return cached_data
    else:
        # Return 200 with null body if not found, as frontend expects this behavior
        logger.info(f"Wiki cache not found for {owner}/{repo} ({repo_type}), lang: {language}")
        return None

@app.post("/api/wiki_cache")
async def store_wiki_cache(request_data: WikiCacheRequest):
    """
    Stores generated wiki data (structure and pages) to the server-side cache.
    """
    # Language validation
    supported_langs = configs["lang_config"]["supported_languages"]

    if not supported_langs.__contains__(request_data.language):
        request_data.language = configs["lang_config"]["default"]

    logger.info(f"Attempting to save wiki cache for {request_data.repo.owner}/{request_data.repo.repo} ({request_data.repo.type}), lang: {request_data.language}")
    success = await save_wiki_cache(request_data)
    if success:
        saved = await read_wiki_cache(request_data.repo.owner, request_data.repo.repo,
                                      request_data.repo.type, request_data.language,
                                      request_data.provider, request_data.model)
        return {
            "message": "Wiki cache saved successfully",
            "generated_at": saved.generated_at if saved else None,
            "repo_commit": saved.repo_commit if saved else None,
        }
    else:
        raise HTTPException(status_code=500, detail="Failed to save wiki cache")

@app.delete("/api/wiki_cache")
async def delete_wiki_cache(
    owner: str = Query(..., description="Repository owner"),
    repo: str = Query(..., description="Repository name"),
    repo_type: str = Query(..., description="Repository type (e.g., github, gitlab)"),
    language: str = Query(..., description="Language of the wiki content"),
    provider: Optional[str] = Query(None, description="LLM provider of the version to delete; omit to delete all versions"),
    model: Optional[str] = Query(None, description="Model of the version to delete; omit to delete all versions"),
    authorization_code: Optional[str] = Query(None, description="Authorization code")
):
    """
    Deletes wiki cache from the file system. With provider+model, deletes that
    version only; otherwise deletes every cached version for the repo.
    """
    # Language validation
    supported_langs = configs["lang_config"]["supported_languages"]
    if not supported_langs.__contains__(language):
        raise HTTPException(status_code=400, detail="Language is not supported")

    if WIKI_AUTH_MODE:
        logger.info("check the authorization code")
        if not authorization_code or WIKI_AUTH_CODE != authorization_code:
            raise HTTPException(status_code=401, detail="Authorization code is invalid")

    logger.info(f"Attempting to delete wiki cache for {owner}/{repo} ({repo_type}), lang: {language}, provider: {provider}, model: {model}")
    if sanitize_version_segment(provider) and sanitize_version_segment(model):
        paths = [get_wiki_cache_path(owner, repo, repo_type, language, provider, model)]
    else:
        paths = list_wiki_cache_paths(owner, repo, repo_type, language)

    deleted = []
    for cache_path in paths:
        if os.path.exists(cache_path):
            try:
                os.remove(cache_path)
                deleted.append(os.path.basename(cache_path))
                logger.info(f"Successfully deleted wiki cache: {cache_path}")
            except Exception as e:
                logger.error(f"Error deleting wiki cache {cache_path}: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail="Failed to delete wiki cache.")

    if not deleted:
        logger.warning(f"Wiki cache not found, cannot delete: {owner}/{repo} ({language}), provider: {provider}, model: {model}")
        raise HTTPException(status_code=404, detail="Wiki cache not found")
    return {
        "message": f"Deleted {len(deleted)} wiki cache file(s) for {owner}/{repo} ({language})",
        "deleted": deleted,
    }

@app.post("/api/wiki_review")
async def store_wiki_review(review: WikiReviewData):
    """Saves a cross-model review next to the wiki caches (same key overwrites)."""
    review.created_at = datetime.now(timezone.utc).isoformat()
    # Never persist access tokens to disk (the review JSON is re-served via GET).
    review.repo.token = None
    review_path = get_wiki_review_path(
        review.repo.owner, review.repo.repo, review.repo.type, review.language,
        review.reviewed_provider, review.reviewed_model,
        review.reviewer_provider, review.reviewer_model)
    try:
        with open(review_path, 'w', encoding='utf-8') as f:
            json.dump(review.model_dump(), f, indent=2)
        logger.info(f"Wiki review saved to {review_path}")
        return {"message": "Wiki review saved successfully", "created_at": review.created_at}
    except Exception as e:
        logger.error(f"Error saving wiki review to {review_path}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to save wiki review")

@app.get("/api/wiki_review", response_model=List[WikiReviewData])
async def get_wiki_reviews(
    owner: str = Query(..., description="Repository owner"),
    repo: str = Query(..., description="Repository name"),
    repo_type: str = Query(..., description="Repository type (e.g., github, gitlab)"),
    language: str = Query(..., description="Language of the wiki content")
):
    """Lists all saved cross-model reviews for a repository, newest first."""
    for seg in (owner, repo, repo_type, language):
        if not seg or seg in ('.', '..') or not _SAFE_CACHE_SEGMENT.match(seg):
            raise HTTPException(status_code=400, detail="Invalid repository identifier.")
    pattern = os.path.join(WIKI_CACHE_DIR,
                           f"deepwiki_review_{repo_type}_{owner}_{repo}_{language}~*.json")
    reviews = []
    for review_path in glob.glob(pattern):
        try:
            with open(review_path, 'r', encoding='utf-8') as f:
                review = WikiReviewData(**json.load(f))
                # Files written before tokens were stripped on save may still
                # carry one; never serve it back.
                if review.repo.token:
                    review.repo.token = None
                reviews.append(review)
        except Exception as e:
            logger.error(f"Error reading wiki review from {review_path}: {e}")
            continue
    reviews.sort(key=lambda r: r.created_at or "", reverse=True)
    return reviews

@app.get("/health")
async def health_check():
    """Health check endpoint for Docker and monitoring"""
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "service": "deepwiki-api"
    }

@app.get("/")
async def root():
    """Root endpoint to check if the API is running and list available endpoints dynamically."""
    # Collect routes dynamically from the FastAPI app
    endpoints = {}
    for route in app.routes:
        if hasattr(route, "methods") and hasattr(route, "path"):
            # Skip docs and static routes
            if route.path in ["/openapi.json", "/docs", "/redoc", "/favicon.ico"]:
                continue
            # Group endpoints by first path segment
            path_parts = route.path.strip("/").split("/")
            group = path_parts[0].capitalize() if path_parts[0] else "Root"
            method_list = list(route.methods - {"HEAD", "OPTIONS"})
            for method in method_list:
                endpoints.setdefault(group, []).append(f"{method} {route.path}")

    # Optionally, sort endpoints for readability
    for group in endpoints:
        endpoints[group].sort()

    return {
        "message": "Welcome to Streaming API",
        "version": "1.0.0",
        "endpoints": endpoints
    }

# --- Processed Projects Endpoint --- (New Endpoint)
@app.get("/api/processed_projects", response_model=List[ProcessedProjectEntry])
async def get_processed_projects():
    """
    Lists all processed projects found in the wiki cache directory.
    Projects are identified by files named like: deepwiki_cache_{repo_type}_{owner}_{repo}_{language}.json
    """
    project_entries: List[ProcessedProjectEntry] = []
    # WIKI_CACHE_DIR is already defined globally in the file

    try:
        if not os.path.exists(WIKI_CACHE_DIR):
            logger.info(f"Cache directory {WIKI_CACHE_DIR} not found. Returning empty list.")
            return []

        logger.info(f"Scanning for project cache files in: {WIKI_CACHE_DIR}")
        filenames = await asyncio.to_thread(os.listdir, WIKI_CACHE_DIR) # Use asyncio.to_thread for os.listdir

        for filename in filenames:
            if filename.startswith("deepwiki_cache_") and filename.endswith(".json"):
                file_path = os.path.join(WIKI_CACHE_DIR, filename)
                try:
                    parsed = parse_wiki_cache_filename(filename)
                    if not parsed:
                        logger.warning(f"Could not parse project details from filename: {filename}")
                        continue
                    stats = await asyncio.to_thread(os.stat, file_path) # Use asyncio.to_thread for os.stat
                    # Pull the generation stats out of the cache JSON. This reads
                    # the whole file, which is fine at this deployment's scale
                    # (a handful of caches); revisit with a sidecar if it grows.
                    generation_stats = None
                    try:
                        def _read_stats(path: str):
                            with open(path, 'r', encoding='utf-8') as f:
                                return json.load(f).get('stats')
                        generation_stats = await asyncio.to_thread(_read_stats, file_path)
                    except Exception:
                        pass  # stats are cosmetic; never fail the listing over them
                    project_entries.append(
                        ProcessedProjectEntry(
                            id=filename,
                            owner=parsed["owner"],
                            repo=parsed["repo"],
                            name=f"{parsed['owner']}/{parsed['repo']}",
                            repo_type=parsed["repo_type"],
                            submittedAt=int(stats.st_mtime * 1000), # Convert to milliseconds
                            language=parsed["language"],
                            provider=parsed["provider"],
                            model=parsed["model"],
                            stats=generation_stats,
                        )
                    )
                except Exception as e:
                    logger.error(f"Error processing file {file_path}: {e}")
                    continue # Skip this file on error

        # Sort by most recent first
        project_entries.sort(key=lambda p: p.submittedAt, reverse=True)
        logger.info(f"Found {len(project_entries)} processed project entries.")
        return project_entries

    except Exception as e:
        logger.error(f"Error listing processed projects from {WIKI_CACHE_DIR}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to list processed projects from server cache.")
