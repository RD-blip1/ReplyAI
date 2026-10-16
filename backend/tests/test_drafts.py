import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select, update
from support import CLASS, COMMENT, QUALITY, generate, post

from app.config import settings
from app.main import app
from app.models import AppSession, ReplyDraft, YouTubeConnection
from app.models.session import utc_now
from app.routers.drafts import run_models
from app.services import llm
from app.services import usage as usage_service
from app.services.google_oauth import YOUTUBE_FORCE_SSL_SCOPE


def test_generate_independence_persistence_and_local_decisions(workspace):
    w = workspace
    draft = generate(w)
    assert draft["original_comment"] == COMMENT
    assert draft["agreement"] == "agree"
    assert draft["review_status"] == "complete"
    assert draft["human_decision"] == "pending"
    secondary = [c for c in w.calls if c[1] is llm.Classification]
    assert len(secondary) == 1 and secondary[0][3] == {"comment": COMMENT}
    quality = [c for c in w.calls if c[1] is llm.Quality]
    assert quality[0][3] == {"comment": COMMENT, "reply": draft["text"]}
    assert len(w.calls) == 3
    assert w.client.get(f'/api/drafts/{draft["id"]}').json() == draft
    assert w.client.get('/api/drafts', params={"video_id": "abcdefghijk", "comment_id": "comment"}).json() == draft
    assert post(w, draft, "decision", decision="approved").json()["human_decision"] == "approved"
    assert post(w, draft, "decision", decision="rejected").json()["human_decision"] == "rejected"
    assert all(method == "list" for _, method, _ in w.youtube_calls)
    assert "test-secret" not in str(draft)
    duplicate = w.client.post("/api/drafts", headers=w.headers,
                              json={"video_id": "abcdefghijk", "comment_id": "comment"})
    assert duplicate.status_code == 409 and len(w.calls) == 3


def test_csrf_isolation_and_forged_inputs(workspace):
    w = workspace
    payload = {"video_id": "abcdefghijk", "comment_id": "comment"}
    for headers in ({}, {**w.headers, "Origin": "https://evil.invalid"},
                    {**w.headers, "X-CSRF-Token": "wrong"}):
        assert w.client.post('/api/drafts', headers=headers, json=payload).status_code == 403
    assert w.client.post('/api/drafts', headers=w.headers,
                         json={**payload, "text": "forged", "channel_id": "forged"}).status_code == 422
    draft = generate(w)
    for action, body in (("edit", {"text": "edit"}), ("review", {}),
                         ("regenerate", {}), ("decision", {"decision": "approved"})):
        assert w.client.post(f'/api/drafts/{draft["id"]}/{action}',
                             json={"revision": 1, **body}).status_code == 403
    w.client.cookies.clear()
    assert w.client.get(f'/api/drafts/{draft["id"]}').status_code == 401
    csrf = w.client.get('/api/session').json()["csrf_token"]
    w.headers = {**w.headers, "X-CSRF-Token": csrf}
    assert w.client.get(f'/api/drafts/{draft["id"]}').status_code == 404
    for action, body in (("edit", {"text": "edit"}), ("review", {}),
                         ("regenerate", {}), ("decision", {"decision": "rejected"})):
        assert post(w, draft, action, **body).status_code == 404


@pytest.mark.parametrize("video,comment", [
    ("zyxwvutsrqp", "comment"), ("abcdefghijk", "foreign"),
    ("abcdefghijk", "foreign-channel"), ("abcdefghijk", "reply"),
])
def test_server_ownership(workspace, video, comment):
    w = workspace
    response = w.client.post('/api/drafts', headers=w.headers,
                             json={"video_id": video, "comment_id": comment})
    assert response.status_code == 404
    assert not w.calls


def test_missing_configuration_keeps_youtube_available(workspace, monkeypatch):
    w = workspace
    monkeypatch.setattr(settings, 'primary_llm_api_key', SecretStr(''))
    setup = w.client.get('/api/drafts/setup').json()
    assert setup == {"configured": False, "missing": ["PRIMARY_LLM_API_KEY"], "invalid": []}
    assert w.client.get('/api/youtube/channel').status_code == 200
    response = w.client.post('/api/drafts', headers=w.headers,
                             json={"video_id": "abcdefghijk", "comment_id": "comment"})
    assert response.status_code == 503 and not w.calls


@pytest.mark.parametrize("primary,secondary,expected", [
    (CLASS, CLASS, "agree"),
    (CLASS, {"sentiment": "negative", "intent": "complaint"}, "disagree"),
    (CLASS, {"sentiment": "uncertain", "intent": "praise"}, "uncertain"),
    ({"sentiment": "positive", "intent": "other"}, CLASS, "uncertain"),
    (CLASS, None, "pending"),
])
def test_comparison(primary, secondary, expected):
    assert llm.agreement(primary, secondary) == expected


