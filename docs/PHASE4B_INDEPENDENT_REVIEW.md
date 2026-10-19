# Phase 4B: Independent Review

Verification (not extension) of the Phase 4 durable background-job work: safety,
locking, transactions, unknown publication outcomes, budgets, the migration, and the
AI-eval harness. Nothing here adds a feature; four defects were found and fixed, and the
failures that remain are documented as limitations.

## Executive summary

- **Baseline preserved.** Full backend suite: **381 passed, 12 skipped** (Phase 4A
  reported 354 + 12; +14 PostgreSQL concurrency, +5 unknown-outcome, +8 budget/scheduling
  tests all pass). `ruff check app tests` clean. Frontend: **75 passed**, `npm run build`
  succeeds, lint has the same 3 pre-existing warnings plus 1 new one of the same pattern
  (see *Known warnings*).
- **Four real defects found and fixed**, each with a regression test or a reproduced
  failure that now passes (details below): the worker could not run as a process; three
  post-rollback `MissingGreenlet` crashes turned correct decisions into unexpected
  failures; an hourly rate-limit rejection silently spent a daily budget token.
- **Core safety holds.** At-most-once publication is enforced by the database (conditional
  claims + partial unique indexes), `unknown` outcomes are never auto-retried, and a
  requeue by a person still cannot resend. Confirmed under real multi-process contention
  on PostgreSQL and in SQLite end-to-end tests.
- **Migration validated on an isolated PostgreSQL database** (upgrade → downgrade −1 →
  upgrade, `alembic check` no drift); the persistent dev database was not mutated. A
  fail-closed guard now aborts test runs that point at the dev database.

## Findings and fixes

### F1 — `app/worker.py` could not start as a real process (fixed)

`_with_session` did `async with sessions() as db`, but `app.database.sessions()` returns
an `async_sessionmaker` (the factory). Every other call site writes `sessions()()`. Tests
always injected a session, so the process entry path was never exercised until a real
multi-process test ran it.

- **Impact:** `python -m app.worker` died immediately with
  `TypeError: 'async_sessionmaker' object does not support the asynchronous context
  manager protocol`. In production, no worker would have started.
- **Fix:** `async with sessions()() as db` (`app/worker.py`).
- **Evidence:** `tests/test_pg_concurrency.py` launches two real
  `python -m app.worker --once` processes; both now behave.

### F2 — three post-rollback `MissingGreenlet` crashes (fixed)

`AsyncSession.rollback()` expires ORM instances, and an `AsyncSession` cannot lazily
reload an expired attribute outside a greenlet. Three places read attributes *after* a
rollback, converting a correct decision into `MissingGreenlet` (and, in the worker path,
an `unexpected_error` retry):

1. `publish_saved_draft` logged `draft.id` / `publication.operation_id` after rolling back
   a failed outcome write.
2. `publish_generated_draft` read `draft.id` / `draft.revision` / `draft.text` after the
   inner call rolled back, and `record_automatic_failure` read `draft.*`.
3. `worker.run_once` / `run_loop` read `job.id` in the exception handler after
   `process_job` rolled back.

- **Impact:** the worst case (reply created, confirmation write lost) is *supposed* to be
  reported `unknown`. The crash could instead surface as an unexpected failure/retry. The
  durable attempt row still prevented a duplicate send, so this was not a duplicate-reply
  risk — but it masked the real decision and burned a retry.
- **Fix:** snapshot the ids/revision/text (and a small `_DraftRef` stand-in for
  `record_automatic_failure`) **before** the rollback, and use the snapshots afterwards
  (`app/routers/publication.py`, `app/worker.py`).
- **Evidence:** `tests/test_unknown_outcomes.py::test_reply_created_but_unrecorded_is_reported_unknown`
  reproduces the crash before the fix and passes after it.

### F3 — an hourly rate-limit rejection spent a daily budget token (fixed)

