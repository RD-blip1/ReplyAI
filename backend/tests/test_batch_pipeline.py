"""End-to-end batch pipeline: enqueue → worker → generation → policy → publication.

Everything runs against the temporary test database with mocked providers and a mocked
YouTube write, so no live model or channel is touched. The publication path under test is
the real guarded one (``publish_generated_draft`` → ``publish_saved_draft``), only its
credential and insert leaves are replaced.
"""

import asyncio

import pytest
from sqlalchemy import select
from support import QUALITY

from app.config import settings
from app.models import (
    ChannelOwner,
    PublishedReplyGuard,
    PublishingSettings,
    ReplyDraft,
    ReplyJob,
    ReplyPublication,
)
from app.services import llm
from app.services.youtube_api import YouTubePublishFailure


@pytest.fixture(autouse=True)
def langgraph(monkeypatch):
    # Triage must run for automation; the batch pipeline is engine-independent, but the
    # pipeline itself is what these tests exercise.
    monkeypatch.setattr(settings, "draft_workflow_engine", "langgraph")


@pytest.fixture
def pipeline(workspace, monkeypatch):
    """Mock the worker's credential resolution and the publication insert."""
    published = []

    async def credentials_for_connection(connection, db):
        return object()

    async def insert(_credentials, parent_comment_id, text):
        published.append((parent_comment_id, text))
        return "reply-id"

    monkeypatch.setattr("app.services.job_pipeline.credentials_for_connection",
                        credentials_for_connection)
    monkeypatch.setattr("app.routers.publication._credentials", lambda *a, **k: _async(object()))
    monkeypatch.setattr("app.routers.publication.insert_comment_reply", insert)
    workspace.published = published

    def fail_insert(outcome, code="publish_rejected"):
        async def insert(_credentials, parent_comment_id, text):
            raise YouTubePublishFailure(code, "provider refused", 422, outcome)
        monkeypatch.setattr("app.routers.publication.insert_comment_reply", insert)

    workspace.fail_insert = fail_insert
    return workspace


async def _async(value):
    return value


def run(coro):
    return asyncio.run(coro)


def enqueue(workspace, comment_ids, **overrides):
    body = {"video_id": "abcdefghijk", "comment_ids": comment_ids, **overrides}
    response = workspace.client.post("/api/batch/jobs/enqueue", headers=workspace.headers, json=body)
    assert response.status_code == 202, response.text
    return response.json()


def set_mode(workspace, mode):
    current = workspace.client.get("/api/publishing-settings").json()
    return workspace.client.put(
        "/api/publishing-settings", headers=workspace.headers,
        json={"mode": mode, "revision": current["revision"], "confirm_automation": mode != "manual_review"},
    )


def run_worker(workspace, worker_id="test-worker"):
    async def main():
        async with workspace.sessions() as db:
            from app import worker
            return await worker.run_once(worker_id, session=db)
    return run(main())


def read_job(workspace, job_id):
    async def main():
        async with workspace.sessions() as db:
            return await db.get(ReplyJob, job_id)
    return run(main())


def test_manual_review_generates_and_waits_for_a_human(workspace, pipeline):
    enqueued = enqueue(workspace, ["c1"])
    assert run_worker(workspace)["status"] == "review"
    job = read_job(workspace, enqueued["created"][0])
    assert job.status == "review"
    assert job.outcome["decision"] == "manual_review_required"

    async def draft():
        async with workspace.sessions() as db:
            return await db.scalar(select(ReplyDraft).where(ReplyDraft.comment_id == "c1"))
    saved = run(draft())
    assert saved.text == "Thanks for watching!"
    assert saved.triage["category"] == "on_topic"
    assert pipeline.published == []


def test_auto_mode_publishes_through_the_guarded_path(workspace, pipeline):
    assert set_mode(workspace, "approve_all").status_code == 200
    enqueued = enqueue(workspace, ["c1"])
    assert run_worker(workspace)["status"] == "published"
    job = read_job(workspace, enqueued["created"][0])
    assert job.status == "published"
    assert job.outcome["decision"] == "published"
    assert pipeline.published == [("c1", "Thanks for watching!")]

    async def rows():
        async with workspace.sessions() as db:
            publication = await db.scalar(select(ReplyPublication))
            guard = await db.scalar(select(PublishedReplyGuard))
            return publication, guard
    publication, guard = run(rows())
    assert publication.status == "posted" and publication.reply_comment_id == "reply-id"
    assert guard is not None