@pytest.mark.parametrize("broken", [llm.Generation, llm.Classification, llm.Quality])
@pytest.mark.parametrize("invalid", [False, True])
def test_provider_failure_partial_completion(workspace, monkeypatch, broken, invalid):
    w = workspace

    class Failing(w.provider):
        async def complete(self, schema, instruction, data):
            if schema is broken:
                if invalid:
                    return SimpleNamespace(model_dump=lambda: {"invalid": "shape"})
                raise llm.ProviderFailure("provider_unavailable")
            return await super().complete(schema, instruction, data)
    monkeypatch.setattr(llm, "provider", Failing)
    draft = generate(w)
    assert draft["review_status"] == "failed" and draft["human_decision"] == "pending"
    assert draft["failure"]
    assert post(w, draft, "decision", decision="approved").status_code == 409
    if broken is llm.Generation:
        assert not draft["text"] and draft["secondary_classification"] == CLASS
    else:
        assert draft["text"] and draft["primary_classification"] == CLASS
    if broken is llm.Classification:
        assert draft["agreement"] == "pending"
    monkeypatch.setattr(llm, "provider", w.provider)
    if broken is not llm.Generation:
        assert post(w, draft, "review").json()["review_status"] == "complete"


def test_edit_and_regenerate_reset_approval_and_review(workspace, monkeypatch):
    w = workspace
    draft = generate(w)
    assert post(w, draft, "decision", decision="approved").status_code == 200
    edited = post(w, draft, "edit", text="An edited reply").json()
    assert edited["revision"] == 2 and edited["quality"] is None
    assert edited["review_status"] == edited["human_decision"] == "pending"
    assert post(w, edited, "decision", decision="approved").status_code == 409
    assert post(w, draft, "edit", text="Stale edit").status_code == 409
    reviewed = post(w, edited, "review").json()
    assert reviewed["review_status"] == "complete" and reviewed["text"] == "An edited reply"
    assert post(w, reviewed, "decision", decision="approved").status_code == 200

    class FailAll(w.provider):
        async def complete(self, *args):
            raise llm.ProviderFailure("provider_unavailable")
    monkeypatch.setattr(llm, "provider", FailAll)
    regenerated = post(w, reviewed, "regenerate").json()
    assert regenerated["revision"] == 3 and regenerated["text"] == ""
    assert regenerated["human_decision"] == "pending" and regenerated["quality"] is None
    assert regenerated["primary_classification"] is None
    assert regenerated["review_status"] == "failed"


def test_stale_review_cannot_validate_new_revision(workspace, monkeypatch):
    w = workspace
    draft = generate(w)
    started, release = threading.Event(), threading.Event()

    class Slow(w.provider):
        async def complete(self, schema, instruction, data):
            if schema is llm.Quality:
                started.set()
                await asyncio.to_thread(release.wait, 10)
            return await super().complete(schema, instruction, data)
    monkeypatch.setattr(llm, "provider", Slow)
    with ThreadPoolExecutor() as pool:
        running = pool.submit(post, w, draft, "review")
        try:
            assert started.wait(5)
            assert post(w, draft, "review").status_code == 409
            edited = post(w, draft, "edit", text="New revision while review runs").json()
            assert edited["revision"] == 2
        finally:
            release.set()
        assert running.result(timeout=10).status_code == 409
    saved = w.client.get(f'/api/drafts/{draft["id"]}').json()
    assert saved["text"] == "New revision while review runs"
    assert saved["review_status"] == "pending" and saved["quality"] is None
    assert saved["human_decision"] == "pending"


def test_decision_is_blocked_while_a_review_runs(workspace, monkeypatch):
    w = workspace
    draft = generate(w)
    started, release = threading.Event(), threading.Event()

    class Slow(w.provider):
        async def complete(self, schema, instruction, data):
            if schema is llm.Quality:
                started.set()
                await asyncio.to_thread(release.wait, 10)
            return await super().complete(schema, instruction, data)
    monkeypatch.setattr(llm, "provider", Slow)
    with ThreadPoolExecutor() as pool:
        running = pool.submit(post, w, draft, "review")
        try:
            assert started.wait(5)
            for decision in ("approved", "rejected"):
                assert post(w, draft, "decision", decision=decision).status_code == 409
        finally:
            release.set()
        assert running.result(timeout=10).status_code == 200
    saved = w.client.get(f'/api/drafts/{draft["id"]}').json()
    assert saved["review_status"] == "complete" and saved["quality"] == QUALITY
    assert saved["human_decision"] == "pending"


