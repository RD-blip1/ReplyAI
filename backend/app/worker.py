"""Fast, lease-aware queue worker for batch reply generation and publication.

- Runs ``recover_interrupted`` once at startup (and optionally on a loop).
- Claims jobs, executes them once, writes their terminal/retry state via
  ``app.services.job_pipeline.process_job`` and records processed counts.
- Heartbeats at least every half the lease so another worker cannot steal a long
  step; a lost heartbeat stops the job's next externally visible action.
- ``python -m app.worker --once`` makes it easy to run in a one-shot CI check.
- Every write is guarded by conditional UPDATEs in ``job_queue`` (the database, not
  this process, is the safety boundary). No in-memory tracking of publication or
  YouTube secrets is kept between jobs.
"""

import asyncio
import logging
import os
import sys
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import sessions
from app.services import job_pipeline, job_queue

logger = logging.getLogger(__name__)

DEFAULT_WORKER_ID = f"worker:{uuid.getnode():012x}:{os.getpid()}"


async def _with_session(fn, session: AsyncSession | None = None, **kw):
    if session is not None:
        return await fn(session, **kw)
    # ``sessions()`` returns the session factory, so the factory itself is called.
    async with sessions()() as db:
        return await fn(db, **kw)


async def recover(session: AsyncSession | None = None) -> dict:
    return await _with_session(
        lambda db: job_queue.recover_interrupted(
            db, lease_seconds=settings.job_lease_seconds, max_recoveries=settings.job_max_recoveries
        ),
        session=session,
    )


async def _fail_best_effort(db: AsyncSession, job_id: str, worker_id: str) -> None:
    try:
        await job_queue.finish(db, job_id, worker_id, "failed",
                               error={"code": "worker_unexpected"})
    except Exception as exc:  # noqa: BLE001 - recording a failure must never raise
        logger.warning("event=worker_finish_failed job=%s type=%s", job_id, type(exc).__name__)


async def run_once(worker_id: str, *, session: AsyncSession | None = None) -> dict:
    """Claim and process at most one job. Useful for unit tests and one-shot runs."""
    async def _run(db: AsyncSession) -> dict:
        await job_queue.recover_interrupted(db, lease_seconds=settings.job_lease_seconds,
                                            max_recoveries=settings.job_max_recoveries)
        await job_queue.touch_worker(db, worker_id)
        job = await job_queue.claim_next(
            db, worker_id, lease_seconds=settings.job_lease_seconds,
            max_per_channel=settings.worker_max_jobs_per_channel,
        )
        if job is None:
            return {"status": "idle"}
        # Snapshot: a rollback inside process_job expires the ORM job, and reading an
        # expired attribute outside a greenlet raises MissingGreenlet instead of
        # reporting the failure.
        job_id = job.id
        try:
            outcome = await job_pipeline.process_job(db, job, worker_id)
            await job_queue.count_job_processed(db, worker_id)
            return {"status": outcome, "job_id": job_id}
        except Exception as exc:
            logger.exception("event=worker_run_once_failed worker=%s job=%s type=%s",
                             worker_id, job_id, type(exc).__name__)
            await _fail_best_effort(db, job_id, worker_id)
            return {"status": "failed", "job_id": job_id, "error": str(exc)}

    return await _with_session(_run, session=session)


async def run_loop(*, worker_id: str | None = None, once: bool = False,
                   poll_interval: float | None = None, session: AsyncSession | None = None) -> None:
    worker_id = worker_id or DEFAULT_WORKER_ID
    poll_interval = poll_interval if poll_interval is not None else settings.job_poll_interval_seconds

    async def _loop(db: AsyncSession) -> None:
        await job_queue.recover_interrupted(db, lease_seconds=settings.job_lease_seconds,
                                            max_recoveries=settings.job_max_recoveries)
        await job_queue.touch_worker(db, worker_id)
        if once:
            job = await job_queue.claim_next(
                db, worker_id, lease_seconds=settings.job_lease_seconds,
                max_per_channel=settings.worker_max_jobs_per_channel,
            )
            if job is None:
                return
            job_id = job.id
            try:
                await job_pipeline.process_job(db, job, worker_id)
                await job_queue.count_job_processed(db, worker_id)
            except Exception as exc:
                logger.exception("event=worker_once_failed worker=%s job=%s type=%s",
                                 worker_id, job_id, type(exc).__name__)
                await _fail_best_effort(db, job_id, worker_id)
            return
        while True:
            job = await job_queue.claim_next(
                db, worker_id, lease_seconds=settings.job_lease_seconds,
                max_per_channel=settings.worker_max_jobs_per_channel,
            )
            if job is not None:
                job_id = job.id
                try:
                    await job_pipeline.process_job(db, job, worker_id)
                    await job_queue.count_job_processed(db, worker_id)
                except Exception as exc:
                    logger.exception("event=worker_job_failed worker=%s job=%s type=%s",
                                     worker_id, job_id, type(exc).__name__)
                    await _fail_best_effort(db, job_id, worker_id)
            else:
                await job_queue.touch_worker(db, worker_id)
                await asyncio.sleep(poll_interval)

    await _with_session(_loop, session=session)


def _parse_args(argv: list[str]) -> tuple[bool, float | None, str | None]:
    once = False
    poll = None
    worker_id = None
    i = 0
    while i < len(argv):
        if argv[i] in ("--once", "-1"):
            once = True
        elif argv[i] in ("--poll-interval", "--interval"):
            try:
                poll = float(argv[i + 1])
                i += 1
            except (ValueError, IndexError):
                pass
        elif argv[i] in ("--worker-id", "--id"):
            try:
                worker_id = argv[i + 1]
                i += 1
            except IndexError:
                pass
        i += 1
    return once, poll, worker_id


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    once, poll, worker_id = _parse_args(sys.argv[1:])
    asyncio.run(run_loop(once=once, poll_interval=poll, worker_id=worker_id))


if __name__ == "__main__":
    main()
