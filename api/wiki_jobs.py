"""Job queue for server-side wiki generation.

A JobManager owns the job registry, an asyncio queue and N worker tasks
(WIKI_JOBS_CONCURRENCY, default 2) that run api.wiki_generator.run_generation.
Every status/progress change is journaled to disk (public dicts only — tokens
stay in memory), so a restart shows interrupted jobs instead of losing them.
"""
import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from api import llm_dispatch
from api.api import RepoInfo, get_adalflow_default_root_path
from api.wiki_generator import JobCancelled, JobProgress, run_generation

logger = logging.getLogger(__name__)

WIKI_JOBS_CONCURRENCY = int(os.getenv("WIKI_JOBS_CONCURRENCY", "2"))
QUEUE_CAP = 20            # max queued+running jobs
FINISHED_RETENTION = 50   # finished jobs kept in the registry/journal
JOURNAL_PATH = os.path.join(get_adalflow_default_root_path(), "wikicache", "wiki_jobs.json")

ACTIVE_STATUSES = ("queued", "running")


class DuplicateJob(Exception):
    """A job with the same identity is already queued or running."""


class QueueFull(Exception):
    """The queue has reached QUEUE_CAP active jobs."""


@dataclass
class WikiJob:
    repo: RepoInfo               # token kept ONLY in memory, never journaled
    language: str
    provider: str
    model: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    comprehensive: bool = True
    self_review: bool = True
    force_regenerate: bool = False
    excluded_dirs: Optional[str] = None
    excluded_files: Optional[str] = None
    included_dirs: Optional[str] = None
    included_files: Optional[str] = None
    status: str = "queued"       # queued|running|done|failed|cancelled|interrupted
    progress: JobProgress = field(default_factory=JobProgress)
    stats: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    cancel_requested: bool = False
    created_at: float = 0.0
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    def key(self):
        # Dedupe identity. NOTE: `comprehensive` is deliberately absent — the
        # cache path ignores it too, so comprehensive/concise share one cache
        # slot (pre-existing behavior); two jobs differing only in it dedupe
        # to one.
        return (self.repo.owner, self.repo.repo, self.repo.type,
                self.language, self.provider, self.model)

    def to_public_dict(self) -> Dict[str, Any]:
        """Journal/API shape: NO token, timestamps as ISO-8601 UTC."""
        def iso(ts: Optional[float]) -> Optional[str]:
            return (datetime.fromtimestamp(ts, timezone.utc).isoformat()
                    if ts else None)
        return {
            "id": self.id,
            "repo": {"owner": self.repo.owner, "repo": self.repo.repo,
                     "type": self.repo.type, "repoUrl": self.repo.repoUrl,
                     "localPath": self.repo.localPath},
            "language": self.language,
            "provider": self.provider,
            "model": self.model,
            "comprehensive": self.comprehensive,
            "self_review": self.self_review,
            "force_regenerate": self.force_regenerate,
            "status": self.status,
            "progress": {"phase": self.progress.phase,
                         "pages_total": self.progress.pages_total,
                         "pages_done": self.progress.pages_done,
                         "current_page_title": self.progress.current_page_title},
            "stats": self.stats,
            "error": self.error,
            "created_at": iso(self.created_at),
            "started_at": iso(self.started_at),
            "finished_at": iso(self.finished_at),
        }

    @classmethod
    def from_public_dict(cls, data: Dict[str, Any]) -> "WikiJob":
        def epoch(value: Optional[str]) -> Optional[float]:
            if not value:
                return None
            try:
                return datetime.fromisoformat(value).timestamp()
            except ValueError:
                return None
        progress = JobProgress(**(data.get("progress") or {}))
        return cls(
            repo=RepoInfo(**(data.get("repo") or {})),
            language=data.get("language", "en"),
            provider=data.get("provider", ""),
            model=data.get("model", ""),
            id=data.get("id") or uuid.uuid4().hex,
            comprehensive=data.get("comprehensive", True),
            self_review=data.get("self_review", True),
            force_regenerate=data.get("force_regenerate", False),
            status=data.get("status", "interrupted"),
            progress=progress,
            stats=data.get("stats") or {},
            error=data.get("error"),
            created_at=epoch(data.get("created_at")) or 0.0,
            started_at=epoch(data.get("started_at")),
            finished_at=epoch(data.get("finished_at")),
        )


