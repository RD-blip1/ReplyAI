"""Offline 100-comment eval harness.

This drives the *real* batch pipeline for all 100 labeled comments in
``fixtures/eval_dataset_100.json`` with a scripted provider, then scores the durable job
outcome against the category's expected routing. It is an eval of the policy/routing
logic, not of model quality: the provider is deterministic, so a mismatch always means
the pipeline routed a category incorrectly. Live model quality is covered separately by
``test_triage_evals.py`` (opt-in, ``RUN_LIVE_EVALS=1``).

The same 100 jobs double as a scale check: enqueue is deduplicated, every job reaches a
terminal state, and no comment gets two active jobs.
"""

import asyncio
import json
from collections import Counter
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.models import ReplyDraft, ReplyJob
from app.services import llm

DATASET = json.loads((Path(__file__).parent / "fixtures" / "eval_dataset_100.json").read_text())
CATEGORY_OF = {row["comment"]: row["category"] for row in DATASET}
assert len(DATASET) == 100 and len(CATEGORY_OF) == 100

# Expected durable outcome (status, decision code) per triage category, per mode.
# "on_topic" is the only category allowed to be written by automation.
EXPECTED = {
    "on_topic": {"manual_review": ("review", "manual_review_required"),
                 "approve_all": ("published", "published")},
    "generic_engagement": ("review", "context_required"),
    "off_topic": ("skipped", "skipped_off_topic"),
    "spam_or_suspicious": ("skipped", "skipped_spam"),
    "harassment_or_abuse": ("skipped", "skipped_harassment"),
    "unsupported_language": ("skipped", "skipped_unsupported_language"),
    "requires_human_attention": ("review", "requires_human"),
}


@pytest.fixture(autouse=True)
def langgraph(monkeypatch):
    monkeypatch.setattr(settings, "draft_workflow_engine", "langgraph")


@pytest.fixture
def eval_pipeline(workspace, monkeypatch):
    # Routing eval, not a budget eval: lift the shared provider budgets so 100 jobs run
    # to terminal states in one drain. Budget parking is covered by the stress suite.
    monkeypatch.setattr(settings, "llm_requests_per_minute", 100_000)
    monkeypatch.setattr(settings, "llm_requests_per_day", 100_000)
    monkeypatch.setattr(settings, "youtube_reads_per_minute", 100_000)
    monkeypatch.setattr(settings, "auto_publish_max_per_hour", 100_000)
    monkeypatch.setattr(settings, "auto_publish_max_per_day", 100_000)

    async def credentials_for_connection(connection, db):
        return object()

    async def owned_comment(connection, credentials, video_id, comment_id):
        # The dataset encodes the comment text as the id, so the authoritative read
        # (mocked here) returns exactly the labeled comment the provider must triage.
        return comment_id

    published = []

    async def insert(_credentials, parent_comment_id, text):
        published.append(parent_comment_id)
        return f"reply-{len(published)}"

    monkeypatch.setattr("app.services.job_pipeline.credentials_for_connection",
                        credentials_for_connection)
    monkeypatch.setattr("app.services.job_pipeline.get_owned_comment", owned_comment)
    monkeypatch.setattr("app.routers.publication._credentials", lambda *a, **k: _async(object()))
    monkeypatch.setattr("app.routers.publication.insert_comment_reply", insert)
    workspace.published = published

    class Scripted(workspace.provider):
        """Triage echoes the dataset label for the comment; everything else is default."""

        async def complete(self, schema, instruction, data):
            if schema is llm.Triage:
                category = CATEGORY_OF.get(data["comment"], "on_topic")
                return schema(category=category, reason="scripted", confidence=1.0)
            return await super().complete(schema, instruction, data)

    monkeypatch.setattr(llm, "provider", Scripted)
    return workspace


async def _async(value):
    return value


def _drain(workspace, worker_id="eval-worker"):
    """Run the worker until the queue is empty; return the number of passes."""
    from app import worker

    passes = 0
    while True:
        async def once():
            async with workspace.sessions() as db:
                return await worker.run_once(worker_id, session=db)
        result = asyncio.run(once())
        if result["status"] == "idle":
            return passes
        passes += 1


def _all_jobs(workspace):
    async def main():
        async with workspace.sessions() as db:
            return (await db.execute(select(ReplyJob))).scalars().all()
    jobs = asyncio.run(main())
    return [(j.comment_id, j.status, (j.outcome or {}).get("decision")) for j in jobs]


def _set_mode(workspace, mode):
    current = workspace.client.get("/api/publishing-settings").json()
    return workspace.client.put(
        "/api/publishing-settings", headers=workspace.headers,
        json={"mode": mode, "revision": current["revision"], "confirm_automation": mode != "manual_review"},
    )


def _enqueue_all(workspace):
    # One batch per comment keeps comment_id == the dataset comment text, so the
    # scripted provider can map a triage call straight back to its label.
    comments = [row["comment"] for row in DATASET]
    response = workspace.client.post(
        "/api/batch/jobs/enqueue", headers=workspace.headers,
        json={"video_id": "abcdefghijk", "comment_ids": comments},
    )
    assert response.status_code == 202, response.text
    return response.json()


def test_eval_100_routes_every_category_correctly_in_approve_all(workspace, eval_pipeline):
    assert _set_mode(workspace, "approve_all").status_code == 200
    enqueued = _enqueue_all(workspace)
    assert len(enqueued["created"]) == 100
    assert enqueued["duplicates"] == 0

    passes = _drain(workspace)
    assert passes == 100  # one claimed job per pass, each currently terminal

    results = _all_jobs(workspace)
    assert len(results) == 100
    mismatches = []
    for comment_id, status, decision in results:
        category = CATEGORY_OF[comment_id]
        expected = EXPECTED[category]["approve_all"] if category == "on_topic" else EXPECTED[category]
        if (status, decision) != expected:
            mismatches.append((category, comment_id, status, decision, expected))
    assert not mismatches, mismatches

    # Automation wrote exactly the on-topic comments, and nothing else.
    assert len(eval_pipeline.published) == sum(
        1 for row in DATASET if row["category"] == "on_topic"
    )


def test_eval_100_in_manual_review_never_publishes(workspace, eval_pipeline):
    assert _set_mode(workspace, "manual_review").status_code == 200
    _enqueue_all(workspace)
    _drain(workspace)

    results = _all_jobs(workspace)
    assert len(results) == 100
    assert eval_pipeline.published == []

    counts = Counter((status, decision) for _, status, decision in results)
    # Every on-topic comment produced a complete draft awaiting a human.
    assert counts[("review", "manual_review_required")] == 30

    async def drafts():
        async with workspace.sessions() as db:
            total = await db.scalar(select(func.count()).select_from(ReplyDraft))
            with_text = await db.scalar(
                select(func.count()).select_from(ReplyDraft)
                .where(func.coalesce(func.length(ReplyDraft.text), 0) > 0)
            )
            return total, with_text
    total, with_text = asyncio.run(drafts())
    assert total == 100  # every comment snapshot was stored
    # Both generatable categories produce text (on_topic + generic_engagement = 45);
    # every other category stops before generation.
    assert with_text == 45


def test_eval_100_dedupes_repeated_enqueue(workspace, eval_pipeline):
    _enqueue_all(workspace)
    second = _enqueue_all(workspace)
    assert second["created"] == []
    assert second["duplicates"] == 100

    async def active():
        async with workspace.sessions() as db:
            return await db.scalar(select(func.count()).select_from(ReplyJob))
    assert asyncio.run(active()) == 100
