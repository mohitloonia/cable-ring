"""Greedy baseline: every decision is the best-looking choice right now, never revisited.

    zones     greedy region growing: seed = biggest unassigned room; add the nearest
                        feasible room, then the room giving the cheapest expanded greedy tour
  nodes     the centre of each room (deepest point >= clearance from walls)
    order     nearest neighbour from each possible start; keep the cheapest closed tour
    paths     bend-aware shortest legal path for each greedy leg
  hub       first valid straight spot on the ring's own cable

It obeys every hard rule (doors only, connected zones, capacity), so it is a fair,
valid reference showing what the optimised engine adds. It does not search alternate
zone splits or ring orders, or reroute legs to reduce crossings and overlap.
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


def _zone_cycle_cost(router, room_cands, rooms: list[str]) -> float:
    cands = [room_cands[r][0] for r in rooms]
    order = nearest_neighbour_order(router, cands)
    tv = router.term_vid
    return sum(router.route(tv[a], tv[b])[0] for a, b in zip(order, order[1:] + order[:1]))


def greedy_zones(plan, G, cfg, router=None, room_cands=None) -> list[list[str]]:
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
            if router is None or room_cands is None:
                pick = max(fits, key=lambda r: (area[r], r))
            elif len(zone) == 1:
                source = router.term_vid[room_cands[zone[0]][0]]
                pick = min(fits, key=lambda r: router.route(
                    source, router.term_vid[room_cands[r][0]])[0])
            else:
                pick = min(fits, key=lambda r: _zone_cycle_cost(
                    router, room_cands, zone + [r]))
            zone.append(pick)
            total += area[pick]
            unassigned.discard(pick)
        zones.append(zone)
    return zones


def nearest_neighbour_order(router, cands: list[int]) -> list[int]:
    """Build nearest-neighbour tours from each start and keep the cheapest closure."""
    tv = router.term_vid
    costs = {(a, b): router.route(tv[a], tv[b])[0] for a in cands for b in cands if a != b}
    best_order, best_cost = cands, np.inf
    for start in cands:
        order, left = [start], set(cands) - {start}
        while left:
            here = order[-1]
            nxt = min(left, key=lambda c: costs[here, c])
            order.append(nxt)
            left.remove(nxt)
        total = sum(costs[a, b] for a, b in zip(order, order[1:])) + costs[order[-1], order[0]]
        if total < best_cost:
            best_order, best_cost = order, total
    return best_order


def solve_greedy(plan, cfg) -> dict:
    t0 = time.time()
    ctx = _context(plan, cfg)
    elig = plan.eligible_rooms
    info = {"mode": "greedy baseline", "node_mode": "center",
            "algorithms": "greedy region growing, nearest neighbour, bend-aware shortest paths",
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
    blocks = greedy_zones(plan, ctx.G, cfg, router, ctx.room_cands)
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
        _, leg.verts = router.route(a, b, blocked=blocked)
        if leg.verts is None:
            _, leg.verts = router.route(a, b)
        leg.segs = router.segments_of(leg.verts) if leg.verts else []

    out = finalize(ctx, Design(blocks, plans, legs, {}))
    report = validate(plan, out, cfg, ctx.G, ctx.legal)
    if not report["valid"]:
        for leg in legs:
            a, b = router.term_vid[leg.a], router.term_vid[leg.b]
            if a < 0 or b < 0:
                continue
            blocked = np.zeros(router.N, dtype=bool)
            blocked[list(terminals - {a, b})] = True
            leg.verts = router.route_greedy(a, b, blocked) or router.route_greedy(a, b)
            leg.segs = router.segments_of(leg.verts) if leg.verts else []
        fallback = finalize(ctx, Design(blocks, plans, legs, {}))
        fallback_report = validate(plan, fallback, cfg, ctx.G, ctx.legal)
        if fallback_report["valid"]:
            out, report = fallback, fallback_report
    return _finish(ctx, out, report, info, t0)