class JobManager:
    def __init__(self, concurrency: int = WIKI_JOBS_CONCURRENCY,
                 journal_path: str = JOURNAL_PATH):
        self.concurrency = concurrency
        self.journal_path = journal_path
        self.jobs: Dict[str, WikiJob] = {}   # id -> job, insertion-ordered
        self.queue: asyncio.Queue = asyncio.Queue()
        self.workers: List[asyncio.Task] = []
        self.dispatch = llm_dispatch.generate
        self._load_journal()

    # --- registry -------------------------------------------------------

    def submit(self, job: WikiJob) -> WikiJob:
        active = [j for j in self.jobs.values() if j.status in ACTIVE_STATUSES]
        for other in active:
            if other.key() == job.key():
                raise DuplicateJob(
                    f"A generation job for {job.repo.owner}/{job.repo.repo} "
                    f"({job.provider}/{job.model}, {job.language}) is already "
                    f"{other.status} (id {other.id})")
        if len(active) >= QUEUE_CAP:
            raise QueueFull(f"Job queue is full ({QUEUE_CAP} active jobs)")
        job.created_at = time.time()
        job.status = "queued"
        self.jobs[job.id] = job
        self._trim_finished()
        self.queue.put_nowait(job.id)
        self._persist()
        logger.info(f"Queued wiki job {job.id}: {job.repo.owner}/{job.repo.repo} "
                    f"({job.provider}/{job.model}, {job.language})")
        return job

    def get(self, job_id: str) -> Optional[WikiJob]:
        return self.jobs.get(job_id)

    def list_jobs(self) -> List[WikiJob]:
        return list(self.jobs.values())

    def remove(self, job_id: str) -> Optional[WikiJob]:
        """Drop a finished job from the registry/journal (dismiss from UI).

        Returns None for unknown ids; raises ValueError while the job is
        still queued/running — cancel it first.
        """
        job = self.jobs.get(job_id)
        if job is None:
            return None
        if job.status in ACTIVE_STATUSES:
            raise ValueError(f"Job {job_id} is {job.status}; cancel it before removing")
        del self.jobs[job_id]
        self._persist()
        logger.info(f"Removed wiki job {job.id} (was {job.status})")
        return job

    def cancel(self, job_id: str) -> Optional[WikiJob]:
        job = self.jobs.get(job_id)
        if job is None:
            return None
        job.cancel_requested = True
        if job.status == "queued":
            # Not picked up yet: finish it immediately (the worker skips it)
            job.status = "cancelled"
            job.finished_at = time.time()
        self._persist()
        logger.info(f"Cancel requested for wiki job {job.id} (status {job.status})")
        return job

    def _trim_finished(self) -> None:
        finished = [j for j in self.jobs.values() if j.status not in ACTIVE_STATUSES]
        excess = len(finished) - FINISHED_RETENTION
        if excess > 0:
            finished.sort(key=lambda j: j.finished_at or j.created_at)
            for job in finished[:excess]:
                del self.jobs[job.id]

    # --- workers ----------------------------------------------------------

    def start(self) -> None:
        """Spawn the worker tasks on the running event loop."""
        for i in range(self.concurrency):
            self.workers.append(asyncio.create_task(
                self._worker(), name=f"wiki-job-worker-{i}"))
        logger.info(f"Started {self.concurrency} wiki job workers")

    def stop(self) -> None:
        for task in self.workers:
            task.cancel()
        self.workers = []

    async def _worker(self) -> None:
        while True:
            job_id = await self.queue.get()
            try:
                job = self.jobs.get(job_id)
                if job is None or job.status != "queued":
                    continue  # cancelled while queued, or trimmed
                job.status = "running"
                job.started_at = time.time()
                self._persist()
                try:
                    await run_generation(job, self.dispatch,
                                         on_progress=lambda j: self._persist())
                    job.status = "done"
                except JobCancelled:
                    job.status = "cancelled"
                    logger.info(f"Wiki job {job.id} cancelled")
                except Exception as e:
                    job.status = "failed"
                    job.error = str(e)
                    logger.error(f"Wiki job {job.id} failed: {e}", exc_info=True)
                job.finished_at = time.time()
                self._persist()
            finally:
                self.queue.task_done()

    # --- journal -----------------------------------------------------------

    def _persist(self) -> None:
        """Atomically write all jobs' public dicts (never the token)."""
        try:
            os.makedirs(os.path.dirname(self.journal_path), exist_ok=True)
            tmp_path = f"{self.journal_path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump({"jobs": [j.to_public_dict() for j in self.jobs.values()]},
                          f, indent=2)
            os.replace(tmp_path, self.journal_path)
        except OSError as e:
            logger.warning(f"Could not persist wiki job journal: {e}")

    def _load_journal(self) -> None:
        try:
            with open(self.journal_path, encoding="utf-8") as f:
                entries = json.load(f).get("jobs", [])
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            logger.warning(f"Could not read wiki job journal: {e}")
            return
        for entry in entries[-FINISHED_RETENTION:]:
            try:
                job = WikiJob.from_public_dict(entry)
            except Exception as e:
                logger.warning(f"Skipping unreadable journal entry: {e}")
                continue
            if job.status in ACTIVE_STATUSES:
                # The process died mid-run; pages saved so far are in the cache
                job.status = "interrupted"
                job.finished_at = job.finished_at or time.time()
            self.jobs[job.id] = job


_manager: Optional[JobManager] = None


def get_manager() -> JobManager:
    global _manager
    if _manager is None:
        _manager = JobManager()
    return _manager
