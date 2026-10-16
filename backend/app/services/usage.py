"""Provider-consumption accounting, kept separate from guarded draft state.

Consumption is a fact about the provider, not about the draft. A stage can spend
tokens and then have its result rejected by a stale-operation guard, so accounting
runs in its own session and its own transaction:

* it cannot flush or commit draft changes that some other code left pending, because
  it never shares a session with the request;
* a rollback of the draft transaction cannot erase a recorded request, because the
  accounting transaction is already committed and separate.

The session comes from :func:`app.database.sessions`, the single place that decides
which database is in use, so an accounting session in a test run is a test session and
can never reach the development database.
"""
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database import sessions
from app.models.draft import DraftUsage

USAGE_TRACKING_STARTED_AT = datetime(2026, 9, 27, 17, 38, tzinfo=UTC)
"""When this ledger began recording requests, matching migration 20260927_03.

A draft created before this point had provider activity that predates the ledger.
Those per-operation counts were overwritten by the stage snapshot and cannot be
reconstructed, so such a draft's totals are reported as incomplete rather than as
a lifetime total. No count is estimated to fill the gap.
"""

PRE_LEDGER_NOTICE = "Recorded usage since tracking began; earlier usage is unavailable."

UNKNOWN_CONSUMPTION_NOTICE = (
    "Consumption is unknown for these requests: no readable response came back, so it "
    "cannot be said whether the provider processed or billed them."
)


def entry(*, draft_id: str, revision: int, operation_token: str, operation: str, stage: str,
          role: str, attempt: int, reported: dict | None, applied: bool) -> dict:
    """One ledger row for one HTTP request.

    ``reported`` is the provider's own usage for that request, or ``None`` when the
    request produced no readable response. Null counts are kept as null: an unknown
    request is never stored as zero, and is not presented as a confirmed charge.
    """
    return {
        "draft_id": draft_id, "revision": revision, "operation_token": operation_token,
        "operation": operation, "stage": stage, "role": role, "attempt": attempt,
        "model": (reported or {}).get("model", ""),
        "input_tokens": (reported or {}).get("input_tokens"),
        "output_tokens": (reported or {}).get("output_tokens"),
        "consumption": "reported" if reported is not None else "unknown",
        "applied": applied,
    }


def _key(row: dict) -> tuple:
    return (row["draft_id"], row["operation_token"], row["stage"], row["attempt"])


async def _insert_missing(db, rows: list[dict]) -> None:
    existing = set()
    for start in range(0, len(rows), 500):
        window = rows[start:start + 500]
        found = await db.execute(select(
            DraftUsage.draft_id, DraftUsage.operation_token,
            DraftUsage.stage, DraftUsage.attempt,
        ).where(DraftUsage.draft_id.in_({row["draft_id"] for row in window})))
        existing.update(found.all())
    for row in rows:
        if _key(row) not in existing:
            db.add(DraftUsage(**row))


async def record_attempts(rows: list[dict]) -> None:
    """Commit accounting rows in a dedicated session and transaction.

    Owning the session is the point: a caller cannot accidentally have unrelated
    pending draft work committed here, and the caller rolling back its own draft
    transaction cannot remove these rows.

    **Ordering contract:** call this only after the caller's transaction has been
    committed or rolled back, which is what :func:`app.routers.drafts.run_models` does.
    Accounting never needs a row the caller holds, so the two sessions do not deadlock,
    but calling this while the caller still holds an open write transaction would make
    the insert wait on that transaction's lock rather than fail fast.

    Only requests that are not already recorded are inserted, and the unique attempt
    key is the authority: if a concurrent writer wins the race the insert is retried
    once, after which the already-counted attempt is skipped. Re-running this with the
    same rows, whether after a database error or by mistake, cannot inflate totals, so
    a persistence retry never becomes a second charge in the ledger.
    """
    if not rows:
        return
    async with sessions()() as db:
        for remaining in (True, False):
            try:
                await _insert_missing(db, rows)
                await db.commit()
                return
            except IntegrityError:
                await db.rollback()
                if not remaining:
                    raise


def as_utc(value: datetime) -> datetime:
    """SQLite hands back naive datetimes even for timezone-aware columns."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value
