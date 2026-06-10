"""Tests for api/wiki_jobs.py — stubbed run_generation, no network/LLM."""
import asyncio
import json

import pytest

import api.wiki_jobs as wiki_jobs
from api.api import RepoInfo
from api.wiki_generator import JobCancelled
from api.wiki_jobs import DuplicateJob, JobManager, QueueFull, WikiJob


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def make_job(owner="o", repo="r", provider="vllm", model="m", **kw):
    return WikiJob(
        repo=RepoInfo(owner=owner, repo=repo, type="github", token="secret-token"),
        language="en", provider=provider, model=model, **kw)


@pytest.fixture
def journal(tmp_path):
    return str(tmp_path / "wiki_jobs.json")


async def wait_for(predicate, timeout=5.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while not predicate():
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError("condition not met in time")
        await asyncio.sleep(0.01)


# --- lifecycle -----------------------------------------------------------------


def test_submit_to_done_lifecycle(journal, monkeypatch):
    async def fake_run(job, dispatch, on_progress=None):
        job.progress.phase = "done"
        job.progress.pages_done = 3
    monkeypatch.setattr(wiki_jobs, "run_generation", fake_run)

    async def main():
        mgr = JobManager(concurrency=2, journal_path=journal)
        mgr.start()
        try:
            job = mgr.submit(make_job())
            assert job.status == "queued"
            await wait_for(lambda: job.status == "done")
            assert job.started_at and job.finished_at
            assert job.progress.pages_done == 3
        finally:
            mgr.stop()
    run(main())


def test_failure_and_cancel_mapping(journal, monkeypatch):
    async def fake_run(job, dispatch, on_progress=None):
        if job.repo.repo == "boom":
            raise ValueError("kaboom")
        raise JobCancelled()
    monkeypatch.setattr(wiki_jobs, "run_generation", fake_run)

    async def main():
        mgr = JobManager(concurrency=2, journal_path=journal)
        mgr.start()
        try:
            failing = mgr.submit(make_job(repo="boom"))
            cancelled = mgr.submit(make_job(repo="other"))
            await wait_for(lambda: failing.status == "failed"
                           and cancelled.status == "cancelled")
            assert failing.error == "kaboom"
            assert cancelled.error is None
        finally:
            mgr.stop()
    run(main())


# --- dedupe + capacity ------------------------------------------------------------


def test_duplicate_job_rejected(journal):
    mgr = JobManager(concurrency=2, journal_path=journal)
    mgr.submit(make_job())
    with pytest.raises(DuplicateJob):
        mgr.submit(make_job())  # same key
    # different model is a different version -> allowed
    mgr.submit(make_job(model="m2"))
    # comprehensive is NOT part of the key (shares one cache slot)
    with pytest.raises(DuplicateJob):
        mgr.submit(make_job(comprehensive=False))


def test_resubmit_allowed_after_finish(journal):
    mgr = JobManager(concurrency=2, journal_path=journal)
    job = mgr.submit(make_job())
    job.status = "done"
    mgr.submit(make_job())  # no DuplicateJob


def test_queue_cap(journal, monkeypatch):
    monkeypatch.setattr(wiki_jobs, "QUEUE_CAP", 2)
    mgr = JobManager(concurrency=2, journal_path=journal)
    mgr.submit(make_job(repo="r1"))
    mgr.submit(make_job(repo="r2"))
    with pytest.raises(QueueFull):
        mgr.submit(make_job(repo="r3"))


# --- parallelism ----------------------------------------------------------------------


def test_two_workers_run_in_parallel_third_waits(journal, monkeypatch):
    started = []
    gate = asyncio.Event()

    async def slow_run(job, dispatch, on_progress=None):
        started.append(job.repo.repo)
        await gate.wait()
    monkeypatch.setattr(wiki_jobs, "run_generation", slow_run)

    async def main():
        mgr = JobManager(concurrency=2, journal_path=journal)
        mgr.start()
        try:
            j1 = mgr.submit(make_job(repo="r1"))
            j2 = mgr.submit(make_job(repo="r2"))
            j3 = mgr.submit(make_job(repo="r3"))
            await wait_for(lambda: len(started) == 2)
            await asyncio.sleep(0.05)  # give a (wrong) third start a chance
            assert sorted(started) == ["r1", "r2"]
            assert j3.status == "queued"
            gate.set()
            await wait_for(lambda: j1.status == j2.status == j3.status == "done")
            assert "r3" in started
        finally:
            mgr.stop()
    run(main())


# --- cancellation -----------------------------------------------------------------------


def test_cancel_queued_job(journal):
    mgr = JobManager(concurrency=2, journal_path=journal)  # workers NOT started
    job = mgr.submit(make_job())
    cancelled = mgr.cancel(job.id)
    assert cancelled.status == "cancelled"
    assert cancelled.finished_at is not None


def test_cancelled_queued_job_skipped_by_worker(journal, monkeypatch):
    ran = []

    async def fake_run(job, dispatch, on_progress=None):
        ran.append(job.id)
    monkeypatch.setattr(wiki_jobs, "run_generation", fake_run)

    async def main():
        mgr = JobManager(concurrency=1, journal_path=journal)
        job = mgr.submit(make_job())
        mgr.cancel(job.id)
        mgr.start()
        try:
            other = mgr.submit(make_job(repo="other"))
            await wait_for(lambda: other.status == "done")
            assert ran == [other.id]
            assert job.status == "cancelled"
        finally:
            mgr.stop()
    run(main())


def test_cancel_running_job(journal, monkeypatch):
    async def cancellable_run(job, dispatch, on_progress=None):
        while not job.cancel_requested:
            await asyncio.sleep(0.01)
        raise JobCancelled()
    monkeypatch.setattr(wiki_jobs, "run_generation", cancellable_run)

    async def main():
        mgr = JobManager(concurrency=1, journal_path=journal)
        mgr.start()
        try:
            job = mgr.submit(make_job())
            await wait_for(lambda: job.status == "running")
            result = mgr.cancel(job.id)
            assert result.status == "running"  # flag set, engine reacts
            await wait_for(lambda: job.status == "cancelled")
        finally:
            mgr.stop()
    run(main())


def test_cancel_unknown_job(journal):
    mgr = JobManager(concurrency=1, journal_path=journal)
    assert mgr.cancel("nope") is None


# --- removal (dismiss) ------------------------------------------------------


def test_remove_finished_job_and_journal(journal):
    mgr = JobManager(concurrency=2, journal_path=journal)
    job = mgr.submit(make_job())
    job.status = "failed"
    mgr._persist()

    removed = mgr.remove(job.id)
    assert removed.id == job.id
    assert mgr.get(job.id) is None
    with open(journal, encoding="utf-8") as f:
        assert job.id not in f.read()
    # a fresh manager no longer sees it
    assert JobManager(concurrency=2, journal_path=journal).list_jobs() == []


def test_remove_active_job_refused(journal):
    mgr = JobManager(concurrency=2, journal_path=journal)
    job = mgr.submit(make_job())  # queued
    with pytest.raises(ValueError, match="cancel it before removing"):
        mgr.remove(job.id)
    job.status = "running"
    with pytest.raises(ValueError, match="cancel it before removing"):
        mgr.remove(job.id)
    assert mgr.get(job.id) is not None


def test_remove_unknown_job(journal):
    mgr = JobManager(concurrency=1, journal_path=journal)
    assert mgr.remove("nope") is None


# --- journal -------------------------------------------------------------------------------


def test_journal_roundtrip_marks_interrupted(journal):
    mgr = JobManager(concurrency=2, journal_path=journal)
    queued = mgr.submit(make_job(repo="queued-repo"))
    running = mgr.submit(make_job(repo="running-repo"))
    running.status = "running"
    done = mgr.submit(make_job(repo="done-repo"))
    done.status = "done"
    mgr._persist()

    # token never serialized
    with open(journal, encoding="utf-8") as f:
        text = f.read()
    assert "secret-token" not in text

    # "restart": a fresh manager reads the same journal
    mgr2 = JobManager(concurrency=2, journal_path=journal)
    by_repo = {j.repo.repo: j for j in mgr2.list_jobs()}
    assert by_repo["queued-repo"].status == "interrupted"
    assert by_repo["running-repo"].status == "interrupted"
    assert by_repo["done-repo"].status == "done"
    assert by_repo["queued-repo"].id == queued.id
    assert by_repo["queued-repo"].repo.token is None


def test_journal_public_dict_shape(journal):
    mgr = JobManager(concurrency=2, journal_path=journal)
    job = mgr.submit(make_job())
    public = job.to_public_dict()
    assert "token" not in json.dumps(public)
    assert public["status"] == "queued"
    assert public["progress"]["phase"] == "queued"
    assert public["created_at"].startswith("20")  # ISO timestamp


def test_missing_journal_is_fine(tmp_path):
    mgr = JobManager(concurrency=2, journal_path=str(tmp_path / "missing.json"))
    assert mgr.list_jobs() == []


def test_corrupt_journal_is_fine(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    mgr = JobManager(concurrency=2, journal_path=str(path))
    assert mgr.list_jobs() == []
