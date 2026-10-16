import asyncio

import pytest


@pytest.fixture(autouse=True, params=["langgraph", "legacy"])
def workflow_engine(request, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "draft_workflow_engine", request.param)
    return request.param


from sqlalchemy import select
from support import QUALITY, generate


def read_evaluation(workspace, draft_id):
    import asyncio
    async def get_eval():
        async with workspace.sessions() as db:
            from sqlalchemy import select

            from app.models.evaluation import DraftEvaluation
            result = await db.execute(select(DraftEvaluation).where(DraftEvaluation.draft_id == draft_id))
            return result.scalars().first()
    return asyncio.run(get_eval())



from app.config import settings
from app.models import PublishingSettings, YouTubeConnection
from app.models.session import utc_now
from app.services import llm, youtube_api


@pytest.fixture
def mocked_publication(workspace, monkeypatch):
    calls = []

    async def credentials(*args):
        return object()

    async def insert(_credentials, parent_comment_id, text):
        calls.append((parent_comment_id, text))
        return "reply-id"

    monkeypatch.setattr("app.routers.publication._credentials", credentials)
    monkeypatch.setattr("app.routers.publication.insert_comment_reply", insert)
    return calls


def select_mode(workspace, mode):
    current = workspace.client.get("/api/publishing-settings").json()
    return workspace.client.put(
        "/api/publishing-settings",
        headers=workspace.headers,
        json={
            "mode": mode,
            "revision": current["revision"],
            "confirm_automation": mode != "manual_review",
        },
    )


def read_publication(workspace, draft_id):
    return workspace.client.get(f"/api/drafts/{draft_id}/publication").json()["publication"]


def test_manual_review_never_publishes_during_generation(workspace, mocked_publication):
    draft = generate(workspace)

    assert mocked_publication == []
    assert draft["human_decision"] == "pending"
    assert read_publication(workspace, draft["id"]) is None


def test_legacy_workflow_fails_closed_when_automation_is_enabled(
    workspace, mocked_publication, monkeypatch
):
    monkeypatch.setattr(settings, "draft_workflow_engine", "legacy")
    assert select_mode(workspace, "approve_all").status_code == 200

    draft = generate(workspace)

    assert draft["triage"] is None
    assert draft["human_decision"] == "pending"
    assert mocked_publication == []
    assert read_publication(workspace, draft["id"]) is None


@pytest.mark.parametrize(
    ("use_graph", "quality_passes"),
    [(False, False), (True, False), (True, True)],
)
def test_manual_eligibility_uses_the_current_quality_review(
    workspace, mocked_publication, monkeypatch, use_graph, quality_passes
):
    if use_graph:
        monkeypatch.setattr(settings, "draft_workflow_engine", "langgraph")
    if not quality_passes:
        original_provider = llm.provider

        def concern_provider(role):
            provider = original_provider(role)
            original_complete = provider.complete

            async def complete(schema, instruction, data):
                if schema is llm.Quality:
                    return schema(**{
                        **QUALITY,
                        "unsupported_claims": "concern",
                        "findings": ["A finding requiring human attention."],
                    })
                return await original_complete(schema, instruction, data)

            provider.complete = complete
            return provider

        monkeypatch.setattr(llm, "provider", concern_provider)

    draft = generate(workspace)
    approved = workspace.client.post(
        f'/api/drafts/{draft["id"]}/decision',
        headers=workspace.headers,
        json={"revision": draft["revision"], "decision": "approved"},
    )
    assert approved.status_code == 200
    assert approved.json()["revision"] == draft["revision"]
    assert approved.json()["human_decision"] == "approved"

    state = workspace.client.get(f'/api/drafts/{draft["id"]}/publication').json()
    if quality_passes:
        assert state["eligibility"]["eligible"] is True
        assert state["eligibility"]["review"]["checks"]["unsupported_claims"] == "pass"
    else:
        assert state["eligibility"]["eligible"] is False
        assert state["eligibility"]["blocked_code"] == "quality_not_passed"
        assert state["eligibility"]["review"]["checks"]["unsupported_claims"] == "concern"
    assert mocked_publication == []


def test_langgraph_uses_only_the_protected_automatic_publication_handoff(
    workspace, mocked_publication, monkeypatch
):
    monkeypatch.setattr(settings, "draft_workflow_engine", "langgraph")
    assert select_mode(workspace, "auto_on_agreement").status_code == 200

    draft = generate(workspace)

    assert mocked_publication == [("comment", draft["text"])]
    assert read_publication(workspace, draft["id"])["status"] == "posted"
    assert draft["human_decision"] == "pending"
    assert all(method == "list" for _, method, _ in workspace.youtube_calls)


