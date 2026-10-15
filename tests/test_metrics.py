"""The metric examples from organiser slide 7."""
from cablering.metrics import compute, crossings, overlap, ring_bends


def ring_out(hub, nodes, legs):
    return {
        "hubs": [{"location": {"x": hub[0], "y": hub[1]},
                  "route_segments": [{"polyline": pl} for pl in legs],
                  "assigned_area_m2": 10.0}],
        "nodes": [{"location": {"x": x, "y": y}} for x, y in nodes],
    }


def test_length_includes_return_leg(cfg):
    # 4 + 2 + 3 + 3 = 12 m
    legs = [[(0, 0), (4, 0)], [(4, 0), (4, 2)], [(4, 2), (1, 2)], [(1, 2), (1, 0), (0, 0)]]
    m = compute(ring_out((0, 0), [(4, 0), (4, 2), (1, 2)], legs), cfg)
    assert abs(m["total_length_m"] - 12) < 1e-9


def test_retrace_counts_length_twice_and_overlap_once(cfg):
    legs = [[(0, 0), (3, 0)], [(3, 0), (0, 0)]]
    m = compute(ring_out((0, 0), [(3, 0)], legs), cfg)
    assert abs(m["total_length_m"] - 6) < 1e-9
    assert abs(m["overlap_m"] - 3) < 1e-9
    assert m["bends"] == 2                      # two reversals, each counts once


def test_crossing_in_the_open_counts():
    assert len(crossings([[(0, 1), (4, 1)], [(2, 0), (2, 3)]], [], 1e-3)) == 1


def test_meeting_at_a_node_does_not_count():
    assert len(crossings([[(0, 0), (2, 0)], [(2, 0), (2, 2)]], [(2, 0)], 1e-3)) == 0


def test_overlap_on_same_line():
    assert abs(overlap([[(0, 0), (5, 0)], [(3, 0), (5, 0)]], 1e-3) - 2) < 1e-9


def test_parallel_runs_do_not_overlap():
    assert overlap([[(0, 0), (5, 0)], [(0, 0.1), (5, 0.1)]], 1e-3) == 0


def test_bends():
    rect = [[(0, 0), (2, 0), (2, 1)], [(2, 1), (0, 1), (0, 0)]]
    assert ring_bends(rect, 1e-3) == 4
    straight_through = [[(0, 0), (1, 0)], [(1, 0), (2, 0), (2, 1), (0, 1), (0, 0)]]
    assert ring_bends(straight_through, 1e-3) == 4     # the node at (1, 0) adds no bend
