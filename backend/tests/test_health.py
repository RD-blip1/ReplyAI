from fastapi.testclient import TestClient

from app.main import app
from app.routers import health


def test_health_is_live_without_dependency_connections() -> None:
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_reports_available_dependencies(monkeypatch) -> None:
    async def available() -> bool:
        return True

    monkeypatch.setattr(health, "check_database", available)
    monkeypatch.setattr(health, "check_redis", available)

    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"database": "available", "redis": "available"},
    }


def test_readiness_returns_service_unavailable_without_leaking_connection_details(
    monkeypatch,
) -> None:
    async def available() -> bool:
        return True

    async def unavailable() -> bool:
        return False

    monkeypatch.setattr(health, "check_database", available)
    monkeypatch.setattr(health, "check_redis", unavailable)

    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {"database": "available", "redis": "unavailable"},
    }
    assert "replyai_dev_only" not in response.text