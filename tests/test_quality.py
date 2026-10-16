"""Quality engine: never worse than the original, and its new parts behave."""
import numpy as np
import pytest

from cablering.config import Config
from cablering.input_loader import plan_from_index
from cablering.optimizer import solve, solve_original
from cablering.quality import design_key
from cablering.routing import DirRoutingGraph, RoutingGraph
from cablering.validation import validate_strict


def test_original_engine_is_unchanged_by_quality_settings(two_room_plan):
    a = solve_original(two_room_plan, Config().as_original())
    b = solve(two_room_plan, Config(engine="original"))
    assert a["reported_metrics"]["cost_J"] == b["reported_metrics"]["cost_J"]


def test_quality_never_worse_on_synthetic_plan(two_room_plan):
    out = solve(two_room_plan, Config())
    orig = out["solver"]["original"]
    assert out["validation"]["valid"]
    assert out["reported_metrics"]["hub_count"] <= orig["hub_count"]
    assert out["reported_metrics"]["cost_J"] <= orig["cost_J"] + 1e-9


@pytest.mark.parametrize("index", [0, 2, 42, 593, 10393])
def test_quality_never_worse_on_dataset_plans(dataset, index):
    plan = plan_from_index(index, dataset)
    out = solve(plan, Config(relocate_trials=2))
    orig = solve_original(plan, Config().as_original())
    assert design_key(out) <= design_key(orig)
    assert out["validation"]["valid"]


def test_design_key_orders_like_the_judges():
    def d(valid, hubs, J, gap=0.0):
        return {"reported_metrics": {"valid": valid, "hub_count": hubs, "cost_J": J, "area_gap_m2": gap}}
    assert design_key(d(True, 2, 50)) < design_key(d(True, 3, 10))      # fewer hubs first
    assert design_key(d(True, 2, 10)) < design_key(d(True, 2, 11))      # then J
    assert design_key(d(True, 2, 10, 1)) < design_key(d(True, 2, 10, 5))  # then area gap
    assert design_key(d(True, 9, 99)) < design_key(d(False, 1, 1))      # validity above all


def test_polyline_keeps_reversal_points():
    from shapely.geometry import box
    g = RoutingGraph(box(0, 0, 4, 1), [(0.5, 0.5), (3.5, 0.5)], [], [], 0.5)
    v = {(round(x, 3), round(y, 3)): i for i, (x, y) in enumerate(zip(g.vx, g.vy))}
    a, far, back = v[(0.5, 0.5)], v[(3.5, 0.5)], v[(0.5, 0.5)]
    path = [a, far, back]
    assert g.polyline(path) == [(0.5, 0.5), (3.5, 0.5), (0.5, 0.5)]   # out and back, not collapsed


def test_direction_graph_charges_reversal_as_one_bend():
    from shapely.geometry import box
    g = DirRoutingGraph(box(0, 0, 4, 1), [(0.5, 0.5), (3.5, 0.5)], [], [], 0.5)
    D = g.all_pairs_dir()
    PX, MX = 0, 1
    straight = D[0, PX, 1, PX]                  # leave east, arrive moving east
    assert abs(straight - 3.0) < 1e-6
    assert abs(D[0, PX, 1, MX] - (3.0 + 0.5)) < 1e-6   # must turn round once at the end
    assert np.isfinite(D).all()


def test_strict_check_passes_on_a_solved_plan(two_room_plan):
    out = solve(two_room_plan, Config())
    assert validate_strict(two_room_plan, out, Config())["valid"]
