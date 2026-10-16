import json
from types import SimpleNamespace

import pytest
from openai import APITimeoutError

from cablering import conversation
from cablering.assistant import Intent, Objective


class FakeOpenAI:
    def __init__(self, content, calls, **options):
        self.calls = calls
        self.options = options
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self.create)
        )
        self.content = content

    def create(self, **options):
        self.calls.append(options)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))]
        )


def _decision(
    action="respond",
    answer="A grounded response.",
    objective=None,
    target_percent=None,
    target_panel_count=None,
    version_id=None,
):
    return json.dumps({
        "action": action,
        "answer": answer,
        "objective": objective,
        "target_percent": target_percent,
        "target_panel_count": target_panel_count,
        "version_id": version_id,
    })


def test_interpret_message_uses_model_context_and_configurable_client(monkeypatch):
    calls = []
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    monkeypatch.setenv("OPENAI_MODEL", "test-instruction-model")
    monkeypatch.setenv("OPENAI_TIMEOUT_SECONDS", "12")
    monkeypatch.setattr(
        conversation, "OpenAI",
        lambda **options: FakeOpenAI(_decision(), calls, **options),
    )
    context = {"current_verified_design": {"reported_metrics": {"bends": 8}}}

    result = conversation.interpret_message(
        "Explain why this design has so many bends.",
        [
            {"role": "user", "content": "Could you explain the score?"},
            {"role": "assistant", "content": "The score includes bends."},
        ],
        context,
    )

    assert result.action == "respond"
    assert result.answer == "A grounded response."
    assert calls[0]["model"] == "test-instruction-model"
    assert calls[0]["response_format"] == conversation.RESPONSE_SCHEMA
    assert calls[0]["messages"][-3]["content"] == "Could you explain the score?"
    assert "AUTHORITATIVE_CONTEXT" in calls[0]["messages"][1]["content"]


@pytest.mark.parametrize(
    ("payload", "intent", "objective", "target"),
    [
        (_decision("optimize", objective="total_length_m"), Intent.OPTIMIZE, Objective.LENGTH, None),
        (_decision("optimize", objective="total_length_m", target_percent=5), Intent.OPTIMIZE, Objective.LENGTH, 5),
        (_decision("optimize", objective="hub_count", target_panel_count=2), Intent.CONSTRAIN, Objective.PANELS, 2),
        (_decision("compare"), Intent.COMPARE, None, None),
        (_decision("undo"), Intent.UNDO, None, None),
        (_decision("restore", version_id="previous"), Intent.RESTORE, None, "previous"),
    ],
)
def test_decision_actions_are_allowlisted_and_validated(payload, intent, objective, target):
    decision = conversation._validate_decision(json.loads(payload))
    assert decision.request.intent == intent
    assert decision.request.objective == objective
    if intent == Intent.CONSTRAIN:
        assert decision.request.target_value == target
    if intent == Intent.RESTORE:
        assert decision.request.version_id == target


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "run_shell", "answer": "", "objective": None,
         "target_percent": None, "target_panel_count": None, "version_id": None},
        {"action": "optimize", "answer": "", "objective": "invented_metric",
         "target_percent": None, "target_panel_count": None, "version_id": None},
        {"action": "optimize", "answer": "", "objective": "crossings",
         "target_percent": 10, "target_panel_count": None, "version_id": None},
        {"action": "optimize", "answer": "", "objective": "hub_count",
         "target_percent": None, "target_panel_count": 0, "version_id": None},
        {"action": "restore", "answer": "", "objective": None,
         "target_percent": None, "target_panel_count": None, "version_id": None},
    ],
)
def test_rejects_invalid_model_actions(payload):
    with pytest.raises(ValueError):
        conversation._validate_decision(payload)


def test_missing_key_is_explicit(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(conversation.ConversationError, match="Set OPENAI_API_KEY"):
        conversation._client()


def test_empty_optional_base_url_uses_official_https_endpoint(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    monkeypatch.setenv("OPENAI_BASE_URL", "")

    client = conversation._client()

    assert str(client.base_url) == "https://api.openai.com/v1/"


def test_malformed_model_response_is_reported_without_action(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    monkeypatch.setattr(
        conversation, "OpenAI",
        lambda **options: FakeOpenAI("{invalid json", [], **options),
    )

    with pytest.raises(conversation.ConversationError, match="unusable response"):
        conversation.interpret_message("Optimize this.", [], {})


def test_provider_timeout_has_a_specific_user_safe_error(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")

    def timeout(**_):
        raise APITimeoutError(request=object())

    monkeypatch.setattr(
        conversation, "OpenAI",
        lambda **_: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=timeout))
        ),
    )
    with pytest.raises(conversation.ConversationError, match="timed out"):
        conversation.interpret_message("Explain the design.", [], {})


def test_context_contains_only_reported_design_facts(two_room_plan, cfg):
    output = {
        "plan_id": "TEST",
        "reported_metrics": {"total_length_m": 12.5, "bends": 4},
        "validation": {"valid": True, "violations": []},
        "solver": {"time_s": 0.3},
        "hubs": [{
            "hub_id": "H1",
            "room_id": "R01",
            "assigned_rooms": ["R01"],
            "assigned_nodes": ["N1"],
            "assigned_area_m2": 24.5,
            "ring_length_m": 12.5,
            "route_segments": [{"from": "N1", "to": "N1", "length_m": 12.5}],
        }],
    }

    result = conversation.design_context(two_room_plan, output, None, cfg)

    summary = result["current_verified_design"]
    assert summary["verified"] is True
    assert summary["reported_metrics"] == {"total_length_m": 12.5, "bends": 4}
    assert summary["hubs"][0]["assigned_device_count"] == 1
    assert "cost_J" not in summary["reported_metrics"]
