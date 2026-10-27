"""Shared draft-model calls and revision-guarded result persistence."""

import asyncio
from typing import NamedTuple

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import ReplyDraft
from app.models.session import utc_now
from app.services import llm
from app.services import usage as usage_service


class StageResult(NamedTuple):
    value: dict | None
    error: str | None
    usage: dict | None
    role: str
    attempts: list[dict | None]
    # Provider-requested backoff (seconds) when a 429 carried a legible Retry-After.
    retry_after: int | None = None


class StaleWorkflowResult(Exception):
    """The guarded draft revision changed while model calls were in flight."""


async def call_stage(role, schema, instruction, data, *, provider: llm.Provider | None = None) -> StageResult:
    """Reuse the configured provider wrapper and convert failures to safe stage outcomes.

    ``provider`` overrides the configured provider for evaluation, so the harness drives
    the same wrapper (timeouts, attempt trail, error mapping) without touching settings.
    """
    if provider is None and settings.llm_budget_ui:
        from app.services.llm_budget import provider as budgeted_provider
        provider = budgeted_provider(role)
    provider = provider or llm.provider(role)
    try:
        async with asyncio.timeout(settings.llm_stage_timeout_seconds):
            value = await provider.complete(schema, instruction, data)
            return StageResult(
                # exclude_none: optional fields a model left out stay absent.
                schema.model_validate(value.model_dump()).model_dump(exclude_none=True),
                None,
                provider.usage(),
                role,
                llm.attempt_trail(provider),
            )
    except (llm.ProviderFailure, TimeoutError) as exc:
        if isinstance(exc, llm.ProviderFailure):
            code, retry_after = str(exc), exc.retry_after
        else:
            code, retry_after = "provider_timeout", None
        return StageResult(
            None, code, provider.usage(), role, llm.attempt_trail(provider), retry_after
        )
    except Exception:  # noqa: BLE001 - provider boundary must not expose secrets
        return StageResult(
            None, "invalid_response", provider.usage(), role, llm.attempt_trail(provider)
        )


def usage_attempts(
    result: StageResult,
    *,
    draft_id: str,
    revision: int,
    operation_token: str,
    operation: str,
    stage: str,
) -> list[dict]:
    """Build usage ledger rows for each request actually issued by a model stage."""
    return [
        usage_service.entry(
            draft_id=draft_id,
            revision=revision,
            operation_token=operation_token,
            operation=operation,
            stage=stage,
            role=result.role,
            attempt=attempt,
            reported=reported,
            applied=True,
        )
        for attempt, reported in enumerate(result.attempts, start=1)
    ]


async def persist_result(
    db: AsyncSession,
    draft: ReplyDraft,
    *,
    revision: int,
    operation_token: str,
    text: str,
    primary: dict | None,
    secondary: dict | None,
    triage: dict | None,
    quality: dict | None,
    failures: dict,
    usage: dict,
    spent: list[dict],
) -> ReplyDraft:
    """Commit only the still-current operation, then independently record consumption."""
    result = await db.execute(
        update(ReplyDraft)
        .where(
            ReplyDraft.id == draft.id,
            ReplyDraft.revision == revision,
            ReplyDraft.operation_token == operation_token,
        )
        .values(
            text=text,
            primary_classification=primary,
            secondary_classification=secondary,
            triage=triage,
            agreement=llm.agreement(primary, secondary) if not failures else "pending",
            quality=quality,
            generation_status="complete" if primary and text else "failed",
            review_status="complete" if primary and secondary and quality else "failed",
            failure=failures,
            token_usage={key: value for key, value in usage.items() if value},
            operation_token=None,
            updated_at=utc_now(),
        )
        .execution_options(synchronize_session=False)
    )
    accepted = result.rowcount == 1
    if accepted:
        await db.commit()
    else:
        await db.rollback()

    for row in spent:
        row["applied"] = accepted
    await usage_service.record_attempts(spent)
    if not accepted:
        raise StaleWorkflowResult
    await db.refresh(draft)
    return draft


def raise_stale_workflow() -> None:
    raise HTTPException(
        status_code=409,
        detail={
            "code": "stale_review",
            "message": "Draft changed during processing. Older results were discarded.",
        },
    )
