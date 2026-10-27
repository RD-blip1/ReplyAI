"""Engine and session construction.

This module is the only place that decides which database the application talks to.
Everything else asks for the engine or a session factory, so a test run can be pointed
at a database it owns and cannot silently fall through to the development database.

The guard is deliberately strict. During a test run the configured URL must be exactly
the URL the test harness declared, and it raises before any engine is constructed, so a
mistake cannot open a connection to the development database, let alone write to it.
"""
import os

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.config import settings

TEST_DATABASE_URL_ENV = "REPLYAI_TEST_DATABASE_URL"
"""Set by the test harness. The configured URL must match it exactly during tests."""

TEST_RUN_ENV = "REPLYAI_TESTING"
"""Optional explicit marker; pytest also sets PYTEST_CURRENT_TEST during each test."""

_override_engine: AsyncEngine | None = None
_override_sessions: async_sessionmaker | None = None
_created: AsyncEngine | None = None


def in_test_run() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get(TEST_RUN_ENV))


def database_url() -> str:
    """The configured URL, validated against the test harness during a test run."""
    url = settings.database_url.get_secret_value()
    if in_test_run():
        # Fail closed if any likely typo of REPLYAI_TEST_* is present.
        allowed = {TEST_DATABASE_URL_ENV, TEST_RUN_ENV}
        for key in os.environ:
            if key.startswith("REPLYAI_TEST") and key not in allowed:
                raise RuntimeError(
                    "Refusing to run against the configured database during tests: "
                    f"unexpected test-related environment variable '{key}' detected. "
                    "Use only the documented test variables or unset it."
                )
        expected = os.environ.get(TEST_DATABASE_URL_ENV)
        if expected is None:
            raise RuntimeError(
                f"Refusing to run against the configured database during tests: "
                f"{TEST_DATABASE_URL_ENV} is not set. The test harness must declare the "
                "test database before any engine or session is created."
            )
        if url != expected:
            raise RuntimeError(
                "Refusing to run against the development database during tests: the "
                f"configured URL does not match {TEST_DATABASE_URL_ENV}. No engine was "
                "created and no connection was opened."
            )
    return url


def engine() -> AsyncEngine:
    """The engine for the current database. Lazy, so the guard runs before connecting."""
    global _created
    if _override_engine is not None:
        return _override_engine
    if _created is None:
        _created = create_async_engine(
            database_url(), connect_args={"timeout": 2}, pool_pre_ping=True)
    return _created


async def dispose() -> None:
    """Release the engine if one exists.

    Shutdown must never create an engine just to close it, so this is a no-op when
    nothing has connected. That keeps teardown from tripping the test guard in runs
    that legitimately never touch a database.
    """
    if _created is not None:
        await _created.dispose()
    if _override_engine is not None:
        await _override_engine.dispose()


def sessions() -> async_sessionmaker:
    """A session factory for the current database."""
    if _override_sessions is not None:
        return _override_sessions
    return async_sessionmaker(engine(), expire_on_commit=False)


def use_test_database(db_engine: AsyncEngine, factory: async_sessionmaker) -> None:
    """Point the whole application at a database the caller owns. Test harness only."""
    global _override_engine, _override_sessions
    _override_engine = db_engine
    _override_sessions = factory


def reset_test_database() -> None:
    global _created, _override_engine, _override_sessions
    _override_engine = None
    _override_sessions = None
    if _created is not None:
        _created.sync_engine.dispose()
    _created = None
