# Phase 4B: Safety Handoff

Everything a future operator or reviewer needs to trust (or finish) this work: what was
done, what was proven, and what was deliberately left alone.

## Safety constraints respected

- No commits, branches, or pushes. No `reset`/`clean`/`checkout`/overwrite/discard of the
  working tree; every untracked file and modification from Phase 4/4A is preserved.
- No live YouTube writes. The only code that mutates YouTube
  (`app/routers/publication.py`) was exercised with the provider mocked; real credentials
  were never used.
- No live model/Groq calls. The offline eval uses a scripted provider; the live eval is
  opt-in (`RUN_LIVE_EVALS=1`) and was **not** run (it requires a real
  `SECONDARY_LLM_API_KEY`).
- The persistent dev database (`replyai`) was not migrated, downgraded, or written to.
  The already-applied `20261018_01` migration is additive-only and was left at head.
- No other repositories were touched.

## What was verified

| Area | How | Result |
|---|---|---|
| Multi-process locking | `tests/test_pg_concurrency.py` on a disposable PostgreSQL DB with real OS processes | 14/14 pass |
| Unknown outcomes | `tests/test_unknown_outcomes.py` (SQLite, end-to-end) | 5/5 pass; no resend on any path |
| Budgets / scheduling | `tests/test_budget_windows.py` with real clock comparisons | 8/8 pass |
| Migration | isolated PG: upgrade → downgrade −1 → upgrade, `alembic check` | no drift |
| Regression | full backend suite | 381 passed, 12 skipped |
| Lint | `ruff check app tests` | clean |
| Frontend | `npm run test`, `npm run build` | 75 passed, build ok |

## Defects found and fixed (with the test that proves it)

1. **`app/worker.py` `_with_session` factory bug** — `sessions()` returns a sessionmaker;
   needed `sessions()()`. The real worker process could not start at all. Proven by the
   two-process test in `test_pg_concurrency.py`.
2. **Post-rollback `MissingGreenlet` (3 sites)** — expired ORM attributes read after
   `rollback()` in `publish_saved_draft`, `publish_generated_draft` /
   `record_automatic_failure`, and `worker`'s failure path. Fixed by snapshotting the
   needed ids/revision/text (and a `_DraftRef`) before rolling back. Proven by
   `test_unknown_outcomes.py`.
3. **Hourly rejection leaked a daily budget token** — the daily token was staged with
   `commit=False` and persisted by the failure recorder. Fixed with one savepoint
   (`db.begin_nested()`) around both reservations. Proven by the F3 regression test in
   `test_budget_windows.py`.

Details, impact, and reproduction for each are in `docs/PHASE4B_INDEPENDENT_REVIEW.md`.

## Invariants to protect in future changes

- **At-most-once publication.** The `reply_publications` open-attempt index and the
  `published_reply_guards` unique `(channel_id, parent_comment_id)` are the real
  guarantees; the worker is only the driver. Any new publish path must claim through
  `publishing.claim`/`settle` and record an attempt before calling YouTube.
- **`unknown` is terminal for automation.** Recovery must keep using `open_attempt`
  (`OPEN_PUBLICATION_STATUSES`); never map `sending`/`unknown` back to a retry.
- **Reservations are savepoint-scoped.** Budget tokens are only valid as part of a
  successful claim; do not move `try_acquire(commit=False)` outside the savepoint, and do
  not replace the savepoint with a session-wide `rollback()` (it will re-break F2).
- **Never read ORM attributes after a rollback.** Snapshot first, or keep the rollback
  inside a savepoint.
- **The fail-closed `database_url()` guard** (`app/database.py`) must stay: it prevents a
  mistyped `REPLYAI_TEST*` variable from running destructive tests against the dev DB.

## Operational notes

- Run backend tests with `REPLYAI_TEST_DATABASE_URL` pointing at a throwaway database
  (e.g. `sqlite+aiosqlite:///./tmp_test.db`); the PostgreSQL suite additionally needs a
  reachable server and drops only the database it creates.
- `alembic` chain validation requires PostgreSQL; SQLite cannot run the earlier
  constraint-alter migration.
- `python -m app.worker` now starts; run with `--once` for a single pass.
- Enabling automatic publishing requires `DRAFT_WORKFLOW_ENGINE=langgraph`; under the
  default `legacy` engine triage is skipped and auto-publish is unreachable by design.

## Deliberately not changed

- The 3 pre-existing frontend lint warnings plus the 1 new `JobsPage` warning of the same
  pattern (documented in the review).
- `ruff format` output (not a repo convention).
- The dev database and its migration state.
