# Overnight Handoff — Durable Background Jobs (Phase 4)

Recorded: 2026-10-18. This is the human handoff for the overnight session. It says what was
built, how to run and verify it, what changed, and — most importantly — what is still open
and what was done imperfectly.

**No commits, pushes, or branches were created.** All work is in the working tree on
`main`. Review `git status` before committing.

## What this session delivered

A durable, opt-in background job layer for batch reply generation and publishing, plus the
verification to trust it:

1. **Durable job queue** (`reply_jobs` and friends) with conditional-UPDATE claims, time
   leases, heartbeat, classified retries with backoff and jitter, attempt refunds on
   budget waits, and crash recovery.
2. **A batch pipeline** that runs the existing guarded generation → triage → quality →
   publication flow from a worker instead of a request, reusing the publication safety
   machinery unchanged.
3. **Shared budgets**: LLM requests/minute and /day, YouTube reads/minute, and a daily
   automatic-send cap in addition to the existing per-hour cap.
4. **A batch API** (`/api/batch/jobs/...`) and a **Jobs page** in the frontend.
5. **Verification**: 36 new backend tests (11 queue, 17 pipeline, 3 eval, 5 stress), a
   100-comment routing eval, and a 100-job stress suite. Backend is 354 passed / 12
   skipped; frontend is 75 passed; `ruff` clean; frontend build succeeds.

Why it was needed: the baseline audit (`docs/OVERNIGHT_BASELINE_AUDIT.md`) found that the
whole system was request-scoped — "no job table, worker, queue, scheduler, backlog
processing, or batch operation," with no crash recovery and no provider backpressure. A
100-comment batch would have been 100 sequential browser requests that died with any
disconnect.

## Files

Added:

- `backend/app/models/job.py` — `ReplyJob`, `QueueWorker`, `RateLimitCounter`, status
  constants.
- `backend/app/services/job_queue.py` — the queue: enqueue, claim, heartbeat, finish,
  milestone, retry/release, cancel, recovery, budgets, stats.
- `backend/app/services/job_pipeline.py` — worker job execution.
- `backend/app/worker.py` — runnable worker (`python -m app.worker`).
- `backend/app/routers/batch.py` — the batch API.
- `backend/alembic/versions/20261018_01_reply_jobs.py` — additive migration.
- `backend/tests/test_job_queue.py`, `test_batch_pipeline.py`, `test_batch_eval_100.py`,
  `test_batch_stress.py`, `fixtures/eval_dataset_100.json`.
- `frontend/src/pages/JobsPage.tsx`, `frontend/src/JobsPage.test.tsx`.
- `docs/OVERNIGHT_BASELINE_AUDIT.md`, `docs/OVERNIGHT_EDGE_CASE_MATRIX.md`,
  `docs/OVERNIGHT_BENCHMARK_REPORT.md`, `docs/OVERNIGHT_HANDOFF.md` (this file).

Changed:

- `backend/app/config.py` — added job/budget settings (including
  `auto_publish_max_per_hour`, previously a module constant).
- `backend/app/main.py` — registered the batch router.
- `backend/app/models/__init__.py` — exported the new models.
- `backend/app/routers/publication.py` — daily auto-publish budget check; hourly cap now
  reads `settings.auto_publish_max_per_hour`.
- `backend/app/services/llm.py` — `ProviderFailure(code, retry_after)`; defensive
  `Retry-After` parsing.
- `backend/app/services/draft_workflow.py` — carry `retry_after` on a stage result.
- `frontend/src/App.tsx`, `frontend/src/components/AppShell.tsx`,
  `frontend/src/lib/types.ts` — the Jobs route, nav item, and types.
- `docs/AI_DRAFTS.md` — the three "no background jobs" claims were corrected and a
  "Background jobs and batch processing (Phase 4)" section was added.

## How to run

Apply the migration and start the API as usual:

```bash
cd ~/Projects/ReplyAI/backend
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port 8001
```

Run the worker (in its own terminal; walk away without it and jobs simply wait):

```bash
cd ~/Projects/ReplyAI/backend
.venv/bin/python -m app.worker              # loop
.venv/bin/python -m app.worker --once       # one job, for a smoke test
.venv/bin/python -m app.worker --poll-interval 2
```

Verify everything:

```bash
cd ~/Projects/ReplyAI/backend && .venv/bin/python -m pytest -q && .venv/bin/ruff check app/ tests/
cd ~/Projects/ReplyAI/frontend && npx vitest run && npm run build && npm run lint
```

The frontend **Jobs** page (new nav item) lists the queue, shows per-status counts,
auto-refreshes while work is outstanding, and offers enqueue, cancel, requeue, and
reconcile. Enqueue needs an owned `video_id` and comment ids (≤100). Nothing enqueues or
runs on its own.

## Things you must know before trusting this

1. **The migration was applied to the development PostgreSQL database earlier than
   intended.** I set `REPLYAI_TEST_RUN` instead of `REPLYAI_TESTING`, so one command used the
   `.env` `DATABASE_URL` and applied `20261018_01` to the dev database. It is **additive
   only** (three new tables; nothing altered or dropped) and no data was lost, but it is a
   process mistake and you should know the dev database is already at head. The migration was
   then re-validated properly on an isolated temporary database.
2. **The tests run on SQLite, not PostgreSQL.** The job queries avoid PostgreSQL-only SQL
   (portable `case` instead of `greatest`) and the migration chain was validated on isolated
   PostgreSQL, but the suite itself does not exercise the real engine.
3. **Exactly-once is not claimed.** The guarantee is at-most-one publication per comment
   (durable guard) with at-least-once attempt delivery; an interrupted claim surfaces as
   `unknown` and is never auto-retried.
4. **Triage now runs for both workflow engines in the batch path.** This is deliberate and is
   the one behavioral difference from the default request path; see the Phase 4 section of
   `docs/AI_DRAFTS.md`.
5. **The 100-comment eval measures routing, not model quality.** The provider is scripted;
   live quality remains covered only by the opt-in `RUN_LIVE_EVALS` suite.
6. **One extra lint warning was introduced** in `JobsPage.tsx` of the same
   `react(set-state-in-effect)` class already present in `PostedPage.tsx` (4 warnings total
   vs. 3 at baseline).

## Not done / next steps

- The rate-window budgets are proven by re-dating `next_run_at`, not by waiting out real
  windows; a wall-clock soak test would be the next confidence step.
- No multi-process concurrency race test exists.
- No worker metrics/export beyond `queue_workers` counters and the stats endpoint.
- Consider whether the dev-database migration should be rolled back; it was left in place
  because it is additive and harmless, but the choice is yours.
