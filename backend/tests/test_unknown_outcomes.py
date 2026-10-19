"""Phase 4B: audit of `unknown` publication outcomes end to end.

An unknown outcome means "the reply may already be public". The audit checks every
path that could turn one into a second YouTube write:

1. provider says the outcome is unknown,
2. the reply was created but recording it failed,
3. a worker crashed after claiming publication (durable `sending` row),
4. a person explicitly requeues the job,
5. the reconcile endpoint runs after a restart.

In every case the assertion is the same: the job settles as `unknown`, the guard stays
in place, and nothing is inserted a second time.
"""
import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.models import PublishedReplyGuard, ReplyJob, ReplyPublication
from app.services import job_queue
from app.services.youtube_api import YouTubePublishFailure


@pytest.fixture(autouse=True)
def langgraph(monkeypatch):
    monkeypatch.setattr(settings, "draft_workflow_engine", "langgraph")


@pytest.fixture
def pipeline(workspace, monkeypatch):
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
    workspace.insert = insert
    return workspace


async def _async(value):
    return value


def run(coro):
    return asyncio.run(coro)


def enqueue(workspace, comment_ids):
    response = workspace.client.post(
        "/api/batch/jobs/enqueue", headers=workspace.headers,
        json={"video_id": "abcdefghijk", "comment_ids": comment_ids},
    )
    assert response.status_code == 202, response.text
    return response.json()


def set_mode(workspace, mode):
    current = workspace.client.get("/api/publishing-settings").json()
    return workspace.client.put(
        "/api/publishing-settings", headers=workspace.headers,
        json={"mode": mode, "revision": current["revision"],
              "confirm_automation": mode != "manual_review"},
    )


def run_worker(workspace, worker_id="unknown-audit-worker"):
    async def main():
        async with workspace.sessions() as db:
            from app import worker
            return await worker.run_once(worker_id, session=db)
    return run(main())


def rows(workspace, model):
    async def main():
        async with workspace.sessions() as db:
            return list((await db.execute(select(model))).scalars())
    return run(main())


def test_provider_unknown_outcome_is_persisted_and_never_retried(workspace, pipeline, monkeypatch):
    assert set_mode(workspace, "approve_all").status_code == 200

    async def fail_insert(_credentials, parent_comment_id, text):
        raise YouTubePublishFailure("publish_outcome_unknown", "no response", 502, "unknown")
    monkeypatch.setattr("app.routers.publication.insert_comment_reply", fail_insert)

    enqueue(workspace, ["c-unknown"])
    assert run_worker(workspace)["status"] == "unknown"
    job = rows(workspace, ReplyJob)[0]
    assert job.status == "unknown"
    assert job.last_error.get("code") == "publish_outcome_unknown"

    attempt = rows(workspace, ReplyPublication)[0]
    assert attempt.status == "unknown"
    guard = rows(workspace, PublishedReplyGuard)[0]
    assert guard.status == "unknown", "an unresolved guard must stay in place"

    # The worker must not pick the job up again on its own.
    assert run_worker(workspace)["status"] == "idle"
    assert len(pipeline.published) == 0

    # Re-running the pipeline decision still refuses to publish.
    async def again():
        async with workspace.sessions() as db:
            from app.models import ReplyDraft
            from app.services import publishing
            draft = (await db.execute(select(ReplyDraft))).scalars().first()
            open_attempt = await publishing.open_attempt(db, draft.id)
            assert open_attempt is not None, "the durable attempt must remain open"
            assert open_attempt.status == "unknown"
            assert pipeline.published == [], "no second insert is allowed"
            return open_attempt.status
    assert run(again()) == "unknown"