def test_langgraph_disagreement_stops_before_publication_handoff(
    workspace, mocked_publication, monkeypatch
):
    monkeypatch.setattr(settings, "draft_workflow_engine", "langgraph")
    original_provider = llm.provider

    def differing_provider(role):
        provider = original_provider(role)
        original_complete = provider.complete

        async def complete(schema, instruction, data):
            if role == "secondary" and schema is llm.Classification:
                return schema(sentiment="negative", intent="complaint")
            return await original_complete(schema, instruction, data)

        provider.complete = complete
        return provider

    monkeypatch.setattr(llm, "provider", differing_provider)
    assert select_mode(workspace, "auto_on_agreement").status_code == 200

    draft = generate(workspace)

    assert draft["agreement"] == "disagree"
    assert draft["human_decision"] == "pending"
    assert mocked_publication == []
    assert read_publication(workspace, draft["id"]) is None


def test_auto_on_agreement_publishes_only_a_fully_cleared_generation(workspace, mocked_publication, workflow_engine):
    if workflow_engine == "legacy":
        pytest.skip("Legacy workflow lacks triage required for automation")
    assert select_mode(workspace, "auto_on_agreement").status_code == 200

    draft = generate(workspace)

    assert mocked_publication == [("comment", draft["text"])]
    assert read_publication(workspace, draft["id"])["status"] == "posted"
    assert draft["human_decision"] == "pending"



@pytest.mark.parametrize("mode", ["auto_on_agreement", "approve_all"])
def test_triage_blocks_automation(workspace, mocked_publication, monkeypatch, mode):
    monkeypatch.setattr(settings, "draft_workflow_engine", "langgraph")
    assert select_mode(workspace, mode).status_code == 200

    original_provider = llm.provider

    def failing_triage_provider(role):
        provider = original_provider(role)
        original_complete = provider.complete

        async def complete(schema, instruction, data):
            if schema is llm.Triage:
                return schema(category="off_topic", reason="spam", confidence=1.0)
            return await original_complete(schema, instruction, data)

        provider.complete = complete
        return provider

    monkeypatch.setattr(llm, "provider", failing_triage_provider)
    draft = generate(workspace)

    assert mocked_publication == []
    assert draft["human_decision"] == "pending"
    assert read_publication(workspace, draft["id"]) is None


@pytest.mark.parametrize("mode", ["auto_on_agreement", "approve_all"])
def test_generic_engagement_requires_human_review(
    workspace, mocked_publication, monkeypatch, mode
):
    monkeypatch.setattr(settings, "draft_workflow_engine", "langgraph")
    original_provider = llm.provider

    def generic_engagement_provider(role):
        provider = original_provider(role)
        original_complete = provider.complete

        async def complete(schema, instruction, data):
            if schema is llm.Triage:
                return schema(
                    category="generic_engagement",
                    reason="The comment is generic praise with no video-specific reference.",
                    confidence=0.99,
                )
            return await original_complete(schema, instruction, data)

        provider.complete = complete
        return provider

    monkeypatch.setattr(llm, "provider", generic_engagement_provider)
    assert select_mode(workspace, mode).status_code == 200

    draft = generate(workspace)

    assert draft["triage"]["category"] == "generic_engagement"
    assert draft["human_decision"] == "pending"
    assert mocked_publication == []
    assert read_publication(workspace, draft["id"]) is None


@pytest.mark.parametrize("mode", ["auto_on_agreement", "approve_all"])
def test_disagreement_keeps_draft_for_human_review(
    workspace, mocked_publication, monkeypatch, mode
):
    monkeypatch.setattr(settings, "draft_workflow_engine", "langgraph")
    original_provider = llm.provider

    def differing_provider(role):
        provider = original_provider(role)
        original_complete = provider.complete

        async def complete(schema, instruction, data):
            if role == "secondary" and schema is llm.Classification:
                return schema(sentiment="negative", intent="complaint")
            return await original_complete(schema, instruction, data)

        provider.complete = complete
        return provider

    monkeypatch.setattr(llm, "provider", differing_provider)
    assert select_mode(workspace, mode).status_code == 200

    draft = generate(workspace)

    assert draft["agreement"] == "disagree"
    assert draft["human_decision"] == "pending"
    assert mocked_publication == []
    assert read_publication(workspace, draft["id"]) is None


