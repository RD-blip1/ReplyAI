import asyncio

import pytest
from sqlalchemy import select
from support import QUALITY, generate

from app import config
from app.models import ReplyPublication
from app.models.evaluation import DraftEvaluation
from app.services import llm


@pytest.fixture(autouse=True)
def set_langgraph_mode(monkeypatch):
    monkeypatch.setattr(config.settings, "draft_workflow_engine", "langgraph")


@pytest.fixture
def mocked_publication(monkeypatch):
    calls = []
    
    async def credentials(*args):
        return object()

    async def mock_insert(_credentials, parent_comment_id, text):
        calls.append((parent_comment_id, text))
        return "mock_youtube_comment_id"

    monkeypatch.setattr("app.routers.publication._credentials", credentials)
    monkeypatch.setattr("app.routers.publication.insert_comment_reply", mock_insert)
    monkeypatch.setattr("app.services.youtube_api.insert_comment_reply", mock_insert)
    return calls


def select_mode(workspace, mode):
    return workspace.client.put("/api/publishing-settings", headers=workspace.headers, json={"mode": mode, "revision": 0, "confirm_automation": True})


def failing_triage_provider(category):
    class MyFakeProvider:
        def __init__(self, role):
            self.role = role
            self.reported = {"input_tokens": 40, "output_tokens": 12}
        def usage(self):
            return {"model": f"test-{self.role}", **self.reported}
        def attempts(self):
            return [self.usage()]
        async def complete(self, schema, instruction, data):
            from support import CLASS
            if schema.__name__ == 'Generation':
                return schema(classification=CLASS, reply="Thanks for watching!")
            if schema.__name__ == 'Classification':
                return schema(**CLASS)
            if schema.__name__ == 'Triage':
                cat = category
                return schema(category=cat, reason="Mocked failure", confidence=1.0)
            if schema.__name__ == "Refinement":
                return schema(reply="Refined warmly.")
            return schema(**QUALITY)
    return MyFakeProvider

def read_evaluation(workspace, draft_id):
    async def get_eval():
        async with workspace.sessions() as db:
            result = await db.execute(select(DraftEvaluation).where(DraftEvaluation.draft_id == draft_id))
            return result.scalars().first()
    return asyncio.run(get_eval())


def read_publications(workspace, draft_id):
    async def get_publications():
        async with workspace.sessions() as db:
            result = await db.execute(
                select(ReplyPublication).where(ReplyPublication.draft_id == draft_id)
            )
            return result.scalars().all()

    return asyncio.run(get_publications())

def test_dry_run_fails_missing_auth(workspace, mocked_publication, monkeypatch):
    assert select_mode(workspace, "dry_run").status_code == 200
    
    # Mock YOUTUBE_FORCE_SSL_SCOPE ONLY in publication.py so that the draft generation
    # succeeds (using the real scope in drafts.py), but the publishing step thinks the
    # required scope is missing.
    monkeypatch.setattr("app.routers.publication.YOUTUBE_FORCE_SSL_SCOPE", "some_impossible_scope")
    
    draft = generate(workspace)
    assert mocked_publication == []
    ev = read_evaluation(workspace, draft["id"])
    assert ev is not None
    assert ev.decision == "missing_auth"

def test_dry_run_fails_expired_auth(workspace, mocked_publication, monkeypatch):
    assert select_mode(workspace, "dry_run").status_code == 200
    from fastapi import HTTPException
    async def expired(*args, **kwargs):
        raise HTTPException(
            status_code=401,
            detail={"code": "reconnect_required", "message": "Reconnect YouTube."},
        )
    monkeypatch.setattr("app.routers.publication._credentials", expired)
    draft = generate(workspace)
    assert mocked_publication == []
    ev = read_evaluation(workspace, draft["id"])
    assert ev is not None
    assert ev.decision == "reconnect_required"

