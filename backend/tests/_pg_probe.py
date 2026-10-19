"""Subprocess probe used by the Phase 4B PostgreSQL concurrency tests.

Each invocation is one OS process that talks to the disposable test database named by
``DATABASE_URL``. The parent test starts several of these, waits until every child has
printed ``READY``, then releases them all at once so the races are real rather than
sequential.

Commands:
    claim <worker_id>                attempt to claim one ready job
    enqueue <owner_id> <video> <a,b,c>  enqueue comments (dedup path)
    acquire <key> <per_min> <per_day>   consume one shared budget token
    reserve <owner> <mode> <revision>   reserve the automatic publish slot, then commit
    reserve_crash <owner> <mode> <revision>  reserve, then die before committing

Every command prints exactly one JSON object on stdout after the barrier.
"""
import asyncio
import json
import os
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


def _sessions():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app import database
    from app.config import settings

    engine = create_async_engine(
        settings.database_url.get_secret_value(), poolclass=NullPool
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    database.use_test_database(engine, sessions)
    return engine, sessions


async def _run(cmd: str, args: list[str], sessions) -> dict:
    from sqlalchemy import select

    from app.models import ChannelOwner
    from app.services import job_queue

    async with sessions() as db:
        if cmd == "claim":
            job = await job_queue.claim_next(db, args[0])
            return {"job": job.id if job else None}
        if cmd == "enqueue":
            owner = await db.scalar(select(ChannelOwner).where(ChannelOwner.id == args[0]))
            if owner is None:
                return {"error": "owner_missing"}
            result = await job_queue.enqueue(
                db, owner, video_id=args[1], comment_ids=args[2].split(",")
            )
            return {"created": len(result["created"]),
                    "duplicates": result["duplicates"]}
        if cmd == "acquire":
            ok = await job_queue.try_acquire(db, args[0], int(args[1]), int(args[2]))
            return {"ok": bool(ok)}
        if cmd in ("reserve", "reserve_crash"):
            from fastapi import HTTPException

            from app.routers.publication import reserve_automatic_slot

            try:
                await reserve_automatic_slot(db, args[0], args[1], int(args[2]))
            except HTTPException as exc:
                detail = exc.detail if isinstance(exc.detail, dict) else {}
                return {"ok": False, "code": detail.get("code", "http_error"),
                        "status": exc.status_code}
            if cmd == "reserve_crash":
                # The worker died after taking the token but before committing it.
                os._exit(17)
            await db.commit()
            return {"ok": True}
    raise SystemExit(f"unknown command: {cmd}")


def main() -> None:
    cmd, args = sys.argv[1], sys.argv[2:]
    sys.stdout.write("READY\n")
    sys.stdout.flush()
    sys.stdin.readline()
    engine, sessions = _sessions()
    try:
        print(json.dumps(asyncio.run(_run(cmd, args, sessions))))
        sys.stdout.flush()
    finally:
        asyncio.run(engine.dispose())


if __name__ == "__main__":
    main()