def test_stale_operations_are_reclaimed_but_fresh_ones_are_not(workspace):
    w = workspace
    draft = generate(w)

    async def mark(token, minutes):
        async with w.sessions() as db:
            row = await db.scalar(select(ReplyDraft).where(ReplyDraft.id == draft["id"]))
            row.operation_token = token
            row.review_status = "pending"
            row.quality = None
            row.human_decision = "pending"
            row.updated_at = utc_now() - timedelta(minutes=minutes)
            await db.commit()
        return w.client.get(f'/api/drafts/{draft["id"]}').json()

    fresh = asyncio.run(mark("in-flight", 0))
    assert post(w, fresh, "review").status_code == 409
    assert post(w, fresh, "regenerate").status_code == 409

    abandoned = asyncio.run(mark("interrupted", 7))
    assert abandoned["review_status"] == "pending" and abandoned["quality"] is None
    reclaimed = post(w, abandoned, "review").json()
    assert reclaimed["review_status"] == "complete" and reclaimed["quality"] == QUALITY
    assert post(w, reclaimed, "decision", decision="approved").status_code == 200

    async def saved_token():
        async with w.sessions() as db:
            row = await db.scalar(select(ReplyDraft).where(ReplyDraft.id == draft["id"]))
            return row.operation_token
    assert asyncio.run(saved_token()) is None


def test_review_reruns_quality_for_saved_text_and_clears_decision(workspace):
    w = workspace
    draft = generate(w)
    assert post(w, draft, "decision", decision="approved").status_code == 200

    reviewed = post(w, draft, "review").json()
    assert reviewed["revision"] == draft["revision"]
    assert reviewed["original_comment"] == COMMENT
    assert reviewed["quality"] == QUALITY
    assert reviewed["human_decision"] == "pending"
    assert reviewed["secondary_classification"] == CLASS and reviewed["agreement"] == "agree"
    assert [call[1] for call in w.calls[3:]] == [llm.Quality]
    assert w.calls[-1][3] == {"comment": COMMENT, "reply": draft["text"]}

    edited = post(w, draft, "edit", text="Edited reply text").json()
    assert post(w, draft, "review").status_code == 409
    reviewed_edit = post(w, edited, "review").json()
    assert reviewed_edit["text"] == "Edited reply text"
    assert reviewed_edit["primary_classification"] == CLASS
    assert [call[1] for call in w.calls[4:]] == [llm.Quality]
    assert w.calls[-1][3] == {"comment": COMMENT, "reply": "Edited reply text"}
    assert post(w, reviewed_edit, "decision", decision="approved").status_code == 200


def test_review_reruns_secondary_classification_when_configuration_changes(workspace, monkeypatch):
    w = workspace
    draft = generate(w)
    monkeypatch.setattr(settings, "secondary_llm_base_url", "https://mock.invalid/v2")
    monkeypatch.setattr(settings, "secondary_llm_model", "test-secondary-v2")
    reviewed = post(w, draft, "review").json()
    assert reviewed["providers"]["secondary"] == {
        "base_url": "https://mock.invalid/v2", "model": "test-secondary-v2"}
    assert reviewed["providers"]["primary"] == draft["providers"]["primary"]
    assert reviewed["secondary_classification"] == CLASS
    assert [call[1] for call in w.calls[3:]] == [llm.Classification, llm.Quality]
    assert w.calls[3][3] == {"comment": COMMENT}
    assert w.calls[4][3] == {"comment": COMMENT, "reply": draft["text"]}
    assert "test-secret" not in str(reviewed)


def test_review_requires_saved_text_and_ai_configuration(workspace, monkeypatch):
    w = workspace
    draft = generate(w)

    class FailAll(w.provider):
        async def complete(self, *args):
            raise llm.ProviderFailure("provider_unavailable")
    monkeypatch.setattr(llm, "provider", FailAll)
    empty = post(w, draft, "regenerate").json()
    assert empty["text"] == "" and empty["review_status"] == "failed"
    assert post(w, empty, "review").status_code == 409
    assert post(w, empty, "decision", decision="rejected").status_code == 200
    assert post(w, empty, "decision", decision="approved").status_code == 409

    monkeypatch.setattr(llm, "provider", w.provider)
    monkeypatch.setattr(settings, "secondary_llm_api_key", SecretStr(""))
    stale = post(w, draft, "review")
    assert stale.status_code == 503
    assert stale.json()["detail"]["code"] == "ai_setup_required"


@pytest.mark.parametrize("schema,stage", [
    (llm.Generation, "generation"),
    (llm.Classification, "classification"),
    (llm.Quality, "quality"),
])
def test_provider_timeout_is_reported_per_stage(workspace, monkeypatch, schema, stage):
    w = workspace

    class TimingOut(w.provider):
        async def complete(self, requested, instruction, data):
            if requested is schema:
                raise TimeoutError
            return await super().complete(requested, instruction, data)
    monkeypatch.setattr(llm, "provider", TimingOut)
    draft = generate(w)
    assert draft["failure"][stage] == "provider_timeout"
    assert draft["review_status"] == "failed"
    assert post(w, draft, "decision", decision="approved").status_code == 409
    assert "test-secret" not in str(draft)
    if schema is llm.Generation:
        assert draft["failure"]["quality"] == "generation_required"