`reserve_automatic_slot` acquired the daily `rate_limit_counters` token with
`commit=False` (staged in the current transaction), then checked the hourly window. When
the hourly window was full, it raised `429 auto_publish_rate_limited`; the caller
(`publish_generated_draft`) then recorded the failure via `record_automatic_failure`,
whose `commit()` persisted the staged **daily** token even though nothing was attempted.

- **Impact:** every hourly-limited attempt permanently burned one of the 50 daily
  tokens, so a burst could exhaust the daily budget without a single publish.
- **Reproduction:** with the hourly window full, a worker run left
  `auto_publish:day:<owner> day_count == 1` and `published == []`.
- **Fix:** both reservations (daily token + hourly window) now happen inside one
  `SAVEPOINT` (`db.begin_nested()`); a refusal rolls the savepoint back, discarding the
  staged token. A savepoint (rather than a session-wide rollback) is used precisely
  because a full rollback would expire the caller's ORM objects and trigger F2.
- **Evidence:** `tests/test_budget_windows.py::test_hourly_rejection_does_not_spend_a_daily_token`
  (asserts `day_count` is unchanged) and
  `::test_hourly_window_final_slot_then_rejection_then_rollover`.

## Budget and scheduling verification (Step 5)

`tests/test_budget_windows.py` (8 tests) drives the real clock comparisons:

| Case | What is verified |
|---|---|
| Hourly window: final slot → rejection → rollover | `max_per_hour`th send allowed, next refused with "per hour", a window older than 1h restarts at 1 |
| Hourly rejection does not spend a daily token | regression for F3 (`day_count` unchanged) |
| Daily token: exhaustion → 24h rollover | at `max_per_day` the send is refused ("per day"); a `day_started_at` older than 24h restarts at 1 |
| Minute provider window rollover | a window 61s old restarts `window_count` at 1 |
| Provider budget refusal | the job is released for `retry`, the attempt is **refunded** (`attempts == 0`), `outcome.waiting_for == provider_budget`, `next_run_at` ≈ `BUDGET_RETRY_SECONDS` (60s) |
| `claim_next` honours `next_run_at` | a job in backoff is not claimable; once due it is |
| Backoff bounds | `backoff_delay(attempts)` stays in `[1, min(30·2^(n−1), 900)]`; the 900s cap holds deep in a retry loop |
| Real short lease | a 1s lease is still owned while live, then expires in real time and becomes recoverable |

The two concurrency budget boundaries (last daily token, last hourly slot) are additionally
raced across real processes in `tests/test_pg_concurrency.py` (all pass).

## Unknown-outcome audit (Step 3)

`tests/test_unknown_outcomes.py` (5 tests) covers every route that could turn an
`unknown` into a second YouTube write:

1. provider reports `unknown` → job `unknown`, attempt `unknown`, guard stays, worker will
   not pick it up again, and a second decision pass still refuses to publish;
2. reply created but the confirmation write failed → `unknown` (the F2 case), and
   reconciliation leaves the terminal job alone; a genuinely crashed worker
   (`publish_claimed` + `sending` attempt) is reconciled to `unknown` with no resend;
3. explicit human requeue of an `unknown` job → still `unknown`, no resend, guard intact;
4. definitive refusal → attempt `failed`, guard released; a requeued job does **not**
   republish an existing draft (`draft_exists`), and the deliberate human publish then
   posts exactly once;
5. the reconcile endpoint derives state from durable attempts.

In every case the durable publication attempt wins over job state and nothing is inserted
twice.

## Migration audit (Step 4)

- `alembic/versions/20261018_01_reply_jobs.py` was applied on an isolated PostgreSQL
  database (`replyai_migrate_tmp`): upgrade → downgrade −1 → upgrade succeeded and
  `alembic check` reported no drift; the temporary database was dropped. `CREATE/DROP
  DATABASE` require `isolation_level="AUTOCOMMIT"`.
- The full Alembic chain cannot run on SQLite (an earlier migration uses `ALTER` of
  constraints, which SQLite rejects), so chain validation must use PostgreSQL.
- The persistent dev database is already at head `20261018_01` (the migration is
  additive-only) and was **not** rolled back.