def test_reply_created_but_unrecorded_is_reported_unknown(workspace, pipeline, monkeypatch):
    """The insert succeeded; only the confirmation write failed.

    This is the nastiest case: a real reply exists on YouTube while the database could
    not record it. It must be reported as unknown, never as posted and never as failed.
    """
    assert set_mode(workspace, "approve_all").status_code == 200

    from app.routers import publication as publication_module
    real_settle = publication_module.settle

    async def settle_then_fail(db, pub, status, *, reply_comment_id=None, failure=None):
        # Persist nothing: simulate the database write failing after the reply was
        # created on YouTube.
        raise SQLAlchemyError("connection lost after insert")

    monkeypatch.setattr(publication_module, "settle", settle_then_fail)
    monkeypatch.setattr(publication_module, "insert_comment_reply", pipeline.insert)

    enqueued = enqueue(workspace, ["c-record-fail"])
    result = run_worker(workspace)
    assert result["status"] == "unknown", result

    job = rows(workspace, ReplyJob)[0]
    assert job.status == "unknown"

    # The attempt row stays `sending` (it could not be settled), which every reader
    # normalises to unknown.
    attempt = rows(workspace, ReplyPublication)[0]
    assert attempt.status == "sending"
    from app.services import publishing
    assert publishing.view(attempt)["status"] == "unknown"

    # The insert did happen exactly once.
    assert len(pipeline.published) == 1
    assert enqueued["created"]  # job exists

    # The worker already settled the job `unknown`, so recovery has nothing stale to
    # reconcile: it must leave the terminal job alone rather than reopen or resend it.
    async def recover():
        async with workspace.sessions() as db:
            return await job_queue.recover_interrupted(db, lease_seconds=0, max_recoveries=3)
    res = run(recover())
    assert res == {"recovered": 0, "marked_unknown": 0, "marked_published": 0, "failed": 0}, res
    assert rows(workspace, ReplyJob)[0].status == "unknown"
    assert len(pipeline.published) == 1, "reconciliation must never resend"

    # The same reconciliation for a worker that died before it could settle: the durable
    # `sending` attempt is the only evidence, and it still wins over the job state.
    from datetime import timedelta

    from sqlalchemy import update

    from app.models import ReplyJob as ReplyJobModel
    from app.models.session import utc_now

    async def crash_before_settle():
        async with workspace.sessions() as db:
            await db.execute(
                update(ReplyJobModel)
                .where(ReplyJobModel.id == enqueued["created"][0])
                .values(status="publish_claimed",
                        lease_expires_at=utc_now() - timedelta(seconds=5),
                        worker_id="dead-worker")
                .execution_options(synchronize_session=False)
            )
            await db.commit()
    run(crash_before_settle())

    res = run(recover())
    assert res["marked_unknown"] == 1, res
    assert rows(workspace, ReplyJob)[0].status == "unknown"
    assert len(pipeline.published) == 1, "reconciliation must never resend"

    monkeypatch.setattr(publication_module, "settle", real_settle)


def test_requeue_of_unknown_job_is_explicit_and_still_refuses_to_send(workspace, pipeline, monkeypatch):
    assert set_mode(workspace, "approve_all").status_code == 200

    async def fail_insert(_credentials, parent_comment_id, text):
        raise YouTubePublishFailure("publish_outcome_unknown", "no response", 502, "unknown")
    monkeypatch.setattr("app.routers.publication.insert_comment_reply", fail_insert)

    enqueued = enqueue(workspace, ["c-requeue"])
    assert run_worker(workspace)["status"] == "unknown"
    job_id = enqueued["created"][0]

    # A person explicitly asks for the job to run again (allowed by the API).
    requeue = workspace.client.post(
        f"/api/batch/jobs/{job_id}/requeue", headers=workspace.headers)
    assert requeue.status_code == 200, requeue.text
    assert requeue.json()["status"] == "pending"

    # The worker claims it, sees the open attempt, and settles it as unknown again.
    assert run_worker(workspace)["status"] == "unknown"
    job = rows(workspace, ReplyJob)[0]
    assert job.status == "unknown"
    assert len(pipeline.published) == 0, "a human requeue must not duplicate a reply"

    # The durable guard still blocks a second reply to the same comment.
    guard = rows(workspace, PublishedReplyGuard)[0]
    assert guard.status == "unknown"


