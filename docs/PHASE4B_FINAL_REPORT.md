# Phase 4B: Final Report

Independent verification and correction of the Phase 4 durable background-job work.
Scope was verify-and-fix, not extend. Four defects were found and fixed; everything else
was confirmed and documented. No commits were made.

## Bottom line

The durable job system is safe to hand off. Publishing is at-most-once by database
constraint, `unknown` outcomes cannot be auto-retried, budgets are enforced under real
concurrency, and the worker starts. Four defects were fixed, each with a regression test.
The main caveat is that the AI evaluation only proves routing/policy, not model quality —
see the limitations and the proposed plan.

## Test results

| Suite | Result |
|---|---|
| Full backend (`pytest -q`) | **381 passed, 12 skipped** (baseline was 354 + 12) |
| PostgreSQL concurrency (real processes) | 14/14 pass |
| Unknown-outcome path | 5/5 pass |
| Budget / scheduling (real clock) | 8/8 pass |
| `ruff check app tests` | clean |
| Frontend `npm run test` | 75/75 pass |
| Frontend `npm run build` | ok |

The +27 tests over baseline are entirely new coverage: 14 concurrency, 5 unknown-outcome,
8 budget/scheduling.

## Defects found and fixed

| # | Defect | Impact | Fix |
|---|---|---|---|
| F1 | `worker.py::_with_session` used `sessions()` (a factory) instead of `sessions()()` | the real worker process could not start; only injected-session tests hid it | call the sessionmaker |
| F2 | 3 sites read ORM attributes after `rollback()` → `MissingGreenlet` (`publication.py` ×2, `worker.py`) | correct decisions (esp. the reply-created-but-unconfirmed `unknown` case) were turned into unexpected failures/retries | snapshot ids/revision/text (and a `_DraftRef`) before rolling back |
| F3 | hourly rate-limit rejection persisted a staged daily budget token (`commit=False` survived into `record_automatic_failure`) | a burst of hourly-limited attempts silently exhausted the daily budget without publishing | wrap both reservations in one savepoint (`db.begin_nested()`); roll the savepoint back on refusal |

Full write-ups with reproductions: `docs/PHASE4B_INDEPENDENT_REVIEW.md`.

## What was proven

- **Concurrency (PostgreSQL, separate OS processes).** Claim/lease, duplicate enqueue,
  budget boundaries, duplicate-guard uniqueness, two real workers on one job, crash before
  / after a confirmed post, and requeue of an `unknown` job — 14 cases, all pass. Migration
  is executed by the harness, so the schema is verified, not simulated.
- **Unknown outcomes (SQLite, end-to-end).** Every route that could resend is covered; the
  durable attempt always wins, and nothing is inserted twice.
- **Budgets / scheduling (real clock).** Hourly-window final-slot/rejection/rollover, daily
  exhaustion + 24h rollover, provider minute window, budget-refusal refund + retry
  scheduling, `claim_next` honouring `next_run_at`, backoff bounds, real 1s lease expiry.
- **Migration.** Isolated PostgreSQL: upgrade → downgrade −1 → upgrade, `alembic check` no
  drift. Dev database untouched (already at head; additive-only).
- **Fail-closed guard.** `app/database.database_url()` aborts test runs that point at the
  dev database.

## Migration incident (reported honestly)

An earlier run accidentally targeted the dev PostgreSQL database (an environment-variable
typo, `REPLYAI_TEST_RUN`) and applied `20261018_01`. The migration is additive-only, the dev
database is already at head, and it was **not** rolled back. The fail-closed guard added in
Step 4 prevents a recurrence. Full chain validation must use PostgreSQL; SQLite rejects an
earlier constraint-`ALTER` migration.

## AI-eval limitations (important)

- The always-on 100-comment eval is **circular for model quality**: its provider echoes the
  dataset label, so it validates *routing/policy* only. "100/100" is not a model-accuracy
  claim.
- The live eval is opt-in, covers 4 triage cases for one model, and uses a copy of the
  production prompt that has already drifted (it omits three categories).
- Generation and the quality reviewer are never scored; there is no confusion matrix,
  confidence calibration, adversarial set, or versioned snapshot.

A concrete plan to raise this to a real eval is in `docs/PHASE4B_INDEPENDENT_REVIEW.md`
(use the real prompt, labeled multi-class set + macro-F1 gate, separate generation/quality
rubrics, confidence calibration, versioned runs).

## Repo state

- Branch uncommitted; HEAD `06a240f`. **No commits, branches, or pushes.**
- Tracked changes: 11 files changed, 316 insertions(+), 75 deletions(-).
- Untracked: 24 files (new tests and docs), all preserved.
- Only `ruff check` is the lint gate (clean); `ruff format` is not a repo convention.
- Frontend lint: 3 pre-existing warnings + 1 new `JobsPage` warning of the same
  `react(set-state-in-effect)` pattern — left deliberately to avoid behavior change.

## Remaining items / recommendations

1. Adopt the eval plan before making model-quality claims.
2. Optionally address the `JobsPage` `set-state-in-effect` warning together with the three
   existing ones in a dedicated pass.
3. Keep the invariants in `docs/PHASE4B_SAFETY_HANDOFF.md` intact in future changes
   (at-most-once publication, `unknown` terminal for automation, savepoint-scoped budgets,
   no post-rollback ORM reads).
