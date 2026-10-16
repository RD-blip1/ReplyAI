import asyncio
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from support import CLASS, COMMENT, QUALITY

from app import database
from app.config import settings
from app.main import app
from app.models import AppSession, Base, ChannelOwner, YouTubeConnection
from app.services import llm
from app.services.google_oauth import YOUTUBE_FORCE_SSL_SCOPE


@pytest.fixture
def api_client(
    tmp_path, monkeypatch
) -> Iterator[tuple[TestClient, async_sessionmaker, str]]:
    """Point the whole application, not just request handling, at a database we own.

    The test database is registered in ``app.database`` rather than injected through a
    FastAPI dependency override, so every session the application creates -- request
    sessions, accounting sessions, the readiness probe, anything added later -- resolves
    to the same temporary database. A dependency override only covered the request path
    and let other code reach the development database unnoticed.
    """
    url = f"sqlite+aiosqlite:///{Path(tmp_path) / 'replyai-test.db'}"
    monkeypatch.setenv(database.TEST_DATABASE_URL_ENV, url)
    monkeypatch.setenv(database.TEST_RUN_ENV, "1")
    monkeypatch.setattr(settings, "database_url", SecretStr(url))
    test_engine = create_async_engine(url, connect_args={"timeout": 2}, poolclass=NullPool)

    @event.listens_for(test_engine.sync_engine, "connect")
    def _enforce_foreign_keys(dbapi_connection, _record):
        # SQLite ignores ON DELETE CASCADE unless asked, which would let these tests
        # pass while the cascade that destroys retained work never actually ran.
        dbapi_connection.execute("PRAGMA foreign_keys=ON")
    test_sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    database.use_test_database(test_engine, test_sessions)

    async def create_schema() -> None:
        async with test_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    asyncio.run(create_schema())
    try:
        with TestClient(app, follow_redirects=False) as client:
            yield client, test_sessions, url
    finally:
        database.reset_test_database()
        asyncio.run(test_engine.dispose())


@pytest.fixture
def workspace(api_client, monkeypatch):
    client, sessions, database_url = api_client
    for role in ("primary", "secondary"):
        monkeypatch.setattr(settings, f"{role}_llm_base_url", "https://mock.invalid/v1")
        monkeypatch.setattr(settings, f"{role}_llm_api_key", SecretStr("test-secret"))
        monkeypatch.setattr(settings, f"{role}_llm_model", f"test-{role}")
    csrf = client.get("/api/session").json()["csrf_token"]
    headers = {"Origin": "http://127.0.0.1:5173", "X-CSRF-Token": csrf}

    async def connect():
        async with sessions() as db:
            session = await db.scalar(select(AppSession))
            db.add(ChannelOwner(channel_id="channel"))
            db.add(YouTubeConnection(session_hash=session.session_hash,
                                    channel_id="channel", channel_title="Test",
                                    uploads_playlist_id="uploads", encrypted_tokens="test",
                                    scopes=YOUTUBE_FORCE_SSL_SCOPE))
            await db.commit()
    asyncio.run(connect())

    async def credentials(*args):
        return object()
    monkeypatch.setattr("app.routers.drafts._credentials", credentials)
    youtube_calls = []

    async def youtube(_credentials, resource, method, **params):
        youtube_calls.append((resource, method, params))
        assert method == "list", "YouTube write call attempted"
        if resource == "playlistItems":
            return {"items": [{}] if params["videoId"] == "abcdefghijk" else []}
        if resource == "commentThreads":
            # Realistic: the thread carries videoId, and only a reply is not the
            # thread's top-level comment.
            comment_id = params["id"]
            return {"items": [{"id": "thread", "snippet": {
                "videoId": "wrong_video" if comment_id == "foreign" else "abcdefghijk",
                "topLevelComment": {"id": "thread-other" if comment_id == "reply" else comment_id},
            }}]}
        assert resource == "comments"
        # Realistic: an id-filtered comments.list omits videoId entirely.
        return {"items": [{"id": params["id"], "snippet": {
            "channelId": "foreign" if params["id"] == "foreign-channel" else "channel",
            **({"parentId": "parent"} if params["id"] == "reply" else {}),
            "textOriginal": COMMENT,
        }}]}

    async def mock_get_video_context(_credentials, video_id):
        return {"title": "Test Video Title", "description": "Test Video Description", "channel_title": "Test Channel"}
    monkeypatch.setattr("app.services.video_context.get_video_context", mock_get_video_context)
    monkeypatch.setattr("app.services.youtube_api.youtube_request", youtube)
    calls = []

    class FakeProvider:
        def __init__(self, role):
            self.role = role
            self.reported = {"input_tokens": 40, "output_tokens": 12}

        def usage(self):
            return {"model": f"test-{self.role}", **self.reported}

        def attempts(self):
            return [self.usage()]

        async def complete(self, schema, instruction, data):
            calls.append((self.role, schema, instruction, data))
            if schema is llm.Generation:
                return schema(classification=CLASS, reply="Thanks for watching!")
            if schema is llm.Classification:
                return schema(**CLASS)
            if schema is llm.Triage:
                return schema(category="on_topic", reason="Test reason", confidence=1.0)
            if schema is llm.Refinement:
                # Deliberately different from the saved reply, so an apply is
                # observably a change rather than an identical-text no-op.
                return schema(reply=f"{data['current_saved_reply']} Refined warmly.")
            return schema(**QUALITY)
    monkeypatch.setattr(llm, "provider", FakeProvider)
    return SimpleNamespace(client=client, sessions=sessions, headers=headers,
                           calls=calls, youtube_calls=youtube_calls, provider=FakeProvider,
                           database_url=database_url)
