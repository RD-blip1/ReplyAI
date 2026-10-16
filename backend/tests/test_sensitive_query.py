import asyncio
import logging

import pytest

from app.middleware.sensitive_query import (
    REDACTED_QUERY,
    SensitiveOAuthAccessLogFilter,
    SensitiveOAuthQueryRedactionMiddleware,
)


def _access_record(full_path: str) -> logging.LogRecord:
    # uvicorn.access records carry (client_addr, method, full_path, http_version, status).
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:12345", "GET", full_path, "1.1", 303),
        exc_info=None,
    )


def test_access_log_filter_redacts_the_callback_query_string() -> None:
    record = _access_record(
        "/auth/google/callback?code=synthetic-code&state=synthetic-state"
    )
    filter_ = SensitiveOAuthAccessLogFilter()

    assert filter_.filter(record) is True
    assert record.args[2] == f"/auth/google/callback?{REDACTED_QUERY}"
    assert "synthetic-code" not in str(record.args)
    assert "synthetic-state" not in str(record.args)
    # The method, path and status are preserved for ordinary diagnostics.
    assert record.args[1] == "GET"
    assert record.args[4] == 303


def test_access_log_filter_leaves_other_routes_untouched() -> None:
    record = _access_record("/api/youtube/videos?page_token=abc")
    SensitiveOAuthAccessLogFilter().filter(record)
    assert record.args[2] == "/api/youtube/videos?page_token=abc"


def test_access_log_filter_tolerates_unexpected_record_shapes() -> None:
    record = logging.LogRecord(
        name="uvicorn.access", level=logging.INFO, pathname=__file__, lineno=1,
        msg="unstructured %s", args=("one",), exc_info=None,
    )
    assert SensitiveOAuthAccessLogFilter().filter(record) is True
    assert record.args == ("one",)


def test_callback_query_is_available_to_route_then_removed_for_access_logging() -> None:
    scope = {
        "type": "http",
        "path": "/auth/google/callback",
        "query_string": b"code=synthetic-code&state=synthetic-state",
    }
    query_seen_by_route: list[bytes] = []

    async def downstream(_scope, _receive, _send) -> None:
        query_seen_by_route.append(scope["query_string"])

    asyncio.run(SensitiveOAuthQueryRedactionMiddleware(downstream)(scope, None, None))

    assert query_seen_by_route == [b"code=synthetic-code&state=synthetic-state"]
    assert scope["query_string"] == b""


def test_callback_query_is_removed_when_route_raises() -> None:
    scope = {
        "type": "http",
        "path": "/auth/google/callback",
        "query_string": b"code=synthetic-code&state=synthetic-state",
    }

    async def failing_route(_scope, _receive, _send) -> None:
        raise RuntimeError("synthetic callback failure")

    with pytest.raises(RuntimeError, match="synthetic callback failure"):
        asyncio.run(SensitiveOAuthQueryRedactionMiddleware(failing_route)(scope, None, None))

    assert scope["query_string"] == b""