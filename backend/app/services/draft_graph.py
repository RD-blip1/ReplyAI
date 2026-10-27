"""LangGraph orchestration for existing ReplyAI draft and review operations.

The graph only coordinates existing provider wrappers, draft persistence, policy
evaluation, and the protected publication handoff. It exposes no model tools.
"""

import logging
from collections.abc import Awaitable, Callable
from operator import add
from typing import Annotated, Literal, NotRequired, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import ReplyDraft
from app.services import (
    draft_workflow,
    input_analysis,
    llm,
    publishing,
    reply_preferences,
    stage_inputs,
    stage_pipeline,
)

logger = logging.getLogger(__name__)

PublishHandoff = Callable[[], Awaitable[None]]


class WorkflowContext(BaseModel):
    """Validate route/database-derived context before it enters the graph."""

    model_config = ConfigDict(extra="forbid", strict=True)

    draft_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    operation_token: str = Field(min_length=1)
    operation: Literal["generate", "regenerate", "review"]
    generate: bool
    original_comment: str
    video_id: str = Field(pattern=r"^[A-Za-z0-9_-]{11}$")
    channel_id: str = Field(min_length=1)
    channel_title: str
    video_title: str | None = None
    video_description: str | None = None
    response_preferences: dict[str, str]
    publishing_mode: publishing.PublishingMode
    publishing_revision: int = Field(ge=0)


class DraftGraphState(TypedDict):
    db: AsyncSession
    draft: ReplyDraft
    context_input: dict
    context: NotRequired[dict]
    handoff: NotRequired[PublishHandoff | None]
    original_state: NotRequired[dict]
    text: NotRequired[str]
    primary: NotRequired[dict | None]
    secondary: NotRequired[dict | None]
    quality: NotRequired[dict | None]
    triage: NotRequired[dict | None]
    triage_result: NotRequired[draft_workflow.StageResult]
    triage_usage: NotRequired[dict | None]
    triage_error: NotRequired[str | None]
    base_usage: NotRequired[dict]
    generation_result: NotRequired[draft_workflow.StageResult]
    classification_result: NotRequired[draft_workflow.StageResult]
    quality_result: NotRequired[draft_workflow.StageResult]
    generation_usage: NotRequired[dict | None]
    classification_usage: NotRequired[dict | None]
    quality_usage: NotRequired[dict | None]
    generation_error: NotRequired[str | None]
    classification_error: NotRequired[str | None]
    quality_error: NotRequired[str | None]
    spent: Annotated[list[dict], add]
    failures: NotRequired[dict]
    usage: NotRequired[dict]
    quality_needed: NotRequired[bool]
    policy_route: NotRequired[str]


def _workflow_data(state: DraftGraphState) -> WorkflowContext:
    return WorkflowContext.model_validate(state["context_input"])


async def load_context(state: DraftGraphState) -> dict:
    context = _workflow_data(state)
    return {"context": context.model_dump()}


async def comment_triage(state: DraftGraphState) -> dict:
    context = WorkflowContext.model_validate(state["context"])
    triage = await draft_workflow.call_stage(
        "secondary",
        llm.Triage,
        stage_pipeline.specification("triage")[2],
        stage_inputs.build("triage", context.model_dump()),
    )
    # Same post-triage steps as the shared runner: language contract, observe-only signals.
    analysis = input_analysis.analyze(context.original_comment)
    value = stage_pipeline.apply_language_contract(triage.value, context.model_dump(), analysis.script_profile)
    if value is not None and settings.input_signals_mode != "off":
        value = {**value, "input_signals": analysis.as_dict()}
    return {
        "triage_result": triage,
        "triage": value,
        "triage_usage": triage.usage,
        "triage_error": triage.error,
        "spent": draft_workflow.usage_attempts(
            triage,
            draft_id=context.draft_id,
            revision=context.revision,
            operation_token=context.operation_token,
            operation=context.operation,
            stage="triage",
        ),
    }

def route_after_context(state: DraftGraphState) -> str | list[str]:
    context = WorkflowContext.model_validate(state["context"])
    if context.generate:
        return "comment_triage"
    if state.get("secondary") is None:
        return "review_classification"
    if state.get("text"):
        return "quality_review"
    return "persist_draft"

def route_after_triage(state: DraftGraphState) -> str | list[str]:
    triage_val = state.get("triage") or {}
    category = triage_val.get("category")
    if category in ["on_topic", "generic_engagement"]:
        return ["primary_generation", "secondary_classification"]
    return "persist_draft"


