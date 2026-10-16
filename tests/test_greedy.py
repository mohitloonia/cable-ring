"""Greedy baseline: valid, capacity-respecting, and really greedy (no Dijkstra)."""
import pytest

import cablering.routing as routing
from cablering.config import Config
from cablering.greedy import greedy_zones, solve_greedy
from cablering.input_loader import plan_from_index
from cablering.optimizer import solve
from cablering.room_graph import build_room_graph, zone_connected


def _no_dijkstra(*args, **kwargs):
    raise AssertionError("the greedy baseline must not run Dijkstra")


def test_greedy_never_calls_dijkstra(two_room_plan, monkeypatch):
    monkeypatch.setattr(routing, "dijkstra", _no_dijkstra)
    out = solve_greedy(two_room_plan, Config())
    assert out["validation"]["valid"], out["validation"]["violations"]
    assert "greedy" in out["solver"]["mode"]


def test_baseline_flag_uses_greedy(two_room_plan):
    out = solve(two_room_plan, Config(), baseline=True)
    assert out["solver"]["mode"] == "greedy baseline"


def test_greedy_zones_respect_capacity_and_doors(two_room_plan):
    cfg = Config(max_area_m2=30)                      # each room 24.5 m2: must split
    G = build_room_graph(two_room_plan, cfg)
    zones = greedy_zones(two_room_plan, G, cfg)
    assert sorted(len(z) for z in zones) == [1, 1]
    for z in zones:
        assert zone_connected(G, z, False)
        assert sum(two_room_plan.room(r).area for r in z) <= 30 + 1e-6


@pytest.mark.parametrize("index", [0, 2, 42, 593])
def test_greedy_is_valid_and_costs_more_than_optimised(dataset, index, monkeypatch):
    plan = plan_from_index(index, dataset)
    best = solve(plan, Config(relocate_trials=0))
    monkeypatch.setattr(routing, "dijkstra", _no_dijkstra)
    greedy = solve_greedy(plan, Config())
    assert greedy["validation"]["valid"], greedy["validation"]["violations"]
    assert greedy["reported_metrics"]["hub_count"] >= best["reported_metrics"]["hub_count"]
    assert greedy["reported_metrics"]["cost_J"] >= best["reported_metrics"]["cost_J"]