def test_invalid_provider_url_blocks_generation_only(workspace, monkeypatch):
    w = workspace
    monkeypatch.setattr(settings, "primary_llm_base_url", "https://user:secret@mock.invalid/v1?k=1")
    setup = w.client.get("/api/drafts/setup").json()
    assert setup == {"configured": False, "missing": [], "invalid": ["PRIMARY_LLM_BASE_URL"]}
    assert "secret" not in str(setup)
    assert w.client.get("/api/youtube/channel").status_code == 200
    response = w.client.post("/api/drafts", headers=w.headers,
                             json={"video_id": "abcdefghijk", "comment_id": "comment"})
    assert response.status_code == 503 and not w.calls


def test_missing_comment_scope_blocks_draft_creation(workspace):
    w = workspace

    async def demote():
        async with w.sessions() as db:
            connection = await db.scalar(select(YouTubeConnection))
            connection.scopes = "https://www.googleapis.com/auth/youtube.readonly"
            await db.commit()
    asyncio.run(demote())
    response = w.client.post("/api/drafts", headers=w.headers,
                             json={"video_id": "abcdefghijk", "comment_id": "comment"})
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "insufficient_scope"
    assert not w.calls
    assert w.client.get("/api/youtube/channel").status_code == 200


def test_failed_lookup_leaves_no_draft_and_preserves_saved_revision(workspace, monkeypatch):
    w = workspace
    owned = False
    requests = []

    async def youtube(_credentials, resource, method, **params):
        requests.append((resource, method))
        assert method == "list", "YouTube write call attempted"
        if resource == "playlistItems":
            return {"items": [{}] if owned else []}
        if resource == "commentThreads":
            return {"items": [{"id": "thread", "snippet": {
                "videoId": "abcdefghijk",
                "topLevelComment": {"id": params["id"]}}}]}
        return {"items": [{"id": params["id"], "snippet": {
            "channelId": "channel", "textOriginal": COMMENT}}]}
    monkeypatch.setattr("app.services.youtube_api.youtube_request", youtube)

    denied = w.client.post("/api/drafts", headers=w.headers,
                           json={"video_id": "abcdefghijk", "comment_id": "comment"})
    assert denied.status_code == 404
    assert denied.json()["detail"]["code"] == "video_unavailable"
    assert not w.calls
    assert ("commentThreads", "list") not in requests
    assert ("comments", "list") not in requests
    assert w.client.get("/api/drafts", params={
        "video_id": "abcdefghijk", "comment_id": "comment"}).json() is None

    owned = True
    draft = generate(w)
    assert len(w.calls) == 3
    owned = False
    assert post(w, draft, "decision", decision="approved").status_code == 200
    blocked = post(w, draft, "regenerate")
    assert blocked.status_code == 404
    saved = w.client.get(f'/api/drafts/{draft["id"]}').json()
    assert saved["revision"] == 1 and saved["text"] == "Thanks for watching!"
    assert saved["human_decision"] == "approved"
    assert len(w.calls) == 3

    owned = True
    assert len(post(w, saved, "regenerate").json()["text"]) > 0


def test_drafts_are_isolated_between_sessions(workspace):
    w = workspace
    draft = generate(w)

    async def session_hashes():
        async with w.sessions() as db:
            return set((await db.execute(select(AppSession.session_hash))).scalars().all())
    known = asyncio.run(session_hashes())

    other = TestClient(app)
    other.get("/api/session")
    other_headers = {"Origin": "http://127.0.0.1:5173",
                     "X-CSRF-Token": other.get("/api/session").json()["csrf_token"]}

    async def connect_other():
        async with w.sessions() as db:
            current = set((await db.execute(select(AppSession.session_hash))).scalars().all())
            db.add(YouTubeConnection(session_hash=(current - known).pop(),
                                    channel_id="other-channel", channel_title="Other",
                                    uploads_playlist_id="uploads", encrypted_tokens="test",
                                    scopes=YOUTUBE_FORCE_SSL_SCOPE))
            await db.commit()
    asyncio.run(connect_other())

    assert other.get(f'/api/drafts/{draft["id"]}').status_code == 404
    assert other.get("/api/drafts", params={
        "video_id": "abcdefghijk", "comment_id": "comment"}).json() is None
    for action, body in (("edit", {"text": "edit"}), ("review", {}), ("regenerate", {}),
                         ("decision", {"decision": "rejected"})):
        assert other.post(f'/api/drafts/{draft["id"]}/{action}', headers=other_headers,
                          json={"revision": draft["revision"], **body}).status_code == 404
    assert other.post("/api/drafts", headers=other_headers,
                      json={"video_id": "abcdefghijk", "comment_id": "comment"}).status_code == 404
    assert other.post("/api/drafts", headers={
        "Origin": "http://127.0.0.1:5173", "X-CSRF-Token": w.headers["X-CSRF-Token"]},
        json={"video_id": "abcdefghijk", "comment_id": "comment"}).status_code == 403
    assert other.get("/api/drafts/setup").json()["configured"] is True
    assert len(w.calls) == 3
    assert w.client.get(f'/api/drafts/{draft["id"]}').json() == draft



