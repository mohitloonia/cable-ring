"""Node sliding: move nodes onto the cable they already sit beside.

For each node of the current best design we look for a better spot inside its room
(still >= clearance from walls, windows and doors):
  * on another leg of the same ring that passes through the room: the ring then
    reaches the node without a detour (saves length and bends)
  * at a corner of its own incoming / outgoing leg: the turn moves onto the node
The zones stay fixed; every trial is re-solved with `solve_with_fixed_points` (order,
routes and hubs re-optimised) and validated. First all nodes move together, then single
nodes greedily, each trial starting from the best design so far.
"""
from __future__ import annotations

import math
import time

from shapely.geometry import LineString, Point
from shapely.ops import nearest_points

from .geometry import clear_region, clearance_obstacles, grow, shrink
from .optimizer import solve_with_fixed_points


def _node_moves(plan, cfg, out) -> dict[str, list[tuple[float, float]]]:
    obst = grow(clearance_obstacles(plan, cfg.clearance_from_doors), cfg.clearance_m + 0.02)
    inner_c = shrink(plan.inner, cfg.clearance_m + 0.02)
    node_of = {n["node_id"]: n for n in out["nodes"]}
    moves: dict[str, list[tuple[float, float]]] = {}
    for hub in out["hubs"]:
        for nid in hub["closed_sequence"][1:-1]:
            node = node_of[nid]
            here = (node["location"]["x"], node["location"]["y"])
            region = clear_region(plan.room(node["room_id"]).poly, obst, inner_c, cfg.wall_margin_m)
            if region.is_empty:
                continue
            own = [s for s in hub["route_segments"] if nid in (s["from"], s["to"])]
            others = [s for s in hub["route_segments"] if nid not in (s["from"], s["to"])]
            found = []
            for s in others:                          # cable passing through the room
                inside = LineString(s["polyline"]).intersection(region)
                if not inside.is_empty:
                    q = nearest_points(inside, Point(here))[0]
                    found.append((math.dist(here, (q.x, q.y)), (q.x, q.y)))
            for s in own:                             # corners next to the node
                pl = s["polyline"]
                corner = pl[-2] if s["to"] == nid else pl[1] if len(pl) > 2 else None
                if corner is not None and region.covers(Point(corner)):
                    found.append((math.dist(here, corner) + 1.0, tuple(corner)))   # rank after the above
            found = [f for f in found if f[0] > 0.05]
            if found:
                found.sort()
                moves[node["room_id"]] = [p for _, p in found[:3]]
    return moves


def relocation_candidates(plan, cfg, best: dict, deadline: float):
    """Yield (name, output) for node-sliding trials; each starts from the best so far."""
    from .quality import design_key

    current = best
    trials = 0
    blocks = [h["assigned_rooms"] for h in current["hubs"]]

    def positions(out):
        return {n["room_id"]: (n["location"]["x"], n["location"]["y"]) for n in out["nodes"]}

    moves = _node_moves(plan, cfg, current)
    if not moves:
        return
    # 1. all nodes at once (their first choice)
    if len(moves) > 1 and time.time() < deadline:
        pts = positions(current)
        pts.update({room: cands[0] for room, cands in moves.items()})
        out = solve_with_fixed_points(plan, cfg, blocks, node_points=pts)
        trials += 1
        yield "node_sliding", out
        if design_key(out) < design_key(current):
            current = out
    # 2. one node at a time, greedily
    singles = [(room, p) for room, cands in moves.items() for p in cands]
    for room, p in singles:
        if trials >= cfg.relocate_trials or time.time() > deadline:
            break
        pts = positions(current)
        if room not in pts or math.dist(pts[room], p) < 0.05:
            continue
        pts[room] = p
        out = solve_with_fixed_points(plan, cfg, blocks, node_points=pts)
        trials += 1
        yield "node_sliding", out
        if design_key(out) < design_key(current):
            current = out