@pytest.mark.parametrize("stage_error", ["provider_timeout", "rate_limited"])
def test_missing_or_failed_review_stays_manual(
    workspace, mocked_publication, monkeypatch, stage_error
, workflow_engine):
    if workflow_engine == "legacy":
        pytest.skip("Legacy workflow lacks triage required for automation")
    original_provider = llm.provider

    def failed_quality_provider(role):
        provider = original_provider(role)
        original_complete = provider.complete

        async def complete(schema, instruction, data):
            if schema is llm.Quality:
                raise llm.ProviderFailure(stage_error)
            return await original_complete(schema, instruction, data)

        provider.complete = complete
        return provider

    monkeypatch.setattr(llm, "provider", failed_quality_provider)
    assert select_mode(workspace, "auto_on_agreement").status_code == 200

    draft = generate(workspace)

    assert draft["review_status"] == "failed"
    assert draft["quality"] is None
    assert mocked_publication == []
    assert read_publication(workspace, draft["id"]) is None


def test_low_confidence_and_findings_fall_back_to_manual(
    workspace, mocked_publication, monkeypatch
):
    original_provider = llm.provider

    def uncertain_quality_provider(role):
        provider = original_provider(role)
        original_complete = provider.complete

        async def complete(schema, instruction, data):
            if schema is llm.Quality:
                return schema(**{**QUALITY, "confidence": 0.79})
            return await original_complete(schema, instruction, data)

        provider.complete = complete
        return provider

    monkeypatch.setattr(llm, "provider", uncertain_quality_provider)
    assert select_mode(workspace, "approve_all").status_code == 200

    draft = generate(workspace)

    assert draft["quality"]["confidence"] == 0.79
    assert mocked_publication == []
    assert read_publication(workspace, draft["id"]) is None


def test_approve_all_requires_confirmation_and_classifier_agreement(
    workspace, mocked_publication, monkeypatch
, workflow_engine):
    if workflow_engine == "legacy":
        pytest.skip("Legacy workflow lacks triage required for automation")
    refused = workspace.client.put(
        "/api/publishing-settings",
        headers=workspace.headers,
        json={"mode": "approve_all", "revision": 0, "confirm_automation": False},
    )
    assert refused.status_code == 400

    original_provider = llm.provider

    def differing_provider(role):
        provider = original_provider(role)
        original_complete = provider.complete

        async def complete(schema, instruction, data):
            if role == "secondary" and schema is llm.Classification:
                return schema(sentiment="negative", intent="complaint")
            return await original_complete(schema, instruction, data)

        provider.complete = complete
        return provider

    monkeypatch.setattr(llm, "provider", differing_provider)
    assert select_mode(workspace, "approve_all").status_code == 200
    draft = generate(workspace)

    assert draft["agreement"] == "disagree"
    assert draft["human_decision"] == "pending"
    assert mocked_publication == []
    assert read_publication(workspace, draft["id"]) is None


def test_switching_back_to_manual_stops_future_automatic_actions(workspace, mocked_publication, workflow_engine):
    if workflow_engine == "legacy":
        pytest.skip("Legacy workflow lacks triage required for automation")
    assert select_mode(workspace, "auto_on_agreement").status_code == 200
    first = generate(workspace)
    assert len(mocked_publication) == 1

    assert select_mode(workspace, "manual_review").status_code == 200
    second = generate(workspace, comment="second comment")
    assert len(mocked_publication) == 1
    assert second["human_decision"] == "pending"
    assert read_publication(workspace, second["id"]) is None
    assert read_publication(workspace, first["id"])["status"] == "posted"


def test_auto_publish_failure_with_unknown_youtube_outcome_is_never_retried(
    workspace, mocked_publication, monkeypatch
, workflow_engine):
    if workflow_engine == "legacy":
        pytest.skip("Legacy workflow lacks triage required for automation")
    async def unknown(*args):
        mocked_publication.append(("attempted", "unknown"))
        raise youtube_api.YouTubePublishFailure(
            "publish_outcome_unknown", youtube_api.PUBLISH_UNKNOWN_MESSAGE, 502, "unknown"
        )

    monkeypatch.setattr("app.routers.publication.insert_comment_reply", unknown)
    assert select_mode(workspace, "approve_all").status_code == 200
    draft = generate(workspace)

    attempt = read_publication(workspace, draft["id"])
    assert attempt["status"] == "unknown"
    assert attempt["retryable"] is False
    assert mocked_publication[-1] == ("attempted", "unknown")
    assert len(mocked_publication) == 1


def test_auto_duplicate_guard_survives_regeneration(workspace, mocked_publication, workflow_engine):
    if workflow_engine == "legacy":
        pytest.skip("Legacy workflow lacks triage required for automation")
    assert select_mode(workspace, "approve_all").status_code == 200
    draft = generate(workspace)
    assert len(mocked_publication) == 1

    response = workspace.client.post(
        f"/api/drafts/{draft['id']}/regenerate",
        headers=workspace.headers,
        json={"revision": draft["revision"]},
    )
    assert response.status_code == 200
    assert len(mocked_publication) == 1
    assert read_publication(workspace, draft["id"])["status"] == "posted"


