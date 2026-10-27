"""Shared failure taxonomy: infrastructure is never a content judgement."""
TRANSIENT = frozenset({'rate_limited', 'transient_error', 'provider_unavailable',
                      'provider_timeout', 'provider_request_failed', 'budget_unavailable'})
PERMANENT = frozenset({'provider_auth_failed', 'provider_bad_request',
                      'provider_rejected_request'})
INVALID = frozenset({'model_output_invalid', 'invalid_response', 'incomplete_response',
                    'response_too_large'})


def outcome_kind(code: str | None) -> str:
    if code is None:
        return 'ok'
    if code in TRANSIENT:
        return 'infra_transient'
    if code in INVALID:
        return 'output_invalid'
    if code == 'local_budget_exhausted':
        return 'budget_stopped'
    # Unknown failure codes fail closed as technical failures.
    return 'infra_permanent'


def case_status(stages: list[dict]) -> str:
    kinds = {s.get('outcome_kind', outcome_kind(s.get('error'))) for s in stages}
    if kinds & {'infra_transient', 'infra_permanent'}:
        return 'INFRA_FAILED'
    if 'output_invalid' in kinds:
        return 'OUTPUT_INVALID'
    if kinds & {'budget_stopped', 'budget'}:
        return 'NOT_RUN'
    return 'COMPLETE'