def test_token_usage_is_recorded_per_model_and_stage(workspace, monkeypatch):
    w = workspace
    draft = generate(w)
    assert draft["token_usage"] == {
        "generation": {"model": "test-primary", "input_tokens": 40, "output_tokens": 12},
        "classification": {"model": "test-secondary", "input_tokens": 40, "output_tokens": 12},
        "quality": {"model": "test-secondary", "input_tokens": 40, "output_tokens": 12},
    }
    assert w.client.get(f'/api/drafts/{draft["id"]}').json()["token_usage"] == draft["token_usage"]

    assert set(post(w, draft, "review").json()["token_usage"]) == {
        "generation", "classification", "quality"}

    edited = post(w, draft, "edit", text="Edited reply").json()
    assert set(edited["token_usage"]) == {"classification"}
    reviewed = post(w, edited, "review").json()
    assert set(reviewed["token_usage"]) == {"classification", "quality"}
    assert reviewed["token_usage"]["quality"]["model"] == "test-secondary"
    assert post(w, reviewed, "decision", decision="approved").status_code == 200

    regenerated = post(w, reviewed, "regenerate").json()
    assert set(regenerated["token_usage"]) == {"generation", "classification", "quality"}
    assert regenerated["token_usage"]["generation"]["model"] == "test-primary"


def test_missing_provider_usage_is_stored_as_unknown(workspace, monkeypatch):
    w = workspace

    class Silent(w.provider):
        def usage(self):
            return {"model": f"test-{self.role}", "input_tokens": None, "output_tokens": None}
    monkeypatch.setattr(llm, "provider", Silent)
    draft = generate(w)
    assert draft["token_usage"]["generation"] == {
        "model": "test-primary", "input_tokens": None, "output_tokens": None}
    assert draft["review_status"] == "complete"
    assert post(w, draft, "decision", decision="approved").status_code == 200

    class Blind(w.provider):
        def usage(self):
            return None
    monkeypatch.setattr(llm, "provider", Blind)
    second = post(w, draft, "edit", text="Second revision").json()
    assert second["token_usage"] == {
        "classification": {"model": "test-secondary", "input_tokens": None, "output_tokens": None}}
    regenerated = post(w, second, "regenerate").json()
    assert regenerated["token_usage"] == {}
    assert regenerated["review_status"] == "complete"


def test_failed_stage_records_no_usage_and_rate_limit_is_explained(workspace, monkeypatch):
    w = workspace

    class RateLimited(w.provider):
        _served = False

        def usage(self):
            return {"model": f"test-{self.role}", **self.reported} if self._served else None

        def attempts(self):
            return [self.usage()]

        async def complete(self, schema, instruction, data):
            if schema is llm.Quality:
                # Rejected before any response, so the provider reported no counts.
                raise llm.ProviderFailure("rate_limited")
            self._served = True
            return await super().complete(schema, instruction, data)
    monkeypatch.setattr(llm, "provider", RateLimited)
    draft = generate(w)
    assert draft["failure"] == {"quality": "rate_limited"}
    assert set(draft["token_usage"]) == {"generation", "classification"}
    assert draft["review_status"] == "failed" and draft["quality"] is None
    assert post(w, draft, "decision", decision="approved").status_code == 409
    monkeypatch.setattr(llm, "provider", w.provider)
    assert set(post(w, draft, "review").json()["token_usage"]) == {
        "generation", "classification", "quality"}


def usage_rows(w, draft):
    payload = w.client.get(f'/api/drafts/{draft["id"]}/usage').json()
    return payload["entries"], payload["totals"]


def usage_payload(w, draft):
    return w.client.get(f'/api/drafts/{draft["id"]}/usage').json()


def test_usage_ledger_records_every_call_per_operation_stage_role_and_revision(workspace):
    w = workspace
    draft = generate(w)
    entries, totals = usage_rows(w, draft)
    assert [(e["operation"], e["stage"], e["role"], e["revision"]) for e in entries] == [
        ("generate", "generation", "primary", 1),
        ("generate", "classification", "secondary", 1),
        ("generate", "quality", "secondary", 1),
    ]
    assert all(e["model"] and e["input_tokens"] and e["output_tokens"] for e in entries)
    assert all(e["consumption"] == "reported" and e["applied"] for e in entries)
    assert totals == {"requests": 3, "reported_requests": 3, "unknown_requests": 0,
                      "discarded_requests": 0, "input_tokens": 120, "output_tokens": 36}

    draft = post(w, draft, "edit", text="Edited reply").json()
    assert draft["revision"] == 2
    draft = post(w, draft, "regenerate").json()
    assert draft["revision"] == 3
    entries, totals = usage_rows(w, draft)
    regenerated = [e for e in entries if e["operation"] == "regenerate"]
    assert [(e["stage"], e["role"], e["revision"]) for e in regenerated] == [
        ("generation", "primary", 3),
        ("classification", "secondary", 3),
        ("quality", "secondary", 3),
    ]
    assert totals["requests"] == 6
    assert totals["input_tokens"] == 240 and totals["output_tokens"] == 72

    edited = post(w, draft, "edit", text="Second edit").json()
    assert edited["revision"] == 4
    assert usage_rows(w, edited)[1]["requests"] == 6, "editing must not add a provider request"