async def primary_generation(state: DraftGraphState) -> dict:
    context = WorkflowContext.model_validate(state["context"])
    generated = await draft_workflow.call_stage(
        "primary",
        llm.Generation,
        stage_pipeline.specification("generation")[2],
        stage_inputs.build("generation", {**context.model_dump(), "triage": state.get("triage")}),
    )
    return {
        "generation_result": generated,
        "text": generated.value["reply"] if generated.value else "",
        "primary": generated.value["classification"] if generated.value else None,
        "generation_usage": generated.usage,
        "generation_error": generated.error,
        "spent": draft_workflow.usage_attempts(
            generated,
            draft_id=context.draft_id,
            revision=context.revision,
            operation_token=context.operation_token,
            operation=context.operation,
            stage="generation",
        ),
    }


async def secondary_classification(state: DraftGraphState) -> dict:
    context = WorkflowContext.model_validate(state["context"])
    classified = await draft_workflow.call_stage(
        "secondary",
        llm.Classification,
        stage_pipeline.specification("classification")[2],
        stage_inputs.build("classification", context.model_dump()),
    )
    return {
        "classification_result": classified,
        "secondary": classified.value,
        "classification_usage": classified.usage,
        "classification_error": classified.error,
        "spent": draft_workflow.usage_attempts(
            classified,
            draft_id=context.draft_id,
            revision=context.revision,
            operation_token=context.operation_token,
            operation=context.operation,
            stage="classification",
        ),
    }


async def review_classification(state: DraftGraphState) -> dict:
    context = WorkflowContext.model_validate(state["context"])
    classified = await draft_workflow.call_stage(
        "secondary",
        llm.Classification,
        stage_pipeline.specification("review_classification")[2],
        stage_inputs.build("classification", context.model_dump()),
    )
    return {
        "classification_result": classified,
        "secondary": classified.value,
        "classification_usage": classified.usage,
        "classification_error": classified.error,
        "spent": draft_workflow.usage_attempts(
            classified,
            draft_id=context.draft_id,
            revision=context.revision,
            operation_token=context.operation_token,
            operation=context.operation,
            stage="classification",
        ),
    }


def parallel_quality_gate(state: DraftGraphState) -> dict:
    return {"quality_needed": bool(state.get("text"))}


def route_after_parallel(state: DraftGraphState) -> str:
    return "quality_review" if state.get("quality_needed") else "persist_draft"


def route_after_review_classification(state: DraftGraphState) -> str:
    return "quality_review" if state.get("text") else "persist_draft"


async def quality_review(state: DraftGraphState) -> dict:
    context = WorkflowContext.model_validate(state["context"])
    reviewed = await draft_workflow.call_stage(
        "secondary",
        llm.Quality,
        stage_pipeline.specification("quality")[2],
        stage_inputs.build("quality", context.model_dump(), reply=state.get("text", "")),
    )
    return {
        "quality_result": reviewed,
        "quality": reviewed.value,
        "quality_usage": reviewed.usage,
        "quality_error": reviewed.error,
        "spent": draft_workflow.usage_attempts(
            reviewed,
            draft_id=context.draft_id,
            revision=context.revision,
            operation_token=context.operation_token,
            operation=context.operation,
            stage="quality",
        ),
    }


async def persist_draft(state: DraftGraphState) -> dict:
    context = WorkflowContext.model_validate(state["context"])
    failures = {}
    usage = dict(state.get("base_usage") or {})
    for stage, error_key, usage_key in (
        ("generation", "generation_error", "generation_usage"),
        ("classification", "classification_error", "classification_usage"),
        ("quality", "quality_error", "quality_usage"),
    ):
        if stage == "generation" and not context.generate:
            continue
        if (
            stage == "classification"
            and not context.generate
            and state.get("secondary") is not None
        ):
            continue
        if stage == "quality" and not state.get("text"):
            continue
        error = state.get(error_key)
        if error:
            failures[stage] = error
            usage.pop(stage, None)
        elif state.get(usage_key):
            usage[stage] = state[usage_key]

    if not state.get("text"):
        triage_val = state.get("triage") or {}
        triage_cat = triage_val.get("category")
        if context.generate and triage_cat and triage_cat not in ("on_topic", "generic_engagement"):
            failures["generation"] = "skipped"
        else:
            failures["quality"] = "generation_required"

    draft = await draft_workflow.persist_result(
        state["db"],
        state["draft"],
        revision=context.revision,
        operation_token=context.operation_token,
        text=state.get("text", ""),
        primary=state.get("primary"),
        secondary=state.get("secondary"),
        triage=state.get("triage"),
        quality=state.get("quality"),
        failures=failures,
        usage=usage,
        spent=state.get("spent", []),
    )
    return {"draft": draft, "failures": failures, "usage": usage}