def test_duplicate_enqueue_creates_one_job(workspace, pipeline):
    first = enqueue(workspace, ["c1"])
    second = enqueue(workspace, ["c1"])
    assert len(first["created"]) == 1
    assert second["created"] == [] and second["duplicates"] == 1


def test_off_topic_comment_is_skipped_without_generation(workspace, pipeline, monkeypatch):
    class OffTopic(pipeline.provider):
        async def complete(self, schema, instruction, data):
            if schema is llm.Triage:
                return schema(category="off_topic", reason="unrelated", confidence=1.0)
            return await super().complete(schema, instruction, data)
    monkeypatch.setattr(llm, "provider", OffTopic)

    enqueued = enqueue(workspace, ["c1"])
    assert run_worker(workspace)["status"] == "skipped"
    job = read_job(workspace, enqueued["created"][0])
    assert job.status == "skipped"
    assert job.outcome["decision"] == "skipped_off_topic"

    async def draft():
        async with workspace.sessions() as db:
            return await db.scalar(select(ReplyDraft).where(ReplyDraft.comment_id == "c1"))
    saved = run(draft())
    assert not (saved.text or "").strip()
    assert pipeline.published == []


def test_generic_engagement_goes_to_review(workspace, pipeline, monkeypatch):
    class Generic(pipeline.provider):
        async def complete(self, schema, instruction, data):
            if schema is llm.Triage:
                return schema(category="generic_engagement", reason="generic", confidence=1.0)
            return await super().complete(schema, instruction, data)
    monkeypatch.setattr(llm, "provider", Generic)

    enqueued = enqueue(workspace, ["c1"])
    assert run_worker(workspace)["status"] == "review"
    job = read_job(workspace, enqueued["created"][0])
    assert job.outcome["decision"] == "context_required"
    assert pipeline.published == []


def test_rejected_publication_does_not_post_and_needs_review(workspace, pipeline):
    assert set_mode(workspace, "approve_all").status_code == 200
    pipeline.fail_insert("rejected", code="parent_comment_unavailable")

    enqueued = enqueue(workspace, ["c1"])
    assert run_worker(workspace)["status"] == "review"
    job = read_job(workspace, enqueued["created"][0])
    assert job.status == "review"

    async def publication():
        async with workspace.sessions() as db:
            return await db.scalar(select(ReplyPublication))
    attempt = run(publication())
    assert attempt.status == "failed"  # definitively nothing was posted


def test_unknown_publication_outcome_is_never_retried(workspace, pipeline):
    assert set_mode(workspace, "approve_all").status_code == 200
    pipeline.fail_insert("unknown", code="publish_outcome_unknown")

    enqueued = enqueue(workspace, ["c1"])
    assert run_worker(workspace)["status"] == "unknown"
    job = read_job(workspace, enqueued["created"][0])
    assert job.status == "unknown"

    async def publication():
        async with workspace.sessions() as db:
            return await db.scalar(select(ReplyPublication))
    attempt = run(publication())
    assert attempt.status == "unknown"

    # Re-running the worker does not retry an unknown outcome; it settles as unknown.
    assert run_worker(workspace)["status"] == "idle"


def test_revoked_connection_marks_needs_reauth(workspace, pipeline, monkeypatch):
    from app.models import YouTubeConnection

    async def disconnect():
        async with workspace.sessions() as db:
            connection = await db.scalar(select(YouTubeConnection))
            connection.reconnect_required = True
            await db.commit()
    run(disconnect())

    enqueued = enqueue(workspace, ["c1"])
    assert run_worker(workspace)["status"] == "needs_reauth"
    job = read_job(workspace, enqueued["created"][0])
    assert job.status == "needs_reauth"
    assert pipeline.published == []


