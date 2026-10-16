"""What-if simulator: scenarios re-solve correctly and are labelled."""
from cablering.optimizer import solve
from cablering.whatif import HubMove, Scenario, door_list, run


def test_empty_scenario_is_the_official_result(two_room_plan, cfg):
    official = solve(two_room_plan, cfg)
    _, out = run(two_room_plan, Scenario())
    assert out["scenario"]["official"] and out["scenario"]["deviations"] == []
    assert out["reported_metrics"]["cost_J"] == official["reported_metrics"]["cost_J"]


def test_closing_the_only_door_splits_the_zone(two_room_plan):
    doors = door_list(two_room_plan)
    assert [d["label"] for d in doors] == ["D1"]
    _, out = run(two_room_plan, Scenario(closed_doors=[doors[0]["index"]]))
    assert out["validation"]["valid"], out["validation"]["violations"]
    assert out["reported_metrics"]["hub_count"] == 2          # rooms no longer connected
    assert not out["scenario"]["official"]
    assert "D1 closed" in out["scenario"]["deviations"][0]


def test_rule_changes_are_listed(two_room_plan):
    _, out = run(two_room_plan, Scenario(max_area_m2=30, w_bend=1.0))
    devs = " | ".join(out["scenario"]["deviations"])
    assert "Max area per hub: 30" in devs and "Bend weight in J: 1" in devs
    assert out["reported_metrics"]["hub_count"] == 2          # 24.5 m2 rooms, 30 m2 limit


def test_hand_placed_hub_is_used_and_never_beats_the_optimizer(two_room_plan, cfg):
    official = solve(two_room_plan, cfg)
    _, out = run(two_room_plan, Scenario(hub_move=HubMove("H1", "R02")))
    assert out["validation"]["valid"], out["validation"]["violations"]
    assert out["hubs"][0]["room_id"] == "R02"
    assert out["reported_metrics"]["cost_J"] >= official["reported_metrics"]["cost_J"] - 1e-6


def test_hub_pushed_into_a_wall_is_flagged(two_room_plan):
    _, out = run(two_room_plan, Scenario(hub_move=HubMove("H1", "R02", dx=2.4)))
    assert not out["validation"]["valid"]
    assert any(v["rule"] == "hub" for v in out["validation"]["violations"])


def test_unknown_hub_is_reported_not_crashed(two_room_plan):
    _, out = run(two_room_plan, Scenario(hub_move=HubMove("H9", "R01")))
    assert "does not exist" in out["scenario"]["note"]