def test_failed_outcome_deletes_the_guard_and_allows_a_deliberate_retry(workspace, pipeline, monkeypatch):
    """A definitive refusal posts nothing, so a retry must be allowed."""
    assert set_mode(workspace, "approve_all").status_code == 200

    async def refuse(_credentials, parent_comment_id, text):
        raise YouTubePublishFailure("parent_comment_unavailable", "gone", 404, "rejected")
    monkeypatch.setattr("app.routers.publication.insert_comment_reply", refuse)

    enqueued = enqueue(workspace, ["c-retry-ok"])
    assert run_worker(workspace)["status"] == "review"
    job = rows(workspace, ReplyJob)[0]
    assert job.status == "review"

    attempt = rows(workspace, ReplyPublication)[0]
    assert attempt.status == "failed"
    assert rows(workspace, PublishedReplyGuard) == [], "a refused attempt releases its guard"

    # A requeued job may not overwrite the saved draft: it stops at `draft_exists`.
    requeue = workspace.client.post(
        f"/api/batch/jobs/{enqueued['created'][0]}/requeue", headers=workspace.headers)
    assert requeue.status_code == 200, requeue.text
    monkeypatch.setattr("app.routers.publication.insert_comment_reply", pipeline.insert)
    assert run_worker(workspace)["status"] == "review"
    assert rows(workspace, ReplyJob)[0].outcome["decision"] == "draft_exists"
    assert len(pipeline.published) == 0, "a batch requeue must not re-post by itself"

    # The deliberate retry is the person confirming the saved draft: the released guard
    # lets exactly one reply through, and the new attempt owns the guard again.
    async def draft_row():
        async with workspace.sessions() as db:
            from app.models import ReplyDraft
            return (await db.execute(select(ReplyDraft))).scalars().first()
    draft = run(draft_row())
    approved = workspace.client.post(
        f"/api/drafts/{draft.id}/decision", headers=workspace.headers,
        json={"revision": draft.revision, "decision": "approved"},
    )
    assert approved.status_code == 200, approved.text
    published = workspace.client.post(
        f"/api/drafts/{draft.id}/publication", headers=workspace.headers,
        json={"revision": draft.revision, "text": draft.text},
    )
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "posted"
    assert len(pipeline.published) == 1
    assert rows(workspace, PublishedReplyGuard)[0].status == "posted"


def test_reconcile_endpoint_marks_stale_jobs_from_durable_state(workspace, pipeline, monkeypatch):
    assert set_mode(workspace, "approve_all").status_code == 200
    enqueued = enqueue(workspace, ["c-reconcile"])
    job_id = enqueued["created"][0]

    # Simulate a worker that died right after claiming publication.
    async def die_mid_publish():
        async with workspace.sessions() as db:
            from app.models import ReplyDraft
            job = await db.get(ReplyJob, job_id)
            draft = ReplyDraft(
                channel_owner_id=job.channel_owner_id, channel_id=job.channel_id,
                video_id=job.video_id, comment_id=job.comment_id,
                original_comment="hello", revision=1,
            )
            db.add(draft)
            await db.flush()
            import uuid as _uuid
            db.add(ReplyPublication(
                draft_id=draft.id, operation_id=_uuid.uuid4().hex,
                channel_id=job.channel_id, video_id=job.video_id,
                parent_comment_id=job.comment_id, draft_revision=1,
                text="hi", status="sending",
            ))
            job.draft_id = draft.id
            job.status = "publish_claimed"
            from app.models.session import utc_now
            job.lease_expires_at = utc_now().replace(year=2000)
            await db.commit()
    run(die_mid_publish())

    response = workspace.client.post("/api/batch/jobs/reconcile", headers=workspace.headers)
    assert response.status_code == 200, response.text
    assert response.json()["marked_unknown"] == 1

    job = rows(workspace, ReplyJob)[0]
    assert job.status == "unknown"
    assert job.last_error.get("code") == "publish_outcome_unknown"
    assert len(pipeline.published) == 0