def test_dry_run_fails_missing_connection(workspace, mocked_publication, monkeypatch):
    assert select_mode(workspace, "dry_run").status_code == 200
    from fastapi import HTTPException
    async def fake_owned_connection(*args, **kwargs):
        raise HTTPException(
            status_code=404,
            detail={"code": "youtube_not_connected", "message": "Connect a YouTube channel first."},
        )
    monkeypatch.setattr("app.routers.publication._owned_connection", fake_owned_connection)
    draft = generate(workspace)
    assert mocked_publication == []
    monkeypatch.undo()
    ev = read_evaluation(workspace, draft["id"])
    assert ev is not None
    assert ev.decision == "youtube_not_connected"

def test_dry_run_skipped_off_topic(workspace, mocked_publication, monkeypatch):
    assert select_mode(workspace, "dry_run").status_code == 200
    monkeypatch.setattr(llm, "provider", failing_triage_provider("off_topic"))
    draft = generate(workspace)
    assert mocked_publication == []
    ev = read_evaluation(workspace, draft["id"])
    assert ev is not None
    assert ev.decision == "skipped_off_topic"

def test_dry_run_generic_engagement_requires_human_review(
    workspace, mocked_publication, monkeypatch
):
    assert select_mode(workspace, "dry_run").status_code == 200
    monkeypatch.setattr(llm, "provider", failing_triage_provider("generic_engagement"))

    draft = generate(workspace)

    assert mocked_publication == []
    evaluation = read_evaluation(workspace, draft["id"])
    assert evaluation is not None
    assert evaluation.decision == "needs_human_review"
    assert "Generic engagement needs human review" in evaluation.reason
    assert read_publications(workspace, draft["id"]) == []

def test_dry_run_skipped_spam(workspace, mocked_publication, monkeypatch):
    assert select_mode(workspace, "dry_run").status_code == 200
    monkeypatch.setattr(llm, "provider", failing_triage_provider("spam_or_suspicious"))
    draft = generate(workspace)
    assert mocked_publication == []
    ev = read_evaluation(workspace, draft["id"])
    assert ev is not None
    assert ev.decision == "skipped_spam"

def test_dry_run_skipped_harassment(workspace, mocked_publication, monkeypatch):
    assert select_mode(workspace, "dry_run").status_code == 200
    monkeypatch.setattr(llm, "provider", failing_triage_provider("harassment_or_abuse"))
    draft = generate(workspace)
    assert mocked_publication == []
    ev = read_evaluation(workspace, draft["id"])
    assert ev is not None
    assert ev.decision == "skipped_harassment"

def test_dry_run_skipped_unsupported_language(workspace, mocked_publication, monkeypatch):
    assert select_mode(workspace, "dry_run").status_code == 200
    monkeypatch.setattr(llm, "provider", failing_triage_provider("unsupported_language"))
    draft = generate(workspace)
    assert mocked_publication == []
    ev = read_evaluation(workspace, draft["id"])
    assert ev is not None
    assert ev.decision == "skipped_unsupported_language"

def test_dry_run_skipped_requires_human(workspace, mocked_publication, monkeypatch):
    assert select_mode(workspace, "dry_run").status_code == 200
    monkeypatch.setattr(llm, "provider", failing_triage_provider("requires_human_attention"))
    draft = generate(workspace)
    assert mocked_publication == []
    ev = read_evaluation(workspace, draft["id"])
    assert ev is not None
    assert ev.decision == "skipped_requires_human", f"{ev.decision}: {ev.reason}"

def test_dry_run_fails_empty_draft(workspace, mocked_publication, monkeypatch):
    assert select_mode(workspace, "dry_run").status_code == 200
    original_provider = llm.provider
    def empty_provider(role):
            provider = original_provider(role)
            original_complete = provider.complete
            async def complete(schema, instruction, data):
                if schema is llm.Quality:
                    return schema(relevance="concern", unsupported_claims="pass", inappropriate_content="pass", confidence=0.9, findings=["Empty reply"])
                return await original_complete(schema, instruction, data)
            provider.complete = complete
            return provider

    monkeypatch.setattr(llm, "provider", empty_provider)
    draft = generate(workspace)
    assert mocked_publication == []
    ev = read_evaluation(workspace, draft["id"])
    assert ev is not None
    assert ev.decision == "blocked_quality"