def route_to_policy(state: DraftGraphState) -> str:
    return (
        "evaluate_publishing_policy"
        if WorkflowContext.model_validate(state["context"]).generate
        else END
    )


def evaluate_publishing_policy(state: DraftGraphState) -> dict:
    context = WorkflowContext.model_validate(state["context"])
    mode = context.publishing_mode
    if mode == "manual_review":
        return {"policy_route": "manual_review"}
    blocked = publishing.automation_blocking_reason(state["draft"], mode)
    if blocked and mode != "dry_run":
        return {"policy_route": f"human_review:{blocked[0]}"}
    return {"policy_route": "protected_publish_handoff"}


def route_after_policy(state: DraftGraphState) -> str:
    # Every automatic-mode draft reaches the handoff: publish_generated_draft runs the
    # central decision policy, records the decision, and publishes only an
    # AUTO_PUBLISH verdict. A blocked route here therefore still records why.
    route = state.get("policy_route") or "manual_review"
    return "protected_publish_handoff" if route != "manual_review" and state.get("handoff") else END


async def protected_publish_handoff(state: DraftGraphState) -> dict:
    callback = state.get("handoff")
    if callback is not None:
        await callback()
    return {}


def build_graph():
    graph = StateGraph(DraftGraphState)
    graph.add_node("load_context", load_context)
    graph.add_node("comment_triage", comment_triage)
    graph.add_node("primary_generation", primary_generation)
    graph.add_node("secondary_classification", secondary_classification)
    graph.add_node("review_classification", review_classification)
    graph.add_node("quality_review", quality_review)
    graph.add_node("persist_draft", persist_draft)
    graph.add_node("evaluate_publishing_policy", evaluate_publishing_policy)
    graph.add_node("protected_publish_handoff", protected_publish_handoff)
    graph.add_edge(START, "load_context")
    graph.add_conditional_edges("load_context", route_after_context)
    graph.add_conditional_edges("comment_triage", route_after_triage)
    graph.add_node("parallel_quality_gate", parallel_quality_gate)
    graph.add_edge(["primary_generation", "secondary_classification"], "parallel_quality_gate")
    graph.add_conditional_edges("parallel_quality_gate", route_after_parallel)
    graph.add_conditional_edges("review_classification", route_after_review_classification)
    graph.add_edge("quality_review", "persist_draft")
    graph.add_conditional_edges("persist_draft", route_to_policy)
    graph.add_conditional_edges("evaluate_publishing_policy", route_after_policy)
    graph.add_edge("protected_publish_handoff", END)
    return graph.compile()


async def run_draft_graph(
    db: AsyncSession,
    draft: ReplyDraft,
    *,
    generate: bool,
    operation: Literal["generate", "regenerate", "review"],
    preferences: dict[str, str] | None,
    channel_title: str,
    video_title: str,
    video_description: str,
    publishing_mode: publishing.PublishingMode,
    publishing_revision: int,
    handoff: PublishHandoff | None = None,
) -> ReplyDraft:
    revision = draft.revision
    token = draft.operation_token
    if token is None:
        raise ValueError("Draft workflow requires a claimed operation token.")

    state: DraftGraphState = {
        "db": db,
        "draft": draft,
        "context_input": {
            "draft_id": draft.id,
            "revision": revision,
            "operation_token": token,
            "operation": operation,
            "generate": generate,
            "original_comment": draft.original_comment,
            "video_id": draft.video_id,
            "channel_id": draft.channel_id,
            "channel_title": channel_title,
            "video_title": video_title,
            "video_description": video_description,
            "response_preferences": preferences or reply_preferences.context(None),
            "publishing_mode": publishing_mode,
            "publishing_revision": publishing_revision,
        },
        "handoff": handoff,
        "text": draft.text,
        "primary": draft.primary_classification,
        "secondary": draft.secondary_classification,
        "quality": None,
        "base_usage": dict(draft.token_usage or {}),
        "spent": [],
    }
    compiled = build_graph()
    logger.info("event=draft_graph_execution_started operation=%s", operation)
    try:
        async for update in compiled.astream(state, stream_mode="updates"):
            for node_name, node_result in update.items():
                logger.info("event=draft_graph_node_completed node=%s", node_name)
                if isinstance(node_result, dict) and node_result.get("policy_route"):
                    logger.info(
                        "event=draft_graph_policy_route_selected route=%s",
                        node_result["policy_route"],
                    )
    except draft_workflow.StaleWorkflowResult:
        draft_workflow.raise_stale_workflow()
    logger.info("event=draft_graph_execution_completed operation=%s", operation)
    return draft
