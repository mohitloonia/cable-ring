import copy

import pytest

from cablering.assistant import (
    DesignHistory,
    Intent,
    Objective,
    compare_designs,
    handle_request,
    parse_request,
)
from cablering.optimizer import solve


@pytest.mark.parametrize(
    ("message", "intent", "objective", "target"),
    [
        ("hi", Intent.GREETING, None, None),
        ("what can you do?", Intent.HELP, None, None),
        ("what should I try next?", Intent.NEXT_STEPS, None, None),
        ("is this solution valid?", Intent.STATUS, None, None),
        ("Check whether this solution is valid.", Intent.STATUS, None, None),
        ("Why did you use two panels?", Intent.EXPLAIN, None, None),
        ("Tell me why this layout has two panels.", Intent.EXPLAIN, None, None),
        ("Explain the current score.", Intent.EXPLAIN, None, None),
        ("Try using one panel.", Intent.CONSTRAIN, Objective.PANELS, 1),
        ("Can you try with just one panel?", Intent.CONSTRAIN, Objective.PANELS, 1),
        ("Reduce cable length by 10%.", Intent.OPTIMIZE, Objective.LENGTH, None),
        ("Make the cable shorter.", Intent.OPTIMIZE, Objective.LENGTH, None),
        ("Find a design with fewer crossings.", Intent.OPTIMIZE, Objective.CROSSINGS, None),
        ("Fewer intersections, please.", Intent.OPTIMIZE, Objective.CROSSINGS, None),
        ("Reduce overlapping cable.", Intent.OPTIMIZE, Objective.OVERLAP, None),
        ("Reduce the number of bends.", Intent.OPTIMIZE, Objective.BENDS, None),
        ("Find a better design.", Intent.OPTIMIZE, Objective.SCORE, None),
        ("Can you optimize this?", Intent.OPTIMIZE, Objective.SCORE, None),
        ("Can you make it better?", Intent.OPTIMIZE, Objective.SCORE, None),
        ("Check whether a smaller panel count is feasible.", Intent.OPTIMIZE, Objective.PANELS, None),
        ("Compare this solution with the previous one.", Intent.COMPARE, None, None),
        ("Undo the last change.", Intent.UNDO, None, None),
        ("Restore version 3.", Intent.RESTORE, None, None),
        ("Restore my previous valid design.", Intent.RESTORE, None, None),
        ("Make the score prefer length over crossings.", Intent.UNSUPPORTED, None, None),
        ("Please do that.", Intent.CLARIFY, None, None),
    ],
)
def test_parse_requests(message, intent, objective, target):
    request = parse_request(message)
    assert request.intent == intent
    assert request.objective == objective
    assert request.target_value == target


def test_compare_handles_percentages_zero_and_missing_metrics():
    before = {"reported_metrics": {"total_length_m": 100, "crossings": 0, "bends": 4}}
    after = {"reported_metrics": {"total_length_m": 80, "crossings": 1}}
    result = compare_designs(before, after)
    assert result["total_length_m"]["delta"] == -20
    assert result["total_length_m"]["percent_change"] == -20
    assert result["crossings"]["percent_change"] is None
    assert result["crossings"]["trend"] == "worsened"
    assert result["bends"]["trend"] == "unavailable"
    assert result["cost_J"]["trend"] == "unavailable"
    assert result["valid"]["trend"] == "unavailable"
    assert result["solver_time_s"]["trend"] == "unavailable"

    verified = compare_designs(
        {"reported_metrics": {}, "validation": {"valid": False}, "solver": {"time_s": 2.0}},
        {"reported_metrics": {}, "validation": {"valid": True}, "solver": {"time_s": 1.0}},
    )
    assert verified["valid"]["trend"] == "improved"
    assert verified["solver_time_s"]["trend"] == "faster"
    assert verified["solver_time_s"]["delta"] == -1.0


def test_parser_captures_scope_and_clarification_state():
    request = parse_request("Why was room R03 assigned to panel H1?")
    assert request.intent == Intent.EXPLAIN
    assert request.scope == "R03"
    assert not request.clarification_required
    assert parse_request("Please do that.").clarification_required

    scoped_optimization = parse_request("Reduce cable length in room R03.")
    assert scoped_optimization.intent == Intent.UNSUPPORTED
    assert scoped_optimization.scope == "R03"


def test_parser_uses_conversation_context_for_followups():
    conversation = [
        {"role": "user", "content": "Reduce cable length by 10%."},
        {"role": "assistant", "content": "The optimizer found a valid alternative."},
    ]
    followup = parse_request("Can you reduce it further?", conversation)
    assert followup.intent == Intent.OPTIMIZE
    assert followup.objective == Objective.LENGTH

    panel_context = [{"role": "user", "content": "Why did you use two panels?"}]
    panel_followup = parse_request("Can you try one?", panel_context)
    assert panel_followup.intent == Intent.CONSTRAIN
    assert panel_followup.target_value == 1

    try_again = parse_request(
        "Try again.",
        [{"role": "user", "content": "Reduce cable length."}],
    )
    assert try_again.intent == Intent.OPTIMIZE
    assert try_again.objective == Objective.LENGTH

    repeated_explanation = parse_request(
        "Please explain that again.",
        [{"role": "user", "content": "Explain the current score."}],
    )
    assert repeated_explanation.intent == Intent.EXPLAIN

    crossing_followup = parse_request(
        "Why did the crossings increase?",
        conversation + [{"role": "user", "content": "Find a design with fewer crossings."}],
    )
    assert crossing_followup.intent == Intent.EXPLAIN
    assert crossing_followup.compare_metric == Objective.CROSSINGS