def test_auto_rate_limit_is_durable_and_does_not_call_youtube(workspace, mocked_publication, workflow_engine):
    if workflow_engine == "legacy":
        pytest.skip("Legacy workflow lacks triage required for automation")
    assert select_mode(workspace, "approve_all").status_code == 200

    async def exhaust_limit():
        async with workspace.sessions() as db:
            setting = await db.scalar(select(PublishingSettings))
            setting.auto_publish_count = 10
            setting.auto_publish_window_started_at = utc_now()
            await db.commit()

    asyncio.run(exhaust_limit())
    draft = generate(workspace)

    assert mocked_publication == []
    assert read_publication(workspace, draft["id"]) is None
    ev = read_evaluation(workspace, draft["id"])
    assert ev.decision == "auto_publish_rate_limited"


def test_missing_youtube_permission_records_safe_failure_and_falls_back(
    workspace, mocked_publication, monkeypatch
, workflow_engine):
    if workflow_engine == "legacy":
        pytest.skip("Legacy workflow lacks triage required for automation")
    from app.services.google_oauth import YOUTUBE_READONLY_SCOPE

    assert select_mode(workspace, "approve_all").status_code == 200

    from app.routers import drafts as draft_router

    original_consider = draft_router.consider_automatic_publication

    async def revoke_before_publish(db, session, draft, generation_mode, generation_mode_revision):
        async with workspace.sessions() as other_db:
            connection = await other_db.scalar(select(YouTubeConnection))
            connection.scopes = YOUTUBE_READONLY_SCOPE
            await other_db.commit()
        await original_consider(db, session, draft, generation_mode, generation_mode_revision)

    monkeypatch.setattr(draft_router, "consider_automatic_publication", revoke_before_publish)
    draft = generate(workspace)

    assert mocked_publication == []
    assert read_publication(workspace, draft["id"]) is None
    ev = read_evaluation(workspace, draft["id"])
    assert ev.decision == "insufficient_scope"


def test_expired_youtube_permission_records_failure_without_sending(
    workspace, mocked_publication, monkeypatch
, workflow_engine):
    if workflow_engine == "legacy":
        pytest.skip("Legacy workflow lacks triage required for automation")
    from fastapi import HTTPException

    assert select_mode(workspace, "approve_all").status_code == 200

    async def expired(*args):
        raise HTTPException(
            status_code=401,
            detail={"code": "reconnect_required", "message": "Reconnect YouTube."},
        )

    monkeypatch.setattr("app.routers.publication._credentials", expired)
    draft = generate(workspace)

    assert mocked_publication == []
    assert read_publication(workspace, draft["id"]) is None
    ev = read_evaluation(workspace, draft["id"])
    assert ev.decision == "reconnect_required"


def test_mode_race_after_claim_cancels_before_youtube_insert(
    workspace, mocked_publication, monkeypatch
, workflow_engine):
    if workflow_engine == "legacy":
        pytest.skip("Legacy workflow lacks triage required for automation")
    from app.routers import publication

    assert select_mode(workspace, "approve_all").status_code == 200
    original_claim = publication.claim

    async def switch_mode_after_claim(
        db, draft, *, automated_mode=None, expected_settings_revision=None
    ):
        attempt = await original_claim(
            db,
            draft,
            automated_mode=automated_mode,
            expected_settings_revision=expected_settings_revision,
        )
        async with workspace.sessions() as other_db:
            setting = await other_db.get(PublishingSettings, draft.channel_owner_id)
            setting.mode = "manual_review"
            setting.revision += 1
            await other_db.commit()
        return attempt

    monkeypatch.setattr(publication, "claim", switch_mode_after_claim)
    draft = generate(workspace)

    assert mocked_publication == []
    attempt = read_publication(workspace, draft["id"])
    assert attempt["status"] == "failed"
    assert attempt["failure"]["code"] == "automatic_mode_changed"


def test_enabling_automation_during_generation_does_not_post_that_draft(
    workspace, mocked_publication, monkeypatch
):
    from app.routers import drafts as draft_router

    original_consider = draft_router.consider_automatic_publication

    async def enable_after_generation(
        db, session, draft, generation_mode, generation_mode_revision
    ):
        async with workspace.sessions() as other_db:
            other_db.add(
                PublishingSettings(
                    channel_owner_id=draft.channel_owner_id,
                    mode="approve_all",
                    revision=1,
                )
            )
            await other_db.commit()
        await original_consider(db, session, draft, generation_mode, generation_mode_revision)

    monkeypatch.setattr(draft_router, "consider_automatic_publication", enable_after_generation)
    draft = generate(workspace)

    assert mocked_publication == []
    assert draft["human_decision"] == "pending"
    assert read_publication(workspace, draft["id"]) is None