def test_reused_classification_is_not_counted_as_a_new_call(workspace):
    w = workspace
    draft = generate(w)
    entries, totals = usage_rows(w, draft)
    assert [e["operation"] for e in entries] == ["generate"] * 3

    reviewed = post(w, draft, "review").json()
    entries, totals = usage_rows(w, reviewed)
    assert [(e["operation"], e["stage"]) for e in entries] == [
        ("generate", "generation"), ("generate", "classification"), ("generate", "quality"),
        ("review", "quality"),
    ]
    assert totals["requests"] == 4, "reused classification must not be re-recorded"
    assert reviewed["token_usage"]["classification"] == draft["token_usage"]["classification"]


def test_review_rerun_after_configuration_change_records_one_more_classification(workspace, monkeypatch):
    w = workspace
    draft = generate(w)
    monkeypatch.setattr(llm, "configuration", lambda role: (
        "https://secondary.example.invalid/openai/v1", "test-key", "test-secondary-v2")
        if role == "secondary" else ("https://primary.example.invalid/openai/v1", "k", "p"))
    monkeypatch.setattr(llm, "provider", w.provider)
    reviewed = post(w, draft, "review").json()
    entries, _ = usage_rows(w, reviewed)
    assert [(e["operation"], e["stage"]) for e in entries[-2:]] == [
        ("review", "classification"), ("review", "quality")]
    assert entries[-2]["model"] == "test-secondary"


def test_absent_provider_usage_object_is_not_invented(workspace, monkeypatch):
    """A response with no usage object at all is unknown, not zero."""
    w = workspace

    class Blind(w.provider):
        def usage(self):
            return None

        def attempts(self):
            return [None]
    monkeypatch.setattr(llm, "provider", Blind)
    draft = generate(w)
    entries, totals = usage_rows(w, draft)
    assert len(entries) == 3
    assert all(e["model"] == "" and e["consumption"] == "unknown" for e in entries)
    assert totals["unknown_requests"] == 3 and totals["requests"] == 3


def test_failed_stage_is_ledgered_without_fabricating_counts(workspace, monkeypatch):
    w = workspace

    class RateLimited(w.provider):
        _served = False

        def usage(self):
            return {"model": f"test-{self.role}", **self.reported} if self._served else None

        def attempts(self):
            return [self.usage()]

        async def complete(self, schema, instruction, data):
            if schema is llm.Quality:
                # Rejected before any response, so the provider reported no counts.
                raise llm.ProviderFailure("rate_limited")
            self._served = True
            return await super().complete(schema, instruction, data)
    monkeypatch.setattr(llm, "provider", RateLimited)
    draft = generate(w)
    entries, totals = usage_rows(w, draft)
    assert [(e["stage"], e["input_tokens"], e["output_tokens"]) for e in entries] == [
        ("generation", 40, 12), ("classification", 40, 12), ("quality", None, None)]
    assert totals["requests"] == 3 and totals["reported_requests"] == 2
    assert totals["unknown_requests"] == 1
    assert next(e for e in entries if e["consumption"] == "unknown")["stage"] == "quality"


def test_usage_ledger_is_session_scoped(workspace):
    w = workspace
    draft = generate(w)
    other = TestClient(app)
    other.get("/api/session")
    assert other.get(f'/api/drafts/{draft["id"]}/usage').status_code == 404
    assert w.client.get(f'/api/drafts/{draft["id"]}/usage').json()["totals"]["requests"] == 3


