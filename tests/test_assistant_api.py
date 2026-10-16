import uuid

import app as app_module
import pytest
from app import (
    AssistantRequest,
    SolveRequest,
    assistant as assistant_endpoint,
    design_histories,
    plan_configs,
    plans,
    solve as solve_endpoint,
)
from cablering.assistant import DesignRequest, Intent, Objective
from cablering.conversation import ModelDecision


@pytest.fixture(autouse=True)
def disable_external_ai_by_default(monkeypatch):
    monkeypatch.setattr(app_module.conversation_ai, "is_configured", lambda: False)


def test_assistant_api_uses_plan_session_and_returns_restorable_design(two_room_plan):
    plan_key = uuid.uuid4().hex
    plans[plan_key] = two_room_plan
    try:
        solved = solve_endpoint(SolveRequest(plan_key=plan_key, compare=False, time_budget_s=5))
        assert solved["output"]["validation"]["valid"]
        assert len(solved["versions"]) == 1

        answer = assistant_endpoint(AssistantRequest(
            plan_key=plan_key,
            message="Why did you use one panel?",
        ))
        assert answer["output"]["validation"]["valid"]
        assert answer["versions"][0]["current"]
        assert answer["optimized_image"].startswith("data:image/png;base64,")

        restored = assistant_endpoint(AssistantRequest(
            plan_key=plan_key,
            message="Restore version 1",
        ))
        assert restored["changed"]
        assert restored["output"] == solved["output"]
    finally:
        plans.pop(plan_key, None)
        design_histories.pop(plan_key, None)
        plan_configs.pop(plan_key, None)


def test_conversation_accepts_consecutive_messages_and_context(two_room_plan):
    plan_key = uuid.uuid4().hex
    plans[plan_key] = two_room_plan
    conversation = []
    try:
        solve_endpoint(SolveRequest(plan_key=plan_key, compare=False, time_budget_s=5))
        messages = [
            "Why did you use one panel?",
            "Can you explain its score?",
            "Please do that again.",
            "Reduce cable length.",
            "Compare it with the previous design.",
            "Undo that change.",
        ]
        replies = []
        for message in messages:
            answer = assistant_endpoint(AssistantRequest(
                plan_key=plan_key,
                message=message,
                conversation=conversation,
            ))
            replies.append(answer["response"])
            conversation.extend([
                {"role": "user", "content": message},
                {"role": "assistant", "content": answer["response"]},
            ])

        assert len(conversation) == 2 * len(messages)
        assert all(replies)
        assert "current design" in replies[1].lower() or "current" in replies[1].lower()
        assert "score" in replies[2].lower()
        assert "restored" in replies[-1].lower() or "no earlier" in replies[-1].lower()
    finally:
        plans.pop(plan_key, None)
        design_histories.pop(plan_key, None)
        plan_configs.pop(plan_key, None)


def test_compare_undo_and_restore_are_available_as_chat_messages(two_room_plan):
    plan_key = uuid.uuid4().hex
    plans[plan_key] = two_room_plan
    try:
        first = solve_endpoint(SolveRequest(plan_key=plan_key, compare=False, time_budget_s=5))
        second = solve_endpoint(SolveRequest(plan_key=plan_key, compare=False, time_budget_s=5))

        compared = assistant_endpoint(AssistantRequest(
            plan_key=plan_key,
            message="Compare it with the previous design.",
        ))
        assert compared["comparison"]["total_length_m"]["trend"] in {
            "improved", "unchanged", "worsened"
        }
        assert compared["alternative_image"].startswith("data:image/png;base64,")

        undone = assistant_endpoint(AssistantRequest(
            plan_key=plan_key,
            message="Undo that change.",
        ))
        assert undone["changed"]
        assert undone["output"] == first["output"]

        restored = assistant_endpoint(AssistantRequest(
            plan_key=plan_key,
            message="Restore version 2.",
        ))
        assert restored["changed"]
        assert restored["output"] == second["output"]
    finally:
        plans.pop(plan_key, None)
        design_histories.pop(plan_key, None)
        plan_configs.pop(plan_key, None)


def test_configured_model_answers_open_ended_question_without_optimizer(
    two_room_plan, monkeypatch
):
    plan_key = uuid.uuid4().hex
    plans[plan_key] = two_room_plan
    calls = []
    monkeypatch.setattr(app_module.conversation_ai, "is_configured", lambda: True)
    monkeypatch.setattr(
        app_module.conversation_ai,
        "interpret_message",
        lambda message, conversation, context: ModelDecision(
            "respond",
            "A detailed explanation based on the authoritative design context.",
        ),
    )
    def unexpected_action(*args, **kwargs):
        calls.append(args)
        raise AssertionError("A normal conversational question must not dispatch an action.")

    monkeypatch.setattr(app_module, "handle_request", unexpected_action)
    try:
        answer = assistant_endpoint(AssistantRequest(
            plan_key=plan_key,
            message=(
                "Could you explain why cable length and bends can trade off, "
                "and distinguish general possibilities from measured facts?"
            ),
        ))
        assert "detailed explanation" in answer["response"]
        assert answer["output"] is None
        assert calls == []
    finally:
        plans.pop(plan_key, None)
        design_histories.pop(plan_key, None)
        plan_configs.pop(plan_key, None)