def test_existing_saved_draft_is_not_overwritten(workspace, pipeline):
    generated = workspace.client.post(
        "/api/drafts", headers=workspace.headers,
        json={"video_id": "abcdefghijk", "comment_id": "c1"},
    )
    assert generated.status_code == 200, generated.text
    original_text = generated.json()["text"]

    enqueued = enqueue(workspace, ["c1"])
    assert run_worker(workspace)["status"] == "review"
    job = read_job(workspace, enqueued["created"][0])
    assert job.outcome["decision"] == "draft_exists"

    async def draft():
        async with workspace.sessions() as db:
            return await db.scalar(select(ReplyDraft).where(ReplyDraft.comment_id == "c1"))
    saved = run(draft())
    assert saved.text == original_text
    assert pipeline.published == []


def test_llm_daily_budget_parks_the_second_job(workspace, pipeline, monkeypatch):
    monkeypatch.setattr(settings, "llm_requests_per_day", 1)
    enqueued = enqueue(workspace, ["c1", "c2"])
    assert len(enqueued["created"]) == 2
    # First job consumes the single token for the secondary role; the second waits.
    assert run_worker(workspace)["status"] in ("review", "published", "skipped")
    second = run_worker(workspace)
    assert second["status"] == "retry"
    job = read_job(workspace, enqueued["created"][1])
    assert job.status == "retry"
    assert job.outcome == {"waiting_for": "provider_budget"}
    assert job.attempts == 0  # environmental denial refunds the attempt


def test_worker_is_idle_with_no_jobs(workspace, pipeline):
    assert run_worker(workspace)["status"] == "idle"


def test_cancel_waiting_job_via_api(workspace, pipeline):
    enqueued = enqueue(workspace, ["c1"])
    job_id = enqueued["created"][0]
    response = workspace.client.post(
        f"/api/batch/jobs/{job_id}/cancel", headers=workspace.headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled"
    assert run_worker(workspace)["status"] == "idle"


def test_stats_summary_reports_counts(workspace, pipeline):
    enqueue(workspace, ["c1", "c2"])
    response = workspace.client.get("/api/batch/jobs/stats/summary", headers=workspace.headers)
    assert response.status_code == 200
    assert response.json()["counts"]["pending"] == 2


def test_job_listing_is_channel_scoped(workspace, pipeline):
    enqueue(workspace, ["c1"])
    response = workspace.client.get("/api/batch/jobs", headers=workspace.headers)
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["comment_id"] == "c1"


def test_requeue_requires_a_finished_job(workspace, pipeline):
    enqueued = enqueue(workspace, ["c1"])
    job_id = enqueued["created"][0]
    too_early = workspace.client.post(
        f"/api/batch/jobs/{job_id}/requeue", headers=workspace.headers
    )
    assert too_early.status_code == 409
    # A finished (review) job can be requeued. A published one deliberately cannot be:
    # re-publishing is not something a requeue should invite.
    assert run_worker(workspace)["status"] == "review"
    requeued = workspace.client.post(
        f"/api/batch/jobs/{job_id}/requeue", headers=workspace.headers
    )
    assert requeued.status_code == 200
    assert requeued.json()["status"] == "pending"
    assert requeued.json()["attempts"] == 0


def test_quality_concern_blocks_automatic_publication(workspace, pipeline, monkeypatch):
    class Concerned(pipeline.provider):
        async def complete(self, schema, instruction, data):
            if schema is llm.Quality:
                return schema({**QUALITY, "confidence": 0.2, "findings": ["possible claim"]})
            return await super().complete(schema, instruction, data)
    monkeypatch.setattr(llm, "provider", Concerned)

    assert set_mode(workspace, "approve_all").status_code == 200
    enqueued = enqueue(workspace, ["c1"])
    assert run_worker(workspace)["status"] == "review"
    job = read_job(workspace, enqueued["created"][0])
    assert job.status == "review"
    assert pipeline.published == []


def test_settings_used_by_worker_exist():
    # Guard against silent config drift the worker depends on.
    assert settings.job_lease_seconds > 0
    assert settings.llm_requests_per_minute > 0
    assert settings.auto_publish_max_per_day > 0
    assert isinstance(PublishingSettings, type)
    assert ChannelOwner is not None