def test_stale_result_changes_nothing_but_keeps_its_reported_usage(workspace, monkeypatch):
    """Consumption is a fact about the provider, not about the draft.

    A superseded operation must not touch draft state, and the requests it already made
    must still be recorded, labelled as discarded and attributed to the revision and
    operation that attempted them.
    """
    w = workspace
    draft = generate(w)
    before = w.client.get(f'/api/drafts/{draft["id"]}').json()
    assert usage_rows(w, draft)[1]["requests"] == 3
    monkeypatch.setattr(llm, "provider", w.provider)

    async def drive():
        async with w.sessions() as db, w.sessions() as other_db:
            row = (await db.execute(
                select(ReplyDraft).where(ReplyDraft.id == draft["id"]))).scalar_one()
            # A concurrent operation claims the draft while this one is in flight, so
            # the token held by ``row`` no longer matches the stored row.
            # This operation claims the draft, then a concurrent operation takes it.
            row.operation_token = "claimed-by-this-operation"
            await db.commit()
            await other_db.execute(update(ReplyDraft).where(ReplyDraft.id == row.id)
                                   .values(operation_token="superseded"))
            await other_db.commit()
            with pytest.raises(HTTPException) as failure:
                await run_models(db, row, generate=True, operation="regenerate")
            assert failure.value.status_code == 409
    asyncio.run(drive())

    # Draft state is untouched: text, classifications, review, and snapshot all stand.
    after = w.client.get(f'/api/drafts/{draft["id"]}').json()
    for field in ("text", "primary_classification", "secondary_classification", "quality",
                  "agreement", "review_status", "generation_status", "human_decision",
                  "token_usage", "revision"):
        assert after[field] == before[field], field
    assert after["token_usage"] == draft["token_usage"]

    # The requests the discarded operation made are still counted, and marked discarded.
    entries, totals = usage_rows(w, draft)
    assert totals["requests"] == 6
    assert totals["discarded_requests"] == 3
    discarded = [e for e in entries if not e["applied"]]
    assert [(e["operation"], e["stage"], e["revision"], e["consumption"]) for e in discarded] == [
        ("regenerate", "generation", 1, "reported"),
        ("regenerate", "classification", 1, "reported"),
        ("regenerate", "quality", 1, "reported"),
    ]
    assert all((e["input_tokens"], e["output_tokens"]) == (40, 12) for e in discarded)
    assert totals["input_tokens"] == 240 and totals["output_tokens"] == 72


def test_accounting_write_is_idempotent_so_a_retry_cannot_double_count(workspace, monkeypatch):
    w = workspace
    draft = generate(w)
    before = usage_rows(w, draft)[1]
    monkeypatch.setattr(llm, "provider", w.provider)

    row = usage_service.entry(
        draft_id=draft["id"], revision=1, operation_token="op-1", operation="review",
        stage="quality", role="secondary", attempt=1,
        reported={"model": "test-secondary", "input_tokens": 7, "output_tokens": 3},
        applied=True)

    async def drive():
        # A persistence retry replays the identical write; it must not add a row.
        await usage_service.record_attempts([row])
        await usage_service.record_attempts([row, dict(row)])
    asyncio.run(drive())

    entries, totals = usage_rows(w, draft)
    assert totals["requests"] == before["requests"] + 1
    assert totals["input_tokens"] == before["input_tokens"] + 7
    assert totals["output_tokens"] == before["output_tokens"] + 3
    assert [e["stage"] for e in entries].count("quality") == 2
    assert [e for e in entries if e["operation"] == "review"] == [{
        "revision": 1, "operation": "review", "stage": "quality", "role": "secondary",
        "attempt": 1, "model": "test-secondary", "input_tokens": 7, "output_tokens": 3,
        "consumption": "reported", "applied": True}]


def test_retry_inside_one_call_is_recorded_as_separate_attempts(workspace, monkeypatch):
    """A single stage can issue several HTTP requests; each is a real attempt."""
    w = workspace

    class Retried(w.provider):
        def __init__(self, role):
            super().__init__(role)
            self._attempts = []

        def usage(self):
            return self._attempts[-1] if self._attempts else None

        def attempts(self):
            return list(self._attempts)

        async def complete(self, schema, instruction, data):
            self._attempts.append(None)  # timed-out request: consumption unknown
            self._attempts.append({"model": f"test-{self.role}", "input_tokens": 40,
                                   "output_tokens": 12})
            return await super().complete(schema, instruction, data)
    monkeypatch.setattr(llm, "provider", Retried)
    draft = generate(w)
    entries, totals = usage_rows(w, draft)
    assert totals["requests"] == 6, "one unknown attempt plus one reported attempt per stage"
    assert [(e["stage"], e["attempt"], e["consumption"]) for e in entries] == [
        ("generation", 1, "unknown"), ("generation", 2, "reported"),
        ("classification", 1, "unknown"), ("classification", 2, "reported"),
        ("quality", 1, "unknown"), ("quality", 2, "reported"),
    ]
    unknown = [e for e in entries if e["consumption"] == "unknown"]
    assert all(e["input_tokens"] is None and e["output_tokens"] is None for e in unknown)
    assert totals["unknown_requests"] == 3 and totals["reported_requests"] == 3
    assert totals["input_tokens"] == 120 and totals["output_tokens"] == 36
    coverage = usage_payload(w, draft)["coverage"]
    assert "cannot be said whether the provider processed or billed them" in coverage["unknown_notice"]


def test_usage_is_captured_when_response_validation_fails(workspace, monkeypatch):
    """A response that arrived and was billed still counts, even if unusable."""
    w = workspace

    class Unusable(w.provider):
        def attempts(self):
            return [{"model": "test-secondary", "input_tokens": 40, "output_tokens": 12}]

        async def complete(self, schema, instruction, data):
            if schema is llm.Quality:
                return schema.model_construct(relevance="nonsense")
            return await super().complete(schema, instruction, data)
    monkeypatch.setattr(llm, "provider", Unusable)
    draft = generate(w)
    assert draft["failure"]["quality"] == "invalid_response"
    entries, totals = usage_rows(w, draft)
    quality = next(e for e in entries if e["stage"] == "quality")
    assert (quality["consumption"], quality["input_tokens"], quality["output_tokens"]) == (
        "reported", 40, 12)
    assert totals["unknown_requests"] == 0 and totals["input_tokens"] == 120