def test_dry_run_fails_placeholder_draft(workspace, mocked_publication, monkeypatch):
    assert select_mode(workspace, "dry_run").status_code == 200
    original_provider = llm.provider
    def placeholder_provider(role):
            provider = original_provider(role)
            original_complete = provider.complete
            async def complete(schema, instruction, data):
                if schema is llm.Quality:
                    return schema(relevance="pass", unsupported_claims="concern", inappropriate_content="pass", confidence=0.9, findings=["Placeholder text"])
                return await original_complete(schema, instruction, data)
            provider.complete = complete
            return provider

    monkeypatch.setattr(llm, "provider", placeholder_provider)
    draft = generate(workspace)
    assert mocked_publication == []
    ev = read_evaluation(workspace, draft["id"])
    assert ev is not None
    assert ev.decision == "blocked_quality"

def test_dry_run_success(workspace, mocked_publication, monkeypatch):
    assert select_mode(workspace, "dry_run").status_code == 200
    draft = generate(workspace)
    assert mocked_publication == []
    ev = read_evaluation(workspace, draft["id"])
    assert ev is not None
    assert ev.decision == "would_publish", f"{ev.decision}: {ev.reason}"
    assert read_publications(workspace, draft["id"]) == []

def test_auto_succeeds(workspace, mocked_publication, monkeypatch):
    assert select_mode(workspace, "approve_all").status_code == 200
    draft = generate(workspace)
    assert len(mocked_publication) == 1
    
    # Assert real publication was created
    import asyncio
    async def read_pub():
        async with workspace.sessions() as db:
            return (await db.execute(select(ReplyPublication).where(ReplyPublication.draft_id == draft["id"]))).scalars().first()
    pub = asyncio.run(read_pub())
    assert pub is not None
    assert pub.status == "posted"

def test_dry_run_state_preservation_no_claim(workspace, mocked_publication, monkeypatch):
    assert select_mode(workspace, "dry_run").status_code == 200
    draft = generate(workspace)
    
    # Verify draft remains unclaimed
    import asyncio
    async def get_tokens():
        async with workspace.sessions() as db:
            from app.models.draft import ReplyDraft
            d = await db.get(ReplyDraft, draft['id'])
            return d.publication_token, d.operation_token

    pub_token, op_token = asyncio.run(get_tokens())
    assert pub_token is None
    assert op_token is None

def test_dry_run_state_preservation_no_youtube_api(workspace, mocked_publication, monkeypatch):
    assert select_mode(workspace, "dry_run").status_code == 200
    draft = generate(workspace)
    
    # Comment/video retrieval uses read-only list calls; no publication insert is made.
    assert mocked_publication == []
    assert all(method == "list" for _, method, _ in workspace.youtube_calls)
    assert read_publications(workspace, draft["id"]) == []

def test_legacy_mode_fallback(workspace, mocked_publication, monkeypatch):
    monkeypatch.setenv("DRAFT_WORKFLOW_ENGINE", "legacy")
    from app import config
    monkeypatch.setattr(config.settings, "draft_workflow_engine", "legacy")
    assert select_mode(workspace, "dry_run").status_code == 200
    draft = generate(workspace)
    assert mocked_publication == []
    ev = read_evaluation(workspace, draft["id"])
    # In legacy mode, we don't have the context/policy evaluations.
    # It just won't run auto publishing. Thus no evaluation is recorded.
    assert ev is not None
    assert ev.decision == "blocked_policy"
    assert "Contextual triage is required" in ev.reason
    assert read_publications(workspace, draft["id"]) == []
