"""End-to-end: the solver output is valid, and the validator catches broken designs."""
import copy

import pytest

from cablering.export_json import check_schema
from cablering.input_loader import plan_from_index, plan_from_input_dict, raw_to_input_dict
from cablering.optimizer import solve
from cablering.validation import validate


def test_two_room_plan_is_valid(two_room_plan, cfg):
    out = solve(two_room_plan, cfg)
    assert out["validation"]["valid"], out["validation"]["violations"]
    assert out["reported_metrics"]["hub_count"] == 1
    check_schema(out)


def test_capacity_forces_second_hub(two_room_plan, cfg):
    cfg.max_area_m2 = 30            # each room is 24.5 m2, together 49 m2
    out = solve(two_room_plan, cfg)
    assert out["validation"]["valid"], out["validation"]["violations"]
    assert out["reported_metrics"]["hub_count"] == 2


def test_validator_catches_broken_designs(two_room_plan, cfg):
    out = solve(two_room_plan, cfg)
    hub = out["hubs"][0]

    diag = copy.deepcopy(out)
    seg = diag["hubs"][0]["route_segments"][0]
    seg["polyline"] = [seg["polyline"][0], seg["polyline"][-1]]
    seg["polyline"][-1] = [seg["polyline"][-1][0] + 0.3, seg["polyline"][-1][1] + 0.3]
    assert not validate(two_room_plan, diag, cfg)["valid"]

    missing = copy.deepcopy(out)
    missing["hubs"][0]["assigned_nodes"] = hub["assigned_nodes"][:1]
    assert not validate(two_room_plan, missing, cfg)["valid"]

    wall_hub = copy.deepcopy(out)
    wall_hub["hubs"][0]["location"] = {"x": 0.1, "y": 0.1}
    rules = {v["rule"] for v in validate(two_room_plan, wall_hub, cfg)["violations"]}
    assert "hub" in rules

    through_wall = copy.deepcopy(out)                # detour into the other room through the wall
    first = through_wall["hubs"][0]["route_segments"][0]
    a = first["polyline"][0]
    other_x = 2.0 if a[0] > 5 else 8.0
    first["polyline"] = [a, [a[0], 4.5], [other_x, 4.5], [other_x, a[1]], a] + first["polyline"][1:]
    assert "free_space" in {v["rule"] for v in validate(two_room_plan, through_wall, cfg)["violations"]}


def test_room_over_100m2_is_reported_infeasible(dataset, cfg):
    out = solve(plan_from_index(5, dataset), cfg)    # plan 5 has a 100+ m2 living room
    assert not out["validation"]["valid"]
    assert out["validation"]["violations"][0]["rule"] == "feasibility"


@pytest.mark.parametrize("index", [0, 1, 2, 3, 4, 6, 10, 25])
def test_dataset_plans_are_valid(dataset, cfg, index):
    plan = plan_from_index(index, dataset)
    out = solve(plan, cfg)
    assert out["validation"]["valid"], out["validation"]["violations"]
    assert out["reported_metrics"]["hub_count"] >= out["solver"]["hub_lower_bound"]


def test_json_input_round_trip(dataset, cfg):
    raw = dataset[7]
    plan_a = plan_from_index(7, dataset)
    plan_b = plan_from_input_dict(raw_to_input_dict(raw, plan_a.plan_id))
    assert abs(plan_a.scale - plan_b.scale) < 1e-6
    assert [r.id for r in plan_a.rooms] == [r.id for r in plan_b.rooms]
    assert abs(sum(r.area for r in plan_a.rooms) - sum(r.area for r in plan_b.rooms)) < 1e-3
