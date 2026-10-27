"""Per-attempt request budgets; each acquisition owns a separate short transaction."""
from app import database
from app.config import settings
from app.services import job_queue, llm


class BudgetHook:
    async def acquire(self, role: str) -> None:
        try:
            async with database.sessions()() as db:
                allowed = await job_queue.try_acquire(
                    db, f'llm:{role}', settings.llm_requests_per_minute,
                    settings.llm_requests_per_day,
                )
        except Exception:  # noqa: BLE001 - redact database details at provider boundary
            raise llm.ProviderFailure('budget_unavailable') from None
        if not allowed:
            raise llm.ProviderFailure('local_budget_exhausted')


class _SingleAttemptProvider:
    """Injected providers without an HTTP adapter model one request per completion."""
    def __init__(self, provider, role, budget):
        self.provider, self.role, self.budget = provider, role, budget
        self.sent = False

    def __getattr__(self, name):
        return getattr(self.provider, name)

    def attempts(self):
        return llm.attempt_trail(self.provider) if self.sent else []

    def usage(self):
        return self.provider.usage() if self.sent else None

    async def complete(self, schema, instruction, data):
        await self.budget.acquire(self.role)
        self.sent = True
        return await self.provider.complete(schema, instruction, data)


def provider(role, budget=None):
    adapter = llm.provider(role)
    hook = budget if budget is not None else BudgetHook()
    if isinstance(adapter, llm.ChatCompletionsProvider):
        adapter.budget = hook
        return adapter
    return _SingleAttemptProvider(adapter, role, hook)
