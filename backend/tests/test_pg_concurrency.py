"""Phase 4B: real PostgreSQL concurrency, multi-process, and crash-recovery checks.

These tests do not run under the normal SQLite harness. They create a disposable
PostgreSQL database, run the real Alembic migration against it, and then release
multiple OS processes at once so the conditional UPDATEs in ``app.services.job_queue``
are genuinely contended. Every process prints READY on stdout; the parent waits for all
of them before releasing, so the race is real rather than sequential.

External providers are mocked or not touched at all. Nothing is written to the
development database: the harness refuses to start unless it can create and drop its own
database on the same server.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app import database
from app.config import settings
from app.models import (
    ChannelOwner,
    PublishedReplyGuard,
    PublishingSettings,
    RateLimitCounter,
    ReplyDraft,
    ReplyJob,
    ReplyPublication,
)
from app.models.session import utc_now
from app.services import job_queue

BACKEND = Path(__file__).resolve().parent.parent
ADMIN_URL = (
    "postgresql+asyncpg://replyai:replyai_dev_only@127.0.0.1:5432/postgres"
)
PROBE = str(Path(__file__).resolve().parent / "_pg_probe.py")

pytestmark = pytest.mark.skipif(
    os.environ.get("REPLYAI_PG_TESTS") == "0",
    reason="PostgreSQL concurrency tests disabled (REPLYAI_PG_TESTS=0)",
)


def _server_reachable() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 5432), timeout=1.5):
            return True
    except OSError:
        return False


async def _create_database() -> str:
    engine = create_async_engine(ADMIN_URL, poolclass=NullPool)
    name = f"replyai_phase4b_{uuid.uuid4().hex[:8]}"
    async with engine.connect() as raw:
        conn = await raw.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text(f"CREATE DATABASE {name}"))
    await engine.dispose()
    return name


async def _drop_database(name: str) -> None:
    engine = create_async_engine(ADMIN_URL, poolclass=NullPool)
    async with engine.connect() as raw:
        conn = await raw.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = :name AND pid <> pg_backend_pid()"
        ), {"name": name})
        await conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
    await engine.dispose()


@pytest.fixture(scope="module")
def pg_url() -> Iterator[str]:
    """A disposable database with the real migration applied, dropped afterwards."""
    if not _server_reachable():
        pytest.skip("PostgreSQL is not reachable on 127.0.0.1:5432")

    name = asyncio.run(_create_database())
    url = f"postgresql+asyncpg://replyai:replyai_dev_only@127.0.0.1:5432/{name}"
    env = dict(
        os.environ,
        DATABASE_URL=url,
        REPLYAI_TESTING="1",
        REPLYAI_TEST_DATABASE_URL=url,
    )
    env.pop("PYTEST_CURRENT_TEST", None)
    # The migration runs in a subprocess so it uses the same guard the app does.
    result = subprocess.run(
        [str(BACKEND / ".venv" / "bin" / "python"), "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND, env=env, capture_output=True, text=True, timeout=180, check=False,
    )
    if result.returncode != 0:
        asyncio.run(_drop_database(name))
        pytest.skip(f"alembic upgrade failed: {result.stderr[-800:]}")
    try:
        yield url
    finally:
        asyncio.run(_drop_database(name))


@pytest.fixture
def pg(pg_url, monkeypatch) -> Iterator:
    """Point the application at the disposable database for one test."""
    monkeypatch.setattr(settings, "database_url", SecretStr(pg_url))
    monkeypatch.setenv(database.TEST_DATABASE_URL_ENV, pg_url)
    monkeypatch.setenv(database.TEST_RUN_ENV, "1")
    engine = create_async_engine(pg_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    database.use_test_database(engine, sessions)

    async def clean() -> None:
        async with engine.begin() as conn:
            for table in reversed(database_base_tables()):
                await conn.execute(text(f"TRUNCATE TABLE {table} CASCADE"))

    asyncio.run(clean())
    try:
        yield SimpleNamespace(db=sessions, url=pg_url)
    finally:
        database.reset_test_database()
        asyncio.run(engine.dispose())


def database_base_tables() -> list[str]:
    from app.models import Base

    return [table.name for table in reversed(Base.metadata.sorted_tables)]


# --- process helpers ---------------------------------------------------------


def _child_env(sessions_url: str) -> dict[str, str]:
    env = dict(
        os.environ,
        DATABASE_URL=sessions_url,
        REPLYAI_TESTING="1",
        REPLYAI_TEST_DATABASE_URL=sessions_url,
    )
    env.pop("PYTEST_CURRENT_TEST", None)
    return env


def release_children(procs: list[subprocess.Popen], sessions_url: str) -> list[dict]:
    """Wait for every child to reach the barrier, then release them all at once."""
    ready = 0
    for proc in procs:
        line = proc.stdout.readline()
        assert line.strip() == "READY", (line, proc.stderr.read() if proc.stderr else "")
        ready += 1
    assert ready == len(procs)
    for proc in procs:
        proc.stdin.write("\n")
        proc.stdin.flush()
    results = []
    for proc in procs:
        out, err = proc.communicate(timeout=60)
        line = (out or "").strip().splitlines()[-1] if (out or "").strip() else ""
        results.append({"rc": proc.returncode, "json": line, "err": err[-600:]})
    return results


def spawn(specs: list[list[str]], sessions_url: str) -> list[subprocess.Popen]:
    return [
        subprocess.Popen(
            [sys.executable, PROBE, *spec],
            cwd=BACKEND, env=_child_env(sessions_url),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        for spec in specs
    ]


def parsed(results: list[dict]) -> list[dict]:
    return [json.loads(r["json"]) if r["json"] else {} for r in results]


# --- fixtures that seed data -------------------------------------------------


def seed_owner(pg) -> str:
    async def _seed() -> str:
        async with pg.db() as db:
            owner = ChannelOwner(channel_id=f"channel-{uuid.uuid4().hex[:6]}")
            db.add(owner)
            await db.commit()
            return owner.id

    return asyncio.run(_seed())


def seed_job(pg, owner_id: str, comment_id: str = "comment-1", **kw) -> str:
    async def _seed() -> str:
        async with pg.db() as db:
            owner = await db.get(ChannelOwner, owner_id)
            result = await job_queue.enqueue(
                db, owner, video_id="videoid123", comment_ids=[comment_id], **kw
            )
            return result["created"][0]

    return asyncio.run(_seed())


# --- tests -------------------------------------------------------------------


def test_concurrent_enqueue_creates_exactly_one_job(pg):
    owner_id = seed_owner(pg)
    procs = spawn(
        [["enqueue", owner_id, "videoid123", "c-dup,c-dup,c-dup"] for _ in range(4)],
        pg.url,
    )
    results = parsed(release_children(procs, pg.url))
    assert len(results) == 4
    total_created = sum(r.get("created", 0) for r in results)
    total_dup = sum(r.get("duplicates", 0) for r in results)
    assert total_created == 1, results
    assert total_dup == 3, results

    async def count():
        async with pg.db() as db:
            return await db.scalar(
                select(ReplyJob).where(ReplyJob.comment_id == "c-dup")
            )

    job = asyncio.run(count())
    assert job is not None


def test_two_workers_cannot_claim_the_same_job(pg):
    owner_id = seed_owner(pg)
    job_id = seed_job(pg, owner_id, "c-claim")
    url = pg.url
    procs = spawn([["claim", f"w-{i}"] for i in range(4)], url)
    results = parsed(release_children(procs, url))
    claimed = [r for r in results if r.get("job")]
    assert len(claimed) == 1, results
    assert claimed[0]["job"] == job_id

    async def read():
        async with pg.db() as db:
            job = await db.get(ReplyJob, job_id)
            return job.status, job.worker_id, job.attempts

    status, worker, attempts = asyncio.run(read())
    assert status == "processing"
    assert worker and worker.startswith("w-")
    assert attempts == 1


def test_lease_loss_makes_a_claimed_job_reclaimable(pg):
    owner_id = seed_owner(pg)
    job_id = seed_job(pg, owner_id, "c-lease")

    async def expire_lease():
        async with pg.db() as db:
            job = await db.get(ReplyJob, job_id)
            job.lease_expires_at = utc_now().replace(year=2000)
            await db.commit()

    asyncio.run(expire_lease())

    async def claim_first():
        async with pg.db() as db:
            job = await job_queue.claim_next(db, "dead-worker")
            return job is not None

    assert asyncio.run(claim_first())
    asyncio.run(expire_lease())

    async def run_recover():
        async with pg.db() as db:
            return await job_queue.recover_interrupted(db, lease_seconds=1, max_recoveries=3)

    res = asyncio.run(run_recover())
    assert res["recovered"] == 1

    async def read():
        async with pg.db() as db:
            job = await db.get(ReplyJob, job_id)
            return job.status, job.recoveries

    status, recoveries = asyncio.run(read())
    assert status == "pending"
    assert recoveries == 1


def test_crash_with_open_sending_attempt_recovers_as_unknown_not_retried(pg):
    owner_id = seed_owner(pg)
    job_id = seed_job(pg, owner_id, "c-unknown")

    async def seed_attempt():
        async with pg.db() as db:
            draft = ReplyDraft(
                channel_owner_id=owner_id, channel_id="chan",
                video_id="videoid123", comment_id="c-unknown",
                original_comment="hello", revision=1,
            )
            db.add(draft)
            await db.flush()
            db.add(ReplyPublication(
                draft_id=draft.id, operation_id=uuid.uuid4().hex,
                channel_id="chan", video_id="videoid123",
                parent_comment_id="c-unknown", draft_revision=1,
                text="hi", status="sending",
            ))
            await db.commit()
            job = await db.get(ReplyJob, job_id)
            job.draft_id = draft.id
            job.lease_expires_at = utc_now().replace(year=2000)
            job.status = "publish_claimed"
            await db.commit()

    asyncio.run(seed_attempt())

    async def run_recover():
        async with pg.db() as db:
            return await job_queue.recover_interrupted(db, lease_seconds=1, max_recoveries=3)

    res = asyncio.run(run_recover())
    assert res["marked_unknown"] == 1

    async def read():
        async with pg.db() as db:
            job = await db.get(ReplyJob, job_id)
            return job.status, job.last_error

    status, last_error = asyncio.run(read())
    assert status == "unknown"
    assert last_error.get("code") == "publish_outcome_unknown"


def test_crash_after_budget_reserve_drops_the_token(pg):
    owner_id = seed_owner(pg)
    url = pg.url

    async def seed_settings():
        async with pg.db() as db:
            db.add(PublishingSettings(
                channel_owner_id=owner_id, mode="approve_all", revision=1,
                auto_publish_count=0, auto_publish_window_started_at=utc_now(),
            ))
            await db.commit()

    asyncio.run(seed_settings())
    procs = spawn([["reserve_crash", owner_id, "approve_all", "1"]], url)
    results = release_children(procs, url)
    # The child exited (code 17) before committing, so nothing was reserved.
    assert results[0]["rc"] == 17, results

    async def check():
        async with pg.db() as db:
            setting = await db.get(PublishingSettings, owner_id)
            counter = await db.scalar(
                select(RateLimitCounter).where(
                    RateLimitCounter.key == f"auto_publish:day:{owner_id}"
                )
            )
            return (setting.auto_publish_count if setting else None,
                    counter.day_count if counter else None)

    hour_count, day_count = asyncio.run(check())
    assert hour_count == 0, "hourly budget was consumed by a crashed worker"
    assert day_count in (None, 0), "daily token survived a crash that never committed"


def test_final_daily_budget_is_taken_exactly_once(pg):
    owner_id = seed_owner(pg)
    url = pg.url
    key = f"budget:{owner_id}"

    async def acquire_all(n: int) -> int:
        async with pg.db() as db:
            taken = 0
            for _ in range(n):
                if await job_queue.try_acquire(db, key, 0, 1):
                    taken += 1
            return taken

    assert asyncio.run(acquire_all(1)) == 1
    assert asyncio.run(acquire_all(1)) == 0

    procs = spawn([["acquire", key, "0", "1"] for _ in range(6)], url)
    results = parsed(release_children(procs, url))
    assert all(r.get("ok") is False for r in results), results


def test_concurrent_hourly_budget_reservations(pg):
    owner_id = seed_owner(pg)
    url = pg.url

    async def seed_settings():
        async with pg.db() as db:
            db.add(PublishingSettings(
                channel_owner_id=owner_id, mode="approve_all", revision=1,
                auto_publish_count=settings.auto_publish_max_per_hour - 1,
                auto_publish_window_started_at=utc_now(),
            ))
            await db.commit()

    asyncio.run(seed_settings())
    procs = spawn(
        [["reserve", owner_id, "approve_all", "1"] for _ in range(4)], url
    )
    results = parsed(release_children(procs, url))
    assert sum(1 for r in results if r.get("ok")) == 1, results

    async def check():
        async with pg.db() as db:
            setting = await db.get(PublishingSettings, owner_id)
            return setting.auto_publish_count

    # Exactly one of the four children got the last hourly slot; the three losers
    # failed closed without consuming it.
    assert asyncio.run(check()) == settings.auto_publish_max_per_hour


def test_cancel_is_refused_while_the_job_is_processing(pg):
    owner_id = seed_owner(pg)
    job_id = seed_job(pg, owner_id, "c-cancel")

    async def claim_then_cancel():
        async with pg.db() as db:
            job = await job_queue.claim_next(db, "worker-cancel")
            assert job is not None and job.id == job_id
            cancelled = await job_queue.cancel(db, owner_id, job_id)
            return cancelled

    assert asyncio.run(claim_then_cancel()) is None

    async def read():
        async with pg.db() as db:
            job = await db.get(ReplyJob, job_id)
            return job.status

    assert asyncio.run(read()) == "processing"


def test_terminal_job_does_not_block_a_new_job_for_the_same_comment(pg):
    owner_id = seed_owner(pg)
    first = seed_job(pg, owner_id, "c-repeat")

    async def finish_first():
        async with pg.db() as db:
            job = await db.get(ReplyJob, first)
            job.status = "published"
            job.finished_at = utc_now()
            await db.commit()

    asyncio.run(finish_first())
    second = seed_job(pg, owner_id, "c-repeat")
    assert second != first

    async def count():
        async with pg.db() as db:
            from sqlalchemy import func

            return await db.scalar(
                select(func.count()).select_from(ReplyJob).where(
                    ReplyJob.comment_id == "c-repeat"
                )
            )

    assert asyncio.run(count()) == 2


def test_duplicate_guard_blocks_a_second_reply_to_the_same_parent(pg):
    owner_id = seed_owner(pg)
    seed_job(pg, owner_id, "c-guard")

    async def reserve_guard():
        async with pg.db() as db:
            draft = ReplyDraft(
                channel_owner_id=owner_id, channel_id="chan",
                video_id="videoid123", comment_id="c-guard",
                original_comment="hello", revision=1,
            )
            db.add(draft)
            await db.flush()
            db.add(PublishedReplyGuard(
                channel_id="chan", parent_comment_id="c-guard",
                status="posted", draft_id=draft.id,
            ))
            await db.commit()
            return draft.id

    draft_id = asyncio.run(reserve_guard())

    async def second_guard():
        from sqlalchemy.exc import IntegrityError

        async with pg.db() as db:
            try:
                async with db.begin_nested():
                    db.add(PublishedReplyGuard(
                        channel_id="chan", parent_comment_id="c-guard",
                        status="sending", draft_id=draft_id,
                    ))
                    await db.flush()
                return "inserted"
            except IntegrityError:
                return "refused"

    assert asyncio.run(second_guard()) == "refused"


def test_two_real_worker_processes_run_one_job_exactly_once(pg, pg_url):
    """Two real `python -m app.worker --once` processes race on a single job.

    The channel has no YouTube connection, so neither process needs a model or a
    YouTube call: the pipeline stops at its first safety check. What is being
    measured is that exactly one process claims the job and the other idles.
    """
    owner = ChannelOwner(channel_id="ghost-channel")
    async def seed():
        async with pg.db() as db:
            db.add(owner)
            await db.commit()
            return owner.id
    owner_id = asyncio.run(seed())
    job_id = seed_job(pg, owner_id, "c-worker-race")

    env = _child_env(pg.url)
    procs = [
        subprocess.Popen(
            [str(BACKEND / ".venv" / "bin" / "python"), "-m", "app.worker", "--once",
             "--worker-id", f"real-worker-{i}"],
            cwd=BACKEND, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        for i in range(2)
    ]
    outs = []
    for proc in procs:
        out, err = proc.communicate(timeout=60)
        outs.append((proc.returncode, out, err))

    async def read():
        async with pg.db() as db:
            job = await db.get(ReplyJob, job_id)
            return job.status, job.attempts, job.last_error

    status, attempts, last_error = asyncio.run(read())
    # Exactly one claim: the claim increments attempts exactly once.
    assert attempts == 1, (outs, attempts)
    assert status == "needs_reauth", (outs, status)
    assert last_error.get("code") == "reconnect_required", outs


def test_crash_before_publication_is_resumed_not_regenerated(pg):
    owner_id = seed_owner(pg)
    job_id = seed_job(pg, owner_id, "c-crash-pre")

    async def seed_verified_crash():
        async with pg.db() as db:
            draft = ReplyDraft(
                channel_owner_id=owner_id, channel_id="chan",
                video_id="videoid123", comment_id="c-crash-pre",
                original_comment="hello", revision=1,
            )
            db.add(draft)
            await db.flush()
            job = await db.get(ReplyJob, job_id)
            job.draft_id = draft.id
            job.status = "verified"
            job.lease_expires_at = utc_now().replace(year=2000)
            job.outcome = {"decision": "models_complete", "draft_revision": 1}
            await db.commit()

    asyncio.run(seed_verified_crash())

    async def run_recover():
        async with pg.db() as db:
            return await job_queue.recover_interrupted(db, lease_seconds=1, max_recoveries=3)

    res = asyncio.run(run_recover())
    assert res == {"recovered": 1, "marked_unknown": 0, "marked_published": 0,
                   "failed": 0}, res

    async def read():
        async with pg.db() as db:
            job = await db.get(ReplyJob, job_id)
            return job.status, job.recoveries, job.outcome

    status, recoveries, outcome = asyncio.run(read())
    assert status == "pending"
    assert recoveries == 1
    # The verification is remembered so the retry resumes at publication.
    assert outcome.get("draft_revision") == 1


def test_crash_after_confirmed_post_is_marked_published(pg):
    owner_id = seed_owner(pg)
    job_id = seed_job(pg, owner_id, "c-crash-post")

    async def seed_posted():
        async with pg.db() as db:
            draft = ReplyDraft(
                channel_owner_id=owner_id, channel_id="chan",
                video_id="videoid123", comment_id="c-crash-post",
                original_comment="hello", revision=1,
            )
            db.add(draft)
            await db.flush()
            db.add(ReplyPublication(
                draft_id=draft.id, operation_id=uuid.uuid4().hex,
                channel_id="chan", video_id="videoid123",
                parent_comment_id="c-crash-post", draft_revision=1,
                text="hi", status="posted", reply_comment_id="yt-reply-1",
                resolved_at=utc_now(),
            ))
            await db.commit()
            job = await db.get(ReplyJob, job_id)
            job.draft_id = draft.id
            job.status = "publish_claimed"
            job.lease_expires_at = utc_now().replace(year=2000)
            await db.commit()

    asyncio.run(seed_posted())

    async def run_recover():
        async with pg.db() as db:
            return await job_queue.recover_interrupted(db, lease_seconds=1, max_recoveries=3)

    res = asyncio.run(run_recover())
    assert res["marked_published"] == 1, res

    async def read():
        async with pg.db() as db:
            job = await db.get(ReplyJob, job_id)
            return job.status, job.outcome

    status, outcome = asyncio.run(read())
    assert status == "published"
    assert outcome["publication"]["reply_comment_id"] == "yt-reply-1"


def test_requeue_of_unknown_job_never_resends(pg):
    """A human may requeue an unknown job, but the worker must not post again."""
    owner_id = seed_owner(pg)
    job_id = seed_job(pg, owner_id, "c-requeue-unknown")

    async def seed_unknown():
        async with pg.db() as db:
            draft = ReplyDraft(
                channel_owner_id=owner_id, channel_id="chan",
                video_id="videoid123", comment_id="c-requeue-unknown",
                original_comment="hello", revision=1,
            )
            db.add(draft)
            await db.flush()
            db.add(ReplyPublication(
                draft_id=draft.id, operation_id=uuid.uuid4().hex,
                channel_id="chan", video_id="videoid123",
                parent_comment_id="c-requeue-unknown", draft_revision=1,
                text="hi", status="unknown",
            ))
            db.add(PublishedReplyGuard(
                channel_id="chan", parent_comment_id="c-requeue-unknown",
                status="unknown", draft_id=draft.id,
            ))
            await db.commit()
            job = await db.get(ReplyJob, job_id)
            job.draft_id = draft.id
            job.status = "unknown"
            job.finished_at = utc_now()
            job.last_error = {"code": "publish_outcome_unknown"}
            job.outcome = {"decision": "unknown_outcome"}
            await db.commit()
            return draft.id

    draft_id = asyncio.run(seed_unknown())

    async def requeue():
        from sqlalchemy import update as sa_update

        async with pg.db() as db:
            result = await db.execute(
                sa_update(ReplyJob).where(ReplyJob.id == job_id)
                .values(status="pending", attempts=0, recoveries=0,
                        next_run_at=utc_now(), last_error={}, outcome={},
                        finished_at=None, worker_id=None, lease_expires_at=None)
                .execution_options(synchronize_session=False)
            )
            await db.commit()
            return result.rowcount

    assert asyncio.run(requeue()) == 1

    async def process_again():
        """Re-run the decision the worker makes: durable attempt state wins."""
        async with pg.db() as db:
            claimed = await job_queue.claim_next(db, "w")
            assert claimed is not None and claimed.id == job_id
            attempt = await db.scalar(
                select(ReplyPublication).where(ReplyPublication.draft_id == draft_id)
            )
            from app.services import publishing
            view = publishing.view(attempt)
            if view.get("status") == "posted":
                await job_queue.finish(db, job_id, "w", "published")
                return "published"
            await job_queue.finish(
                db, job_id, "w", "unknown",
                error={"code": "publish_outcome_unknown"},
                outcome={"decision": "unknown_outcome"},
            )
            return "unknown"

    assert asyncio.run(process_again()) == "unknown"

    async def read():
        async with pg.db() as db:
            job = await db.get(ReplyJob, job_id)
            return job.status

    assert asyncio.run(read()) == "unknown"
