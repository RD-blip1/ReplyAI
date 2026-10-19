# Overnight Benchmark Report

Recorded: 2026-10-18. These are measured numbers from this session, not estimates. Where a
number was not measured, it says so.

## Test suites

| Suite | Before this session | After | Notes |
| --- | --- | --- | --- |
| Backend `pytest` | 318 passed, 12 skipped | **354 passed, 12 skipped, 0 failed** (215.09s) | +36 tests |
| Backend `ruff check app/ tests/` | clean | **clean** | — |
| Frontend `vitest` | 70 passed (8 files) | **75 passed (9 files)** | +5 tests, +1 file |
| Frontend `npm run build` | succeeded | **succeeded** | `tsc -b && vite build`, 302ms |
| Frontend `npm run lint` (oxlint) | 3 warnings, 0 errors | **4 warnings, 0 errors** | +1 warning of the existing `react(set-state-in-effect)` class, matching the pattern already in `PostedPage.tsx` |

The 36 new backend tests are 11 in `tests/test_job_queue.py`, 17 in
`tests/test_batch_pipeline.py`, 3 in `tests/test_batch_eval_100.py`, and 5 in
`tests/test_batch_stress.py`; they run in 93.81s together. The 12 skipped tests are the
pre-existing opt-in live-provider tests and are unchanged.

## 100-comment routing eval

`tests/test_batch_eval_100.py` runs the real batch pipeline for 100 labeled comments in
`tests/fixtures/eval_dataset_100.json` against a scripted provider, then scores each job's
durable outcome against the category's expected routing.

Dataset composition (100 comments, 7 categories):

| Category | Count | Expected routing |
| --- | --- | --- |
| on_topic | 30 | Publish under `approve_all`; human review under `manual_review` |
| generic_engagement | 15 | Review (`context_required`) — text is generated |
| off_topic | 15 | Skipped, no generation |
| spam_or_suspicious | 15 | Skipped, no generation |
| harassment_or_abuse | 10 | Skipped, no generation |
| unsupported_language | 8 | Skipped, no generation |
| requires_human_attention | 7 | Review (`requires_human`) |

Result: **every one of the 100 comments routed to its expected outcome in both modes**, with
zero mismatches. Under `approve_all`, automation published exactly the 30 on-topic comments
and nothing else. Under `manual_review`, nothing was published and exactly the 45
generatable comments (on-topic + generic engagement) produced reply text. A repeated enqueue
of all 100 produced 0 created and 100 duplicates. This is a routing/policy measurement:
the provider is deterministic, so it does not measure live model quality.

## 100-job stress suite

`tests/test_batch_stress.py` runs 100 jobs (and several smaller adversarial batches) through
the real pipeline with injected failures.

| Scenario | Scale | Result |
| --- | --- | --- |
| Transient generation failures | 100 jobs, first 10 fail once | 100 published, 10 recovered on retry, 0 failed, 100 unique publications |
| Provider budget (5/min) | 100 jobs | Jobs park with `waiting_for: provider_budget` and refunded attempts; 0 failed |
| Poisoned provider | 20 jobs | 20 routed to review, 0 published, 0 failed |
| Unknown publish outcome | 15 jobs | 15 terminal `unknown`, never retried on further drains |
| Duplicate ingestion | 50 comments enqueued twice | 50 jobs, 50 unique publications, 50 duplicates refused |

After every drained batch the count of distinct active jobs is 0, i.e. no comment is left
with two live jobs.

## Migration validation

`20261018_01_reply_jobs` (down-revision `850fa922f687`) was validated on an **isolated**
temporary PostgreSQL database (`replyai_migrate_tmp`), not the development database:

- `alembic upgrade head` — applied cleanly.
- `alembic downgrade -1` — removed the three new tables cleanly.
- `alembic upgrade head` — re-applied cleanly.
- `alembic check` — "No new upgrade operations detected" (no model/schema drift).
- Temporary database dropped afterward.

The migration is additive only: it creates `reply_jobs`, `queue_workers`, and
`rate_limit_counters`, and alters nothing that already existed. The full historical chain
cannot be replayed on SQLite because an earlier migration performs an `ALTER` of a
constraint, which SQLite does not support; this is a pre-existing limitation of the chain,
not of this migration.

## Performance characteristics (measured indirectly)

These are honest observations rather than load-test numbers:

- The 100-comment eval drives 100 full pipeline executions in about a minute on the per-test
  SQLite database; the stress suite drives roughly 300 job executions (including retries and
  parked jobs) in about 50 seconds.
- No wall-clock rate-window test is included: the budget stress test proves parking and
  refunding by re-dating `next_run_at`, so it does not wait out real one-minute windows.
- No throughput or latency benchmark under concurrent worker processes was performed; the
  concurrency safety argument rests on conditional `UPDATE`s and unique indexes, verified
  logically.