def test_unknown_consumption_stays_unknown_and_is_not_claimed_as_billed(workspace, monkeypatch):
    w = workspace

    class Unreachable(w.provider):
        def usage(self):
            return None

        def attempts(self):
            return [None]
    monkeypatch.setattr(llm, "provider", Unreachable)
    draft = generate(w)
    entries, totals = usage_rows(w, draft)
    assert len(entries) == 3
    assert all(e["consumption"] == "unknown" and e["model"] == "" for e in entries)
    assert all(e["input_tokens"] is None and e["output_tokens"] is None for e in entries)
    assert totals["unknown_requests"] == 3 and totals["reported_requests"] == 0
    # Absent counts are not recorded as zero.
    assert totals["input_tokens"] == 0 and totals["output_tokens"] == 0
    notice = usage_payload(w, draft)["coverage"]["unknown_notice"]
    assert "not" in notice and "billed" in notice
    assert "zero" not in notice


def test_pre_ledger_history_is_reported_as_incomplete(workspace, monkeypatch):
    w = workspace
    draft = generate(w)
    payload = usage_payload(w, draft)
    assert payload["coverage"] == {
        "complete": True, "pre_ledger_activity": False, "unbacked_snapshot_stages": [],
        "notice": None, "unknown_notice": None}

    # A draft whose activity predates the ledger keeps only its stage snapshot.
    monkeypatch.setattr(usage_service, "USAGE_TRACKING_STARTED_AT",
                        datetime(2030, 1, 1, tzinfo=UTC))
    payload = usage_payload(w, draft)
    assert payload["coverage"]["complete"] is False
    assert payload["coverage"]["pre_ledger_activity"] is True
    assert payload["coverage"]["notice"] == (
        "Recorded usage since tracking began; earlier usage is unavailable.")
    # The totals are still returned, but never labelled lifetime totals.
    assert payload["totals"]["requests"] == 3
    assert "lifetime" not in json.dumps(payload).lower()



def test_accounting_cannot_commit_someone_elses_pending_draft_changes(workspace):
    """Accounting owns its session, so it cannot flush unrelated pending work."""
    w = workspace
    draft = generate(w)
    before = usage_rows(w, draft)[1]["requests"]

    async def drive():
        async with w.sessions() as db:
            row = await db.get(ReplyDraft, draft["id"])
            row.text = "an edit nobody committed"  # pending, never flushed
            assert row.text == "an edit nobody committed"
            await usage_service.record_attempts([usage_service.entry(
                draft_id=draft["id"], revision=1, operation_token="op-pending",
                operation="review", stage="quality", role="secondary", attempt=1,
                reported={"model": "test-secondary", "input_tokens": 1, "output_tokens": 1},
                applied=True)])
            # The accounting commit did not drag the pending edit in with it.
            await db.rollback()
    asyncio.run(drive())

    assert w.client.get(f'/api/drafts/{draft["id"]}').json()["text"] == draft["text"]
    _entries, totals = usage_rows(w, draft)
    assert totals["requests"] == before + 1, "the accounting row was still recorded"


def test_draft_rollback_cannot_erase_recorded_attempts(workspace):
    """A discarded draft transaction leaves the provider attempts on record.

    ``run_models`` settles the guarded draft transaction first and only then records
    accounting, and that ordering is part of the contract: the accounting session is
    separate, so it holds no lock the caller needs and the caller's rollback cannot
    reach rows this session already committed.
    """
    w = workspace
    draft = generate(w)
    before = usage_rows(w, draft)[1]

    async def drive():
        async with w.sessions() as db:
            row = await db.get(ReplyDraft, draft["id"])
            row.text = "draft change that gets rolled back"
            await db.flush()
            await db.rollback()  # the draft change is erased, as a stale guard would
            await usage_service.record_attempts([usage_service.entry(
                draft_id=draft["id"], revision=1, operation_token="op-rolled-back",
                operation="regenerate", stage="generation", role="primary", attempt=1,
                reported={"model": "test-primary", "input_tokens": 9, "output_tokens": 4},
                applied=False)])
    asyncio.run(drive())

    assert w.client.get(f'/api/drafts/{draft["id"]}').json()["text"] == draft["text"]
    entries, totals = usage_rows(w, draft)
    assert totals["requests"] == before["requests"] + 1
    recorded = next(e for e in entries if e["operation"] == "regenerate")
    assert (recorded["input_tokens"], recorded["output_tokens"], recorded["applied"]) == (
        9, 4, False)
    assert totals["input_tokens"] == before["input_tokens"] + 9
    assert totals["output_tokens"] == before["output_tokens"] + 4
    assert totals["output_tokens"] == before["output_tokens"] + 4
