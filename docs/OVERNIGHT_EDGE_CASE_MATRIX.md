# Overnight Edge-Case Matrix

Recorded: 2026-10-18. Every row states what is proven, by which test, and — where it
matters — what is **not** covered. Test names are exact; the adversarial claims were
checked against the code, not assumed.

Backend suite at the end of the session: **354 passed, 12 skipped**. The 12 skipped tests
are the pre-existing opt-in live-provider tests (`RUN_LIVE_EVALS`, real OAuth/Groq), gated
exactly as before.

## Duplicate protection

| Claim | Proof | Not covered |
| --- | --- | --- |
| A second enqueue of an active comment creates no second job | `test_enqueue_deduplicates_active_jobs`, `test_duplicate_enqueue_creates_one_job` (API), `test_eval_100_dedupes_repeated_enqueue` (100 comments) | Two workers claiming the same comment at the same instant across processes is proven by the conditional `UPDATE` in `claim_next` + the partial unique index, exercised per-test, not by a real multi-process race |
| The same comment is never published twice under load | `test_duplicate_ingestion_cannot_double_publish` (50 comments enqueued twice, 50 unique publications), `test_100_jobs_complete_and_recover_from_injected_transient_failures` (100 unique) | YouTube-side duplicates remain outside local control, unchanged from baseline |
| A single open publication attempt blocks a second | Baseline `test_publishing_modes.py` / `test_dry_run.py`, unchanged | — |

## Retry and failure classification

| Situation | Expected | Proof |
| --- | --- | --- |
| Transient provider error | Retry with backoff, recover within `max_attempts` | `test_100_jobs_complete_and_recover_from_injected_transient_failures` (10 jobs fail once, all publish on retry) |
| Provider budget exhausted | Park with attempt refunded, never fail | `test_provider_budget_parks_jobs_and_never_fails_them`, `test_llm_daily_budget_parks_the_second_job` |
| Poisoned / invalid provider response | Route to human review, never crash or loop | `test_poisoned_generation_routes_to_review_never_crashes` |
| Unknown publication outcome | Terminal, never auto-retried; requeueable only by a person | `test_unknown_publication_outcome_is_never_retried`, `test_unknown_publish_outcomes_are_terminal_under_load` |
| Definitively rejected send | Review, nothing posted | `test_rejected_publication_does_not_post_and_needs_review` |
| Revoked grant | `needs_reauth` terminal state | `test_revoked_connection_marks_needs_reauth` |
| Backoff bounds and jitter | Bounded, non-constant | `test_backoff_is_bounded_and_jittered` |

## Lease, recovery, and ownership

| Situation | Expected | Proof |
| --- | --- | --- |
| An expired lease becomes recoverable, then claimable | Returned to the queue | `test_expired_lease_becomes_recoverable_then_claimable` |
| Worker finishes a job it no longer owns | Rejected | `test_finish_requires_current_ownership` |
| Recovery finds an open (`sending`/`unknown`) attempt | Marks `unknown`, does not retry | `test_recovery_reports_unknown_for_an_open_attempt` |
| Recovery finds a `posted` attempt | Marks `published` | `test_recovery_reports_published_for_a_posted_attempt` |
| A second claim while leased | Returns nothing | `test_created_job_is_claimable_and_second_claim_is_none` |

## API surface and authorization

| Situation | Expected | Proof |
| --- | --- | --- |
| Job listing is scoped to the caller's channel | No cross-channel leakage | `test_job_listing_is_channel_scoped` |
| Cancel only a waiting (`pending`/`retry`) job | Other statuses refused | `test_cancel_only_applies_to_waiting_jobs`, `test_cancel_waiting_job_via_api` |
| Requeue only a terminal job | Others refused | `test_requeue_requires_a_finished_job` |
| Stats summary counts by status | Correct totals | `test_stats_summary_reports_counts`, `test_queue_stats_counts_by_status` |
| Enqueue is deduplicated at the API layer | Created/duplicates split | `test_duplicate_enqueue_creates_one_job` |
| Worker with an empty queue | Idle, no error | `test_worker_is_idle_with_no_jobs` |
| Config used by the worker exists | No missing-setting crash | `test_settings_used_by_worker_exist` |

## Routing correctness (100-comment eval)

The offline harness (`test_batch_eval_100.py`) drives the real pipeline for all 100 labeled
comments with a scripted provider, so a mismatch means the pipeline routed a category
wrongly, not that a model was unlucky.

| Category (count) | Expected routing | Mode |
| --- | --- | --- |
| `on_topic` (30) | Publish under `approve_all`; review under `manual_review` | both |
| `generic_engagement` (15) | Review, `context_required` (text generated) | both |
| `off_topic` (15), `spam_or_suspicious` (15), `harassment_or_abuse` (10), `unsupported_language` (8) | Skipped, no generation | both |
| `requires_human_attention` (7) | Review, `requires_human` | both |

Proof: `test_eval_100_routes_every_category_correctly_in_approve_all`,
`test_eval_100_in_manual_review_never_publishes` (also asserts only the 45 generatable
comments — on-topic + generic engagement — produced reply text). The harness lifts the
per-minute/day/hour budgets so routing is isolated from rate policy; budget behavior is
covered separately by the stress suite.

## Safety invariants under load

| Invariant | Proof |
| --- | --- |
| No comment gains two active jobs | Partial unique index; `_distinct_active_pairs == 0` after drain |
| Automation writes exactly the on-topic set, nothing else | `test_eval_100_routes_every_category_correctly_in_approve_all` |
| Manual review never publishes | `test_eval_100_in_manual_review_never_publishes` |
| A poisoned model cannot force a publish | `test_poisoned_generation_routes_to_review_never_crashes` |
| Budget pressure fails nothing | `test_provider_budget_parks_jobs_and_never_fails_them` |

## Deliberately not proven here (honest gaps)

- **No real YouTube mutation or live Groq call.** All providers and YouTube reads are
  mocked; the live suite stays opted-out.
- **No true multi-process concurrency test.** Atomicity rests on conditional `UPDATE`s and
  unique indexes, verified logically per test, not by racing two OS processes.
- **SQLite, not PostgreSQL, for the suite.** The job queries are written to run on both
  (portable `case` instead of `greatest`), and the migration chain was validated separately
  on an isolated PostgreSQL database, but the tests themselves execute on per-test SQLite.
- **Exactly-once is not claimed.** The guarantee is at-most-one publication per comment plus
  at-least-once delivery of attempts; an interrupted claim becomes `unknown`, never a silent
  duplicate.
- **A budget-parked batch spans minutes by design.** The stress tests prove jobs park and
  resume logically (by re-dating `next_run_at`); they do not wait out real wall-clock
  windows.
