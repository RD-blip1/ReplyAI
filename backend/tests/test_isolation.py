"""The test suite must never be able to reach the development database.

These tests assert the guard itself, and they never open a connection to anything:
each one replaces engine construction with a tripwire, so a failure would be raised by
the guard before a driver, socket, or statement could exist.
"""
import pytest
from pydantic import SecretStr

from app import database
from app.config import settings
from app.services import usage as usage_service


@pytest.fixture
def tripwire(monkeypatch):
    """Fail loudly if anything tries to build an engine while the guard is under test."""
    def forbidden(*args, **kwargs):
        raise AssertionError("an engine was constructed despite the isolation guard")

    monkeypatch.setattr(database, "create_async_engine", forbidden)
    monkeypatch.setattr(database, "_override_engine", None)
    monkeypatch.setattr(database, "_override_sessions", None)
    monkeypatch.setattr(database, "_created", None)
    return forbidden


def test_development_database_is_refused_during_a_test_run(tripwire, monkeypatch):
    monkeypatch.delenv(database.TEST_DATABASE_URL_ENV, raising=False)
    monkeypatch.setenv(database.TEST_RUN_ENV, "1")
    monkeypatch.setattr(settings, "database_url",
                        SecretStr("postgresql+asyncpg://replyai@127.0.0.1:5432/replyai"))
    with pytest.raises(RuntimeError, match="Refusing to run against the configured database"):
        database.database_url()
    with pytest.raises(RuntimeError):
        database.engine()
    with pytest.raises(RuntimeError):
        database.sessions()


def test_declared_test_database_must_match_the_configured_url(tripwire, monkeypatch):
    monkeypatch.setenv(database.TEST_RUN_ENV, "1")
    monkeypatch.setenv(database.TEST_DATABASE_URL_ENV, "sqlite+aiosqlite:///tmp/owned.db")
    monkeypatch.setattr(settings, "database_url",
                        SecretStr("postgresql+asyncpg://replyai@127.0.0.1:5432/replyai"))
    with pytest.raises(RuntimeError, match="does not match"):
        database.database_url()


def test_pointing_settings_at_a_test_file_alone_does_not_bypass_the_guard(tripwire, monkeypatch):
    """Merely changing the URL is not enough; the harness must declare the database."""
    monkeypatch.delenv(database.TEST_DATABASE_URL_ENV, raising=False)
    monkeypatch.setenv(database.TEST_RUN_ENV, "1")
    monkeypatch.setattr(settings, "database_url", SecretStr("sqlite+aiosqlite:///tmp/other.db"))
    with pytest.raises(RuntimeError, match="is not set"):
        database.database_url()


def test_outside_a_test_run_the_configured_url_is_returned(tripwire, monkeypatch):
    monkeypatch.delenv(database.TEST_RUN_ENV, raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(settings, "database_url", SecretStr("postgresql+asyncpg://x/y"))
    assert database.database_url() == "postgresql+asyncpg://x/y"


def test_pytest_marker_alone_triggers_the_guard(tripwire, monkeypatch):
    """Pytest sets PYTEST_CURRENT_TEST, so the guard works even without the opt-in flag."""
    monkeypatch.delenv(database.TEST_RUN_ENV, raising=False)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_x (call)")
    monkeypatch.delenv(database.TEST_DATABASE_URL_ENV, raising=False)
    monkeypatch.setattr(settings, "database_url", SecretStr("postgresql+asyncpg://x/y"))
    assert database.in_test_run() is True
    with pytest.raises(RuntimeError):
        database.database_url()


def test_accounting_has_no_private_path_to_a_database():
    """Accounting resolves sessions through the guarded factory, like everything else."""
    assert usage_service.sessions is database.sessions
    assert not hasattr(database, "SessionLocal"), "a second, unguarded factory still exists"
    assert not hasattr(usage_service, "SessionLocal")


def test_registered_test_database_is_used_for_every_session(api_client):
    """Inside a running test, request and accounting sessions resolve to the owned database."""
    _client, sessions, url = api_client
    assert database.sessions() is sessions
    assert database.database_url() == url
    assert "test" in url
