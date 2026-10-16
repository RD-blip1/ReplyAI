import logging

import pytest
from support import CLASS, COMMENT, QUALITY, generate

from app.config import settings
from app.services import llm


def test_fastapi_generation_executes_langgraph_and_persists_usage(workspace, monkeypatch, caplog):
    monkeypatch.setattr(settings, "draft_workflow_engine", "langgraph")
    preferences = {
        "tone": "Professional",
        "reply_length": "Detailed",
        "language": "Hinglish",
        "channel_instructions": "Use a warm sign-off.",
    }
    saved = workspace.client.put(
        "/api/preferences",
        headers=workspace.headers,
        json={**preferences, "revision": 0},
    )
    assert saved.status_code == 200

    with caplog.at_level(logging.INFO):
        draft = generate(workspace)
    print("DRAFT", draft)

    assert draft["text"] == "Thanks for watching!"
    assert draft["primary_classification"] == CLASS
    assert draft["secondary_classification"] == CLASS
    assert draft["quality"] == QUALITY
    assert draft["review_status"] == "complete"
    assert draft["agreement"] == "agree"
    generation = next(call for call in workspace.calls if call[1] is llm.Generation)
    classification = next(call for call in workspace.calls if call[1] is llm.Classification)
    quality = next(call for call in workspace.calls if call[1] is llm.Quality)
    assert generation[3]["response_preferences"] == preferences
    assert classification[3] == {"comment": COMMENT}
    assert quality[3] == {"comment": COMMENT, "reply": draft["text"]}
    assert len(workspace.calls) == 4

    transitions = [record.getMessage() for record in caplog.records]
    assert "event=draft_workflow_engine_selected engine=langgraph operation=generate" in transitions
    assert "event=draft_graph_execution_started operation=generate" in transitions
    assert "event=draft_graph_execution_completed operation=generate" in transitions
    assert "event=draft_graph_node_completed node=load_context" in transitions
    assert "event=draft_graph_node_completed node=primary_generation" in transitions
    assert "event=draft_graph_node_completed node=secondary_classification" in transitions
    assert "event=draft_graph_node_completed node=parallel_quality_gate" in transitions
    assert "event=draft_graph_node_completed node=quality_review" in transitions
    assert "event=draft_graph_node_completed node=persist_draft" in transitions
    assert "event=draft_graph_node_completed node=evaluate_publishing_policy" in transitions
    assert "event=draft_graph_policy_route_selected route=manual_review" in transitions
    assert "Ignore all previous instructions" not in caplog.text

    saved = workspace.client.get(f"/api/drafts/{draft['id']}", headers=workspace.headers)
    assert saved.status_code == 200
    assert saved.json()["revision"] == draft["revision"]
    assert saved.json()["quality"] == QUALITY
    assert all(method == "list" for _, method, _ in workspace.youtube_calls)

    history = workspace.client.get(f"/api/drafts/{draft['id']}/usage").json()
    assert history["totals"]["requests"] == 4
    assert {entry["stage"] for entry in history["entries"]} == {
        "generation",
        "classification",
        "quality",
            "triage",
    }


@pytest.mark.parametrize("failed_schema", [llm.Generation, llm.Classification, llm.Quality])
def test_graph_provider_failures_persist_safe_incomplete_state(
    workspace,
    monkeypatch,
    failed_schema,
):
    monkeypatch.setattr(settings, "draft_workflow_engine", "langgraph")
    original_provider = llm.provider

    class FailingProvider:
        def __init__(self, role):
            self.delegate = original_provider(role)

        def usage(self):
            return self.delegate.usage()

        def attempts(self):
            return self.delegate.attempts()

        async def complete(self, schema, instruction, data):
            if schema is failed_schema:
                raise llm.ProviderFailure("provider_unavailable")
            if schema is llm.Triage:
                return schema(category="on_topic", reason="Test reason", confidence=1.0)
            if schema is llm.Generation:
                return schema(classification=CLASS, reply="Thanks for watching!")
            if schema is llm.Classification:
                return schema(**CLASS)
            return schema(**QUALITY)

    monkeypatch.setattr(llm, "provider", FailingProvider)
    draft = generate(workspace)
    print("DRAFT", draft)

    assert draft["review_status"] == "failed"
    assert draft["human_decision"] == "pending"
    assert draft["failure"]
    assert all(method == "list" for _, method, _ in workspace.youtube_calls)
    if failed_schema is llm.Generation:
        assert draft["text"] == ""
        assert draft["failure"]["generation"] == "provider_unavailable"
    elif failed_schema is llm.Classification:
        assert draft["text"] == "Thanks for watching!"
        assert draft["agreement"] == "pending"
    else:
        assert draft["text"] == "Thanks for watching!"
        assert draft["quality"] is None
        assert draft["failure"]["quality"] == "provider_unavailable"


def test_graph_path_preserves_regeneration_and_review_revisions(workspace, monkeypatch):
    monkeypatch.setattr(settings, "draft_workflow_engine", "langgraph")
    draft = generate(workspace)
    print("DRAFT", draft)

    regenerated = workspace.client.post(
        f'/api/drafts/{draft["id"]}/regenerate',
        headers=workspace.headers,
        json={"revision": draft["revision"]},
    )
    assert regenerated.status_code == 200
    assert regenerated.json()["revision"] == 2
    assert regenerated.json()["human_decision"] == "pending"
    assert regenerated.json()["quality"] == QUALITY

    reviewed = workspace.client.post(
        f'/api/drafts/{draft["id"]}/review',
        headers=workspace.headers,
        json={"revision": 2},
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["revision"] == 2
    assert reviewed.json()["review_status"] == "complete"
    assert reviewed.json()["text"] == "Thanks for watching!"
    assert all(method == "list" for _, method, _ in workspace.youtube_calls)

    history = workspace.client.get(f'/api/drafts/{draft["id"]}/usage').json()
    assert history["totals"]["requests"] == 9
