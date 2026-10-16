"""Greedy baseline: every decision is the best-looking choice right now, never revisited.

  zones     greedy region growing: seed = biggest unassigned room; repeatedly add the
            biggest door-connected room that still fits (area / node limits); when
            nothing fits, start a new hub
  nodes     the centre of each room (deepest point >= clearance from walls)
  order     nearest neighbour: from the zone's first room, always go to the closest
            unvisited node, then back
  paths     greedy best-first search: always step towards the target; no Dijkstra,
            no bend or overlap awareness
  hub       first valid straight spot on the ring's own cable

It obeys every hard rule (doors only, connected zones, capacity), so it is a fair,
valid reference showing what the optimised engine adds. No Dijkstra or Held-Karp is used.
"""
from __future__ import annotations

import time

import numpy as np

from .assignment import lower_bound
from .candidates import build_candidates
from .geometry import clearance_obstacles, free_space
from .optimizer import Context, Design, Leg, _finish, _infeasible, finalize, guide_lines, node_ids
from .room_graph import build_room_graph
from .routing import RoutingGraph
from .sequencing import RingPlan
from .validation import validate


def _context(plan, cfg) -> Context:
    G = build_room_graph(plan, cfg)
    space = free_space(plan, G, cfg.wall_margin_m)
    legal = free_space(plan, G, 2 * cfg.tol_m)
    pts, room_of, room_cands, fallback = build_candidates(plan, G, cfg, "center")
    gx, gy = guide_lines(G, cfg)
    router = RoutingGraph(space, pts, gx, gy, cfg.w_bend)      # grid only: all_pairs is never run
    return Context(plan, cfg, G, space, legal, clearance_obstacles(plan, cfg.clearance_from_doors),
                   router, room_of, room_cands, fallback, np.zeros((0, 0)), node_ids(plan, "center"),
                   "center")


def _eligible_neighbours(G, room: str, eligible: set[str], via_other: bool) -> set[str]:
    """Eligible rooms linked to `room` by a door / opening (optionally through bathrooms etc.)."""
    found, seen, todo = set(), {room}, [room]
    while todo:
        r = todo.pop()
        for nb in G[r]:
            if nb in seen:
                continue
            seen.add(nb)
            if nb in eligible:
                found.add(nb)
            elif via_other:
                todo.append(nb)
    return found


def greedy_zones(plan, G, cfg) -> list[list[str]]:
    area = {r.id: r.area for r in plan.eligible_rooms}
    eligible = set(area)
    unassigned = set(eligible)
    zones = []
    while unassigned:
        seed = max(unassigned, key=lambda r: (area[r], r))
        zone, total = [seed], area[seed]
        unassigned.discard(seed)
        while True:
            frontier = set().union(*(_eligible_neighbours(G, r, eligible, cfg.zone_via_other_rooms)
                                     for r in zone)) & unassigned
            fits = [r for r in frontier
                    if total + area[r] <= cfg.max_area_m2 + cfg.tol_m and len(zone) < cfg.max_nodes]
            if not fits:
                break
            pick = max(fits, key=lambda r: (area[r], r))     # biggest that still fits
            zone.append(pick)
            total += area[pick]
            unassigned.discard(pick)
        zones.append(zone)
    return zones


def _path_length(router, verts) -> float:
    if not verts:
        return np.inf
    xs, ys = router.vx[verts], router.vy[verts]
    return float(np.abs(np.diff(xs)).sum() + np.abs(np.diff(ys)).sum())


def nearest_neighbour_order(router, cands: list[int]) -> list[int]:
    """Start at the first candidate; always walk to the closest unvisited one."""
    tv = router.term_vid
    order, left = [cands[0]], list(cands[1:])
    while left:
        here = tv[order[-1]]
        nxt = min(left, key=lambda c: _path_length(router, router.route_greedy(here, tv[c])))
        order.append(nxt)
        left.remove(nxt)
    return order


def solve_greedy(plan, cfg) -> dict:
    t0 = time.time()
    cfg = cfg.as_original()                   # simple geometry settings for the reference
    ctx = _context(plan, cfg)
    elig = plan.eligible_rooms
    info = {"mode": "greedy baseline", "node_mode": "center",
            "algorithms": "greedy region growing, nearest neighbour, greedy best-first search",
            "hub_lower_bound": lower_bound([r.area for r in elig], cfg) if elig else 0,
            "zone_splits_feasible": 1, "designs_evaluated": 1,
            "fallback_rooms": sorted(ctx.fallback_rooms),
            "dropped_rooms": plan.source.get("dropped_rooms", [])}
    if not elig:
        return _finish(ctx, {"plan_id": plan.plan_id, "units": "m", "hubs": [], "nodes": [],
                             "reported_metrics": {}},
                       {"valid": True, "violations": [], "warnings": ["no eligible rooms"]}, info, t0)
    too_big = [r.id for r in elig if r.area > cfg.max_area_m2 + cfg.tol_m]
    if too_big:
        return _infeasible(ctx, f"rooms {too_big} alone exceed {cfg.max_area_m2} m2", info, t0)

    router = ctx.router
    blocks = greedy_zones(plan, ctx.G, cfg)
    plans, legs = [], []
    for zi, rooms in enumerate(blocks):
        cands = [ctx.room_cands[r][0] for r in rooms]
        order = nearest_neighbour_order(router, cands) if len(cands) > 1 else cands
        plans.append(RingPlan(0.0, order))
        if len(order) >= 2:
            for i in range(len(order)):
                legs.append(Leg(zi, order[i], order[(i + 1) % len(order)]))

    terminals = {router.term_vid[c] for rp in plans for c in rp.order if router.term_vid[c] >= 0}
    for leg in legs:
        a, b = router.term_vid[leg.a], router.term_vid[leg.b]
        if a < 0 or b < 0:
            continue
        blocked = np.zeros(router.N, dtype=bool)
        blocked[list(terminals - {a, b})] = True
        leg.verts = router.route_greedy(a, b, blocked) or router.route_greedy(a, b)
        leg.segs = router.segments_of(leg.verts) if leg.verts else []

    out = finalize(ctx, Design(blocks, plans, legs, {}))
    report = validate(plan, out, cfg, ctx.G, ctx.legal)
    return _finish(ctx, out, report, info, t0)
