"""100-job stress suite: scale, budgets, classified retries, and safety invariants.

Jobs run through the real pipeline against the temporary database with mocked providers.
The suite injects provider failures at specific comments to prove the classification
rules hold under load, not just in a single-job unit test:

- transient failures retry with backoff and recover within ``max_attempts``;
- provider budget exhaustion parks jobs (attempt refunded) and never fails them;
- a poisoned generation (invalid provider response) routes to human review, never a crash;
- an unknown publication outcome stops immediately and is never retried;
- duplicate comments never create two jobs, and no comment is ever published twice.
"""

import asyncio
from collections import Counter
from typing import ClassVar

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.models import ReplyJob, ReplyPublication
from app.models.session import utc_now
from app.services import llm
from app.services.youtube_api import YouTubePublishFailure

COUNT = 100
ACTIVE = ("retry", "pending", "processing", "verified", "publish_claimed")


@pytest.fixture(autouse=True)
def langgraph(monkeypatch):
    monkeypatch.setattr(settings, "draft_workflow_engine", "langgraph")


@pytest.fixture
def stress(workspace, monkeypatch):
    # Routing/classification stress, not a budget-by-default suite: lift the day budget
    # and never rate-limit YouTube reads. Per-minute LLM budget is set per-test.
    monkeypatch.setattr(settings, "llm_requests_per_day", 100_000)
    monkeypatch.setattr(settings, "llm_requests_per_minute", 100_000)
    monkeypatch.setattr(settings, "youtube_reads_per_minute", 100_000)
    monkeypatch.setattr(settings, "auto_publish_max_per_hour", 100_000)
    monkeypatch.setattr(settings, "auto_publish_max_per_day", 100_000)

    async def credentials_for_connection(connection, db):
        return object()

    async def owned_comment(connection, credentials, video_id, comment_id):
        return comment_id

    monkeypatch.setattr("app.services.job_pipeline.credentials_for_connection",
                        credentials_for_connection)
    monkeypatch.setattr("app.services.job_pipeline.get_owned_comment", owned_comment)
    monkeypatch.setattr("app.routers.publication._credentials", lambda *a, **k: _async(object()))

    state = {"published": [], "generation_attempts": Counter()}

    async def insert(_credentials, parent_comment_id, text):
        state["published"].append(parent_comment_id)
        return f"reply-{len(state['published'])}"

    monkeypatch.setattr("app.routers.publication.insert_comment_reply", insert)

    class Flaky(workspace.provider):
        """Injects generation failures keyed off the comment text.

        Sets live on the class so an *instance* created per request observes the
        injection applied by the test. Nothing is assigned per-instance.
        """

        flaky: ClassVar[set] = set()
        poisoned: ClassVar[set] = set()

        async def complete(self, schema, instruction, data):
            comment = data.get("comment")
            if schema is llm.Generation and comment in Flaky.poisoned:
                raise llm.ProviderFailure("invalid_response")
            if schema is llm.Generation and comment in Flaky.flaky:
                state["generation_attempts"][comment] += 1
                if state["generation_attempts"][comment] <= 1:
                    raise llm.ProviderFailure("transient_error")
            return await super().complete(schema, instruction, data)

    monkeypatch.setattr(llm, "provider", Flaky)
    workspace.state = state
    workspace.flaky_provider = Flaky
    return workspace


async def _async(value):
    return value


def _set_mode(workspace, mode):
    current = workspace.client.get("/api/publishing-settings").json()
    return workspace.client.put(
        "/api/publishing-settings", headers=workspace.headers,
        json={"mode": mode, "revision": current["revision"],
              "confirm_automation": mode != "manual_review"},
    )


def _enqueue(workspace, comment_ids):
    response = workspace.client.post(
        "/api/batch/jobs/enqueue", headers=workspace.headers,
        json={"video_id": "abcdefghijk", "comment_ids": comment_ids},
    )
    assert response.status_code == 202, response.text
    return response.json()


def _run_once(workspace, worker_id="stress-worker"):
    from app import worker

    async def main():
        async with workspace.sessions() as db:
            return await worker.run_once(worker_id, session=db)
    return asyncio.run(main())


async def _make_retries_due(workspace):
    from sqlalchemy import update

    async with workspace.sessions() as db:
        await db.execute(
            update(ReplyJob).where(ReplyJob.status == "retry").values(next_run_at=utc_now())
        )
        await db.commit()


def _drain(workspace, max_passes=COUNT * 5, expedite=False):
    passes = 0
    while passes < max_passes:
        if expedite:
            asyncio.run(_make_retries_due(workspace))
        if _run_once(workspace)["status"] == "idle":
            return passes
        passes += 1
    raise AssertionError(f"queue did not drain within {max_passes} passes")


