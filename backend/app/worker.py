"""Fast, lease-aware queue worker for batch reply generation and publication.

- Runs ``recover_interrupted`` once at startup (and optionally on a loop).
- Claims jobs, executes them once, writes their terminal/retry state via
  ``app.services.job_pipeline.process_job`` and records processed counts.
- Checkpoint heartbeats renew ownership between model stages. A lost heartbeat
  stops spending before the next stage. Single-stage overruns and host pauses remain
  limitations; startup enforces stage timeout + 15 seconds < lease duration.
- ``python -m app.worker --once`` processes one job (or one discovery pass) and exits.
- Every write is guarded by conditional UPDATEs in ``job_queue`` (the database, not
  this process, is the safety boundary). No in-memory tracking of publication or
  YouTube secrets is kept between jobs.
"""

import argparse
import asyncio
import logging
import os
import sys
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import sessions
from app.services import discovery, job_pipeline, job_queue

logger = logging.getLogger(__name__)

DEFAULT_WORKER_ID = f"worker:{uuid.getnode():012x}:{os.getpid()}"


def validate_lease_configuration():
    if settings.llm_stage_timeout_seconds + 15 >= settings.job_lease_seconds:
        logger.error("event=invalid_lease_configuration")
        raise ValueError("LLM stage timeout + 15 seconds must be less than job lease")


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


async def _prepare(db: AsyncSession, worker_id: str) -> None:
    await job_queue.recover_interrupted(db, lease_seconds=settings.job_lease_seconds,
                                        max_recoveries=settings.job_max_recoveries)
    await job_queue.touch_worker(db, worker_id)


async def _process_next(db: AsyncSession, worker_id: str) -> dict | None:
    """Claim and process one job; None when the queue has nothing ready.

    A failure inside one job is recorded on that job and never stops the worker.
    """
    job = await job_queue.claim_next(
        db, worker_id, lease_seconds=settings.job_lease_seconds,
        max_per_channel=settings.worker_max_jobs_per_channel,
    )
    if job is None:
        return None
    # Snapshot: a rollback inside process_job expires the ORM job, and reading an
    # expired attribute outside a greenlet raises MissingGreenlet instead of
    # reporting the failure.
    job_id = job.id
    try:
        outcome = await job_pipeline.process_job(db, job, worker_id)
        await job_queue.count_job_processed(db, worker_id)
        return {"status": outcome, "job_id": job_id}
    except Exception as exc:
        logger.exception("event=worker_job_failed worker=%s job=%s type=%s",
                         worker_id, job_id, type(exc).__name__)
        await _fail_best_effort(db, job_id, worker_id)
        return {"status": "failed", "job_id": job_id, "error": str(exc)}


async def run_once(worker_id: str, *, session: AsyncSession | None = None) -> dict:
    """Claim and process at most one job; run discovery instead when the queue is idle."""
    validate_lease_configuration()

    async def _run(db: AsyncSession) -> dict:
        await _prepare(db, worker_id)
        result = await _process_next(db, worker_id)
        if result is None:
            await discovery.run_due_once(sessions())
            return {"status": "idle"}
        return result

    return await _with_session(_run, session=session)


async def run_loop(*, worker_id: str | None = None, once: bool = False,
                   poll_interval: float | None = None, session: AsyncSession | None = None) -> None:
    validate_lease_configuration()
    worker_id = worker_id or DEFAULT_WORKER_ID
    poll_interval = poll_interval if poll_interval is not None else settings.job_poll_interval_seconds

    async def _loop(db: AsyncSession) -> None:
        await _prepare(db, worker_id)
        while True:
            if await _process_next(db, worker_id) is not None:
                if once:
                    return
                continue
            await discovery.run_due_once(sessions())
            if once:
                return
            await job_queue.touch_worker(db, worker_id)
            await asyncio.sleep(poll_interval)

    await _with_session(_loop, session=session)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m app.worker",
                                     description="ReplyAI queue worker and comment discovery.")
    parser.add_argument("--once", "-1", action="store_true",
                        help="process at most one job (or one discovery pass) and exit")
    parser.add_argument("--poll-interval", "--interval", type=float, default=None,
                        help="seconds to sleep when the queue is idle")
    parser.add_argument("--worker-id", "--id", default=None,
                        help="stable identifier shown on the Jobs page")
    parser.add_argument("--discovery-only", action="store_true",
                        help="run one discovery pass for every due channel and exit")
    return parser.parse_args(argv)


def main() -> None:
    from app import simulation
    if simulation.install():
        logger.warning("event=simulation_mode_enabled youtube=fake models=scripted network=disabled")
    logging.basicConfig(level=logging.INFO)
    args = _parse_args(sys.argv[1:])
    if args.discovery_only:
        asyncio.run(discovery.run_due_once(sessions()))
        return
    asyncio.run(run_loop(once=args.once, poll_interval=args.poll_interval,
                         worker_id=args.worker_id))


if __name__ == "__main__":
    main()