- Fail-closed guard: `app/database.database_url()` now raises if any `REPLYAI_TEST*`
  environment variable other than `{REPLYAI_TEST_DATABASE_URL, REPLYAI_TESTING}` is set,
  so a mistyped variable can never silently run tests against the dev database.

## AI-eval limitations review (Step 6)

Two eval surfaces exist: `tests/test_batch_eval_100.py` (offline, always on) and
`tests/test_triage_evals.py` (live, opt-in `RUN_LIVE_EVALS=1`).

Limitations, in priority order:

1. **The 100-comment eval is circular for model quality.** Its provider is scripted to
   echo the dataset label into `llm.Triage`, so it can only validate *routing/policy*
   (which category leads to which durable outcome), never triage accuracy. This is
   correctly documented in the test, but it means "100/100 correct" is a statement about
   policy, not about the model.
- **The live eval is thin and already drifting.** It covers 4 hand-written triage cases
  for one model (secondary), with an inline instruction string that is a *copy* of
  `app/services/draft_graph.py::comment_triage`; the copy already omits
  `harassment_or_abuse`, `unsupported_language`, and `requires_human_attention`, so it
  evaluates a prompt the app no longer sends. It is opt-in (`RUN_LIVE_EVALS=1`) and not
  run in CI.
- **No generation- or quality-model evaluation.** The primary reply generator and the
  secondary quality reviewer (`llm.Quality`) are exercised only for routing, never
  scored for correctness, tone, or hallucination.
- **Dataset shape.** 100 rows, one video topic, English, single annotator, no
  adversarial/prompt-injection rows (an injection string exists in `tests/support.py` but
  is not part of the eval set), no inter-annotator agreement, no confidence calibration.
- **No metrics beyond exact match.** There is no confusion matrix, precision/recall, or
  threshold, and no versioned snapshot to compare across model changes.

## Independent eval plan (proposed, not implemented)

1. **Make the live eval use the real prompt.** Import the instruction from
   `draft_graph.comment_triage` (or extract it to a constant both use) so the eval cannot
   drift from production.
2. **Grow it into a labeled set with a confusion matrix.** ≥150 rows across all 7
   categories plus `uncertain`, multi-topic and multilingual, ≥10% adversarial/injection
   rows; report per-class precision/recall and a minimum macro-F1 gate.
3. **Score generation and quality separately.** A rubric pass over generated replies
   (relevance, factuality, tone) and a check that the quality reviewer's `pass`/`concern`
   agrees with a human label on a sample.
4. **Calibrate confidence.** Assert that `confidence` correlates with correctness so the
   `uncertain`/`confidence_insufficient` routing can be trusted.
5. **Version the dataset and record runs.** Store dataset + model + prompt hash with each
   result so a regression is attributable.

Until (1)–(3) exist, treat the eval as a **policy/routing regression suite**, and say so
in any status report.

## Verification of existing tests

- `tests/test_job_queue.py` 11/11, `tests/test_batch_pipeline.py` 17/17,
  `tests/test_batch_eval_100.py` 3/3, `tests/test_batch_stress.py` 5/5 — unchanged and
  passing.
- New: `tests/test_pg_concurrency.py` 14/14 (PostgreSQL, real processes),
  `tests/test_unknown_outcomes.py` 5/5, `tests/test_budget_windows.py` 8/8.
- Full suite green after every fix; `ruff check` clean.

## Known warnings (not fixed, deliberate)

- `frontend/src/pages/JobsPage.tsx:111` — `react(set-state-in-effect)`. This is the same
  pattern as the 3 pre-existing warnings (`src/hooks/useAppData.ts`,
  `src/pages/InboxPage.tsx`, `src/pages/PostedPage.tsx`). Removing it means restructuring
  the `load`/effect relationship, which risks changing behavior, so it is left
  consistent with the existing pages and recorded here.
- `ruff format` is **not** a repo convention: it reports 61 pre-existing files. Only
  `ruff check` is the gate, and it is clean.