def test_hypothetical_model_action_does_not_change_design(two_room_plan, monkeypatch):
    plan_key = uuid.uuid4().hex
    plans[plan_key] = two_room_plan
    monkeypatch.setattr(app_module.conversation_ai, "is_configured", lambda: True)
    monkeypatch.setattr(
        app_module.conversation_ai,
        "interpret_message",
        lambda message, conversation, context: ModelDecision(
            "optimize",
            "A shorter route could affect crossings, but this is only hypothetical.",
            DesignRequest(Intent.OPTIMIZE, objective=Objective.LENGTH),
        ),
    )
    try:
        answer = assistant_endpoint(AssistantRequest(
            plan_key=plan_key,
            message="What would happen if I asked you to shorten the cable?",
        ))
        assert answer["response"].startswith("A shorter route could")
        assert answer["output"] is None
        assert design_histories[plan_key].list_versions() == []
    finally:
        plans.pop(plan_key, None)
        design_histories.pop(plan_key, None)
        plan_configs.pop(plan_key, None)


def test_model_optimization_action_uses_existing_dispatch_and_validated_output(
    two_room_plan, monkeypatch
):
    plan_key = uuid.uuid4().hex
    plans[plan_key] = two_room_plan
    initial = solve_endpoint(SolveRequest(
        plan_key=plan_key, compare=False, time_budget_s=5
    ))
    calls = []
    original_handle = app_module.handle_request
    monkeypatch.setattr(app_module.conversation_ai, "is_configured", lambda: True)
    monkeypatch.setattr(
        app_module.conversation_ai,
        "interpret_message",
        lambda message, conversation, context: ModelDecision(
            "optimize",
            "I will ask the optimizer to reduce cable length.",
            DesignRequest(Intent.OPTIMIZE, objective=Objective.LENGTH),
        ),
    )

    def dispatch(*args, **kwargs):
        calls.append(args)
        return original_handle(*args, **kwargs)

    monkeypatch.setattr(app_module, "handle_request", dispatch)
    monkeypatch.setattr(
        app_module.conversation_ai,
        "generate_response",
        lambda message, conversation, context, result: "The optimizer's verified result is summarized.",
    )
    try:
        answer = assistant_endpoint(AssistantRequest(
            plan_key=plan_key,
            message="Please actually reduce the cable length.",
        ))
        assert len(calls) == 1
        assert answer["response"] == "The optimizer's verified result is summarized."
        assert answer["output"]["validation"]["valid"]
        assert answer["versions"][0]["metrics"] == answer["output"]["reported_metrics"]
        assert initial["output"]["validation"]["valid"]
    finally:
        plans.pop(plan_key, None)
        design_histories.pop(plan_key, None)
        plan_configs.pop(plan_key, None)


def test_open_ended_request_without_api_key_discloses_setup(two_room_plan, monkeypatch):
    plan_key = uuid.uuid4().hex
    plans[plan_key] = two_room_plan
    monkeypatch.setattr(app_module.conversation_ai, "is_configured", lambda: False)
    try:
        answer = assistant_endpoint(AssistantRequest(
            plan_key=plan_key,
            message="In general, what is the difference between a valid and an optimal solution?",
        ))
        assert "OPENAI_API_KEY" in answer["response"]
        assert "not configured" in answer["response"]
    finally:
        plans.pop(plan_key, None)
        design_histories.pop(plan_key, None)
        plan_configs.pop(plan_key, None)


def test_status_request_uses_local_validation_when_ai_key_is_rejected(
    two_room_plan, monkeypatch
):
    plan_key = uuid.uuid4().hex
    plans[plan_key] = two_room_plan
    monkeypatch.setattr(app_module.conversation_ai, "is_configured", lambda: True)

    def unexpected_model_call(*args, **kwargs):
        raise AssertionError("A deterministic status request must not call the AI provider.")

    monkeypatch.setattr(
        app_module.conversation_ai, "interpret_message", unexpected_model_call
    )
    try:
        before_generate = assistant_endpoint(AssistantRequest(
            plan_key=plan_key,
            message="Check whether this solution is valid",
        ))
        assert "run Generate first" in before_generate["response"]
        assert "rejected" not in before_generate["response"]

        solved = solve_endpoint(SolveRequest(
            plan_key=plan_key, compare=False, time_budget_s=5
        ))
        status = assistant_endpoint(AssistantRequest(
            plan_key=plan_key,
            message="Check whether this solution is valid",
        ))
        assert "passed validation and is valid" in status["response"]
        assert status["output"] == solved["output"]
    finally:
        plans.pop(plan_key, None)
        design_histories.pop(plan_key, None)
        plan_configs.pop(plan_key, None)


def test_deterministic_explanation_falls_back_when_ai_key_is_rejected(
    two_room_plan, monkeypatch
):
    plan_key = uuid.uuid4().hex
    plans[plan_key] = two_room_plan
    monkeypatch.setattr(app_module.conversation_ai, "is_configured", lambda: True)

    def reject_provider(*args, **kwargs):
        raise app_module.conversation_ai.ConversationError(
            "The AI provider rejected OPENAI_API_KEY."
        )

    monkeypatch.setattr(
        app_module.conversation_ai, "interpret_message", reject_provider
    )
    try:
        solve_endpoint(SolveRequest(plan_key=plan_key, compare=False, time_budget_s=5))
        answer = assistant_endpoint(AssistantRequest(
            plan_key=plan_key,
            message="Why did you use one panel?",
        ))
        assert "panel" in answer["response"].lower()
        assert "rejected OPENAI_API_KEY" not in answer["response"]
        assert answer["output"]["validation"]["valid"]
    finally:
        plans.pop(plan_key, None)
        design_histories.pop(plan_key, None)
        plan_configs.pop(plan_key, None)
