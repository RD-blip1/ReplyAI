# Phase 4B: PostgreSQL Concurrency Verification

## Method

`backend/tests/test_pg_concurrency.py` (14 tests) runs against a **disposable**
PostgreSQL database on the same server as development:

1. The harness creates `replyai_phase4b_<random>` with `CREATE DATABASE` (autocommit
   connection).
2. It runs the real `alembic upgrade head` in a subprocess with
   `REPLYAI_TEST_DATABASE_URL` pointed at that database, so the migration itself is
   executed and verified, not simulated.
3. Every test truncates all tables before it runs, so cases are independent.
4. The database is dropped with `DROP DATABASE ... WITH (FORCE)` (terminating
   backends first) in the fixture teardown — including on failure.
5. The development database (`replyai`) is never written to: the harness only ever
   connects to it as an admin to create/drop its own database.

**Multi-process races are real.** `backend/tests/_pg_probe.py` is launched as
separate OS processes. Each prints `READY`, blocks on stdin, and the parent releases
all of them simultaneously, so the conditional UPDATEs in `job_queue` are genuinely
contended instead of serialised by a single event loop. External providers are either
mocked or not reached; no live YouTube or model call happens.

Skip control: `REPLYAI_PG_TESTS=0` disables the module; unreachable PostgreSQL is
skipped rather than failed.

## Cases and results

| Case | How it is raced | Result |
|---|---|---|
| Concurrent enqueue dedup | 4 processes enqueue the same comment at once | **PASS** — 1 job created, 3 counted as duplicates; the partial unique index `uq_active_reply_job` is the referee |
| Two workers claim one job | 4 processes call `claim_next` simultaneously | **PASS** — exactly one claims, `attempts == 1`, others get `None` |
| Lease loss / reclaim | claimed job's lease expired, then `recover_interrupted` | **PASS** — job returns to `pending`, `recoveries == 1` |
| Crash with open `sending` attempt | job died mid-publish, durable attempt is `sending` | **PASS** — marked `unknown` with `publish_outcome_unknown`, never requeued |
| Crash after budget reserve (before commit) | child process `os._exit(17)` after `reserve_automatic_slot`, before `commit()` | **PASS** — neither the hourly counter nor the daily `rate_limit_counters` token persisted |
| Final daily budget token | token already at limit, then 6 processes try to take one | **PASS** — all 6 refused; limit not exceeded |
| Final hourly budget slot | `auto_publish_count` at `max_per_hour - 1`, 4 processes reserve | **PASS** — exactly 1 succeeds, count lands exactly on the limit; losers fail closed with 429 and consume nothing |
| Cancel while processing | job claimed, then `cancel()` | **PASS** — refused (returns `None`); in-flight work is not interrupted |
| Terminal job does not block a new one | finish a job, re-enqueue the same comment | **PASS** — second job created; index only covers active statuses |
| Duplicate guard uniqueness | two `published_reply_guards` for the same parent | **PASS** — second insert refused by `IntegrityError` inside a savepoint |
| **Two real worker processes** | two `python -m app.worker --once` processes on one job | **PASS** — `attempts == 1`, job finishes `needs_reauth`/`reconnect_required`, the other process idles |
| Crash **before** publication (`verified` + no attempt) | recovery | **PASS** — back to `pending`, `recoveries == 1`, `outcome.draft_revision` kept so the retry resumes at publication instead of regenerating |
| Crash **after** confirmed post (`posted` attempt) | recovery | **PASS** — job marked `published` with the recorded `reply_comment_id` |
| Requeue of an `unknown` job | requeue, then re-run the worker's decision | **PASS** — the durable attempt still wins: job goes back to `unknown`, no resend |

## Defect found and fixed

**`app/worker.py` could not start as a real process.**
`_with_session` did `async with sessions() as db`, but `app.database.sessions()`
returns an `async_sessionmaker` (the factory), not a session. Every other call site
(`app/dependencies.py:16`, `app/services/usage.py:99`) correctly writes
`sessions()()`. The in-process tests passed because they always inject a session
(`worker.run_once(..., session=db)`), so nothing ever exercised the process entry
path — the multi-process test above is what exposed it.

```python
# app/worker.py (fixed)
async with sessions()() as db:
    return await fn(db, **kw)
```

Before the fix, `python -m app.worker` died immediately with
`TypeError: 'async_sessionmaker' object does not support the asynchronous context
manager protocol`. After the fix, `python -m app.worker --once` starts and exits 0.

Two further defects surfaced while auditing unknown outcomes (the reply-created-but-
unconfirmed path): reading expired ORM objects after a `rollback()` (`MissingGreenlet`) in
`publication.py` and `worker.py`, and an hourly rate-limit rejection that persisted a
staged daily budget token. Both are fixed and covered by `tests/test_unknown_outcomes.py`
and `tests/test_budget_windows.py`; see `docs/PHASE4B_INDEPENDENT_REVIEW.md` (F2, F3). The
multi-process tests above exercise the fixed worker path.

## Not covered here

- The crash-after-reserve case is exercised at the reservation boundary, not inside a
  full `publish_generated_draft` call, because a full publish requires a YouTube
  credential; the equivalent in-process coverage lives in
  `tests/test_batch_pipeline.py` (mocked insert) and `tests/test_dry_run.py`.
- Real provider rate limits (Groq/YouTube) are not hit; budget arithmetic is verified
  against `rate_limit_counters` and `publishing_settings` only.