def _jobs(workspace):
    async def main():
        async with workspace.sessions() as db:
            return (await db.execute(select(ReplyJob))).scalars().all()
    return asyncio.run(main())


def _distinct_active_pairs(workspace):
    async def main():
        async with workspace.sessions() as db:
            subq = (
                select(ReplyJob.channel_id, ReplyJob.comment_id)
                .where(ReplyJob.status.in_(ACTIVE))
                .subquery()
            )
            return await db.scalar(select(func.count()).select_from(subq))
    return asyncio.run(main())


def _posted_count(workspace):
    async def main():
        async with workspace.sessions() as db:
            return await db.scalar(
                select(func.count()).select_from(ReplyPublication)
                .where(ReplyPublication.status == "posted")
            )
    return asyncio.run(main())


def test_100_jobs_complete_and_recover_from_injected_transient_failures(workspace, stress):
    assert _set_mode(workspace, "approve_all").status_code == 200
    comments = [f"stress on topic {i}" for i in range(COUNT)]
    enqueued = _enqueue(workspace, comments)
    assert len(enqueued["created"]) == COUNT

    stress.flaky_provider.flaky = set(comments[:10])  # each fails its first generation

    _drain(workspace, expedite=True)

    jobs = _jobs(workspace)
    statuses = Counter(j.status for j in jobs)
    assert len(jobs) == COUNT
    assert statuses["published"] == COUNT, statuses  # flaky jobs recovered, not failed
    assert sum(stress.state["generation_attempts"].values()) == 20  # 2 attempts each

    published = stress.state["published"]
    assert len(published) == COUNT
    assert len(set(published)) == COUNT  # no comment published twice
    assert _posted_count(workspace) == COUNT
    assert _distinct_active_pairs(workspace) == 0  # everything reached a terminal state


def test_provider_budget_parks_jobs_and_never_fails_them(workspace, stress, monkeypatch):
    monkeypatch.setattr(settings, "llm_requests_per_minute", 5)
    assert _set_mode(workspace, "approve_all").status_code == 200
    comments = [f"budget on topic {i}" for i in range(COUNT)]
    _enqueue(workspace, comments)

    parked = 0
    for _ in range(COUNT):
        status = _run_once(workspace)["status"]
        if status == "idle":
            break
        if status == "retry":
            parked += 1
    assert parked >= 1  # the per-minute cap forced at least one job to wait

    jobs = _jobs(workspace)
    statuses = Counter(j.status for j in jobs)
    assert statuses["failed"] == 0, statuses
    assert statuses["published"] >= 1
    for job in jobs:
        assert job.attempts <= job.max_attempts
        if job.status == "retry":
            assert job.outcome == {"waiting_for": "provider_budget"}
            assert job.attempts == 0  # the wait refunded the attempt


def test_poisoned_generation_routes_to_review_never_crashes(workspace, stress):
    assert _set_mode(workspace, "approve_all").status_code == 200
    comments = [f"poison {i}" for i in range(20)]
    stress.flaky_provider.poisoned = set(comments)
    _enqueue(workspace, comments)
    _drain(workspace)

    statuses = Counter(j.status for j in _jobs(workspace))
    assert statuses["review"] == 20, statuses
    assert statuses["failed"] == 0
    assert stress.state["published"] == []


def test_unknown_publish_outcomes_are_terminal_under_load(workspace, stress, monkeypatch):
    assert _set_mode(workspace, "approve_all").status_code == 200
    comments = [f"unknown {i}" for i in range(15)]
    unknowns = set(comments)

    async def insert(_credentials, parent_comment_id, text):
        if parent_comment_id in unknowns:
            raise YouTubePublishFailure("publish_outcome_unknown", "lost response", 502, "unknown")
        stress.state["published"].append(parent_comment_id)
        return "reply"

    monkeypatch.setattr("app.routers.publication.insert_comment_reply", insert)
    _enqueue(workspace, comments)
    _drain(workspace)

    assert Counter(j.status for j in _jobs(workspace))["unknown"] == 15

    # Draining again changes nothing: unknown is terminal and is never retried.
    _drain(workspace, expedite=True)
    assert Counter(j.status for j in _jobs(workspace))["unknown"] == 15


def test_duplicate_ingestion_cannot_double_publish(workspace, stress):
    assert _set_mode(workspace, "approve_all").status_code == 200
    comments = [f"dup {i}" for i in range(50)]
    first = _enqueue(workspace, comments)
    second = _enqueue(workspace, comments)  # exact duplicates while the first batch is active
    assert len(first["created"]) == 50
    assert second["created"] == [] and second["duplicates"] == 50

    _drain(workspace)

    jobs = _jobs(workspace)
    assert len(jobs) == 50
    assert Counter(j.status for j in jobs)["published"] == 50
    assert len(stress.state["published"]) == 50
    assert len(set(stress.state["published"])) == 50