def test_genuine_ambiguity_and_unsupported_metric_are_distinguished():
    assert parse_request("Please do that.").intent == Intent.CLARIFY
    unsupported = parse_request("Reduce energy consumption.")
    assert unsupported.intent == Intent.UNSUPPORTED


def test_explanation_is_conversational_and_uses_real_score(two_room_plan, cfg):
    output = solve(two_room_plan, cfg)
    from cablering.assistant import explain_solution

    answer = explain_solution(two_room_plan, output, cfg, "Explain the current score.")
    assert f"{output['reported_metrics']['total_length_m']:.3f} m" in answer
    assert f"{output['reported_metrics']['cost_J']:.3f}" in answer
    assert "contributes" in answer
    assert "designs evaluated" not in answer
    assert "closed order" not in answer


def test_history_snapshots_valid_outputs_and_restores_them(two_room_plan, cfg):
    output = solve(two_room_plan, cfg)
    history = DesignHistory()
    history.initialize(output)
    first = history.list_versions()[0]["id"]

    altered = copy.deepcopy(output)
    altered["solver"]["note"] = "saved second state"
    history.accept(altered, "test change")
    second = history.list_versions()[1]["id"]
    assert history.current["solver"]["note"] == "saved second state"
    assert history.undo() == output
    assert history.restore(second)["solver"]["note"] == "saved second state"
    assert history.restore_previous() == output
    assert history.restore(first) == output


def test_optimization_calls_real_solver_and_validates_candidate(two_room_plan, cfg):
    current = solve(two_room_plan, cfg)
    history = DesignHistory()
    history.initialize(current)
    calls = []

    def actual_optimizer(plan, config):
        calls.append((plan, config))
        return solve(plan, config)

    result = handle_request(
        two_room_plan, cfg, history, parse_request("Reduce cable length."), actual_optimizer
    )
    assert calls == [(two_room_plan, cfg)]
    assert result["output"]["validation"]["valid"]
    assert result["comparison"]["total_length_m"]["trend"] in {"improved", "unchanged", "worsened"}


def test_explanations_and_help_do_not_run_optimizer(two_room_plan, cfg):
    current = solve(two_room_plan, cfg)
    history = DesignHistory()
    history.initialize(current)
    calls = []

    def unexpected_optimizer(plan, config):
        calls.append((plan, config))
        raise AssertionError("An explanation must not start an optimization.")

    for message in ("Explain the current score.", "Is this solution valid?", "What can you do?"):
        result = handle_request(
            two_room_plan, cfg, history, parse_request(message), unexpected_optimizer
        )
        assert result["response"]
    assert calls == []


def test_invalid_candidate_does_not_replace_current_valid_design(two_room_plan, cfg):
    current = solve(two_room_plan, cfg)
    history = DesignHistory()
    history.initialize(current)
    invalid = copy.deepcopy(current)
    polyline = invalid["hubs"][0]["route_segments"][0]["polyline"]
    polyline[-1] = [polyline[-1][0] + 0.3, polyline[-1][1] + 0.3]

    result = handle_request(
        two_room_plan, cfg, history, parse_request("Find a better design."),
        lambda _plan, _cfg: invalid,
    )
    assert result["changed"] is False
    assert result["output"] == current
    assert history.current == current
    assert "candidate failed validation" in result["response"]


def test_optimizer_exception_preserves_current_design(two_room_plan, cfg):
    current = solve(two_room_plan, cfg)
    history = DesignHistory()
    history.initialize(current)

    def fail(_plan, _cfg):
        raise RuntimeError("test failure")

    result = handle_request(
        two_room_plan, cfg, history, parse_request("Find a better design."), fail
    )
    assert result["changed"] is False
    assert result["output"] == current
    assert history.current == current
    assert "RuntimeError" in result["response"]


def test_panel_target_is_accepted_when_existing_search_returns_it(two_room_plan, cfg):
    cfg.max_area_m2 = 30
    current = solve(two_room_plan, cfg)
    history = DesignHistory()
    history.initialize(current)
    result = handle_request(
        two_room_plan, cfg, history, parse_request("Try using two panels."),
        lambda plan, config: solve(plan, config),
    )
    assert current["reported_metrics"]["hub_count"] == 2
    assert result["changed"]
    assert result["output"]["reported_metrics"]["hub_count"] == 2


def test_panel_target_not_met_preserves_current_design(two_room_plan, cfg):
    cfg.max_area_m2 = 30
    current = solve(two_room_plan, cfg)
    history = DesignHistory()
    history.initialize(current)
    result = handle_request(
        two_room_plan, cfg, history, parse_request("Try using one panel."),
        lambda _plan, _cfg: current,
    )
    assert current["reported_metrics"]["hub_count"] == 2
    assert result["changed"] is False
    assert result["output"] == current
    assert "cannot force an exact count" in result["response"]
