"""The optimisation engine: plan in, valid lowest-cost design out.

Pipeline for one plan
  1. context   room graph, free space, candidate node points, routing graph, cost table
  2. zones     fewest hubs: every connected, capacity-feasible split of the rooms
  3. cycles    per zone: one node point per room + visiting order (exact Held-Karp)
  4. realise   route every leg of every cycle on the routing graph; a leg is penalised
               for touching (crossing) or sharing (overlap) cable already placed
  5. improve   rip-up-and-reroute each leg, try alternative cycles per zone and the
               next-best zone splits; keep a change only if the exact J drops
  6. hubs      put each hub ON its own cable, on a straight piece inside an eligible
               room of the zone, >= 0.5 m from walls. Splitting a straight run adds
               no length and no bend, so the hub costs nothing. One-room zone: the hub
               sits 10 cm from the node (tiny out-and-back)
  7. check     validate every candidate design; best = (valid, fewest hubs, J, area gap)
  8. fallback  if nothing is valid, run the simple baseline and keep it if valid

Ranking is lexicographic, so the hub count is fixed to the minimum feasible first.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass

import numpy as np
from shapely.geometry import LineString, Point

from .assignment import lower_bound, partitions, rooms_of, valid_groups
from .candidates import build_candidates
from .config import Config
from .geometry import clearance, clearance_obstacles, door_axis, free_space
from .metrics import compute as compute_metrics
from .metrics import length
from .room_graph import build_room_graph
from .routing import RoutingGraph
from .sequencing import RingPlan, best_cycles
from .validation import validate


# --------------------------------------------------------------------------- #
# 1. Context
# --------------------------------------------------------------------------- #
@dataclass
class Context:
    plan: object
    cfg: Config
    G: object
    space: object
    legal: object
    obst: object
    router: RoutingGraph
    room_of: list[str]
    room_cands: dict[str, list[int]]
    fallback_rooms: set[str]
    D: np.ndarray
    node_id_of: dict[str, str]
    mode: str


def guide_lines(G, cfg) -> tuple[list[float], list[float]]:
    """Door centre lines (+ parallel lanes for in-and-out runs) and opening centres."""
    gx, gy = [], []
    for _, _, d in G.edges(data=True):
        for ln in d["links"]:
            c = ln["portal"]
            gx.append(c.x)
            gy.append(c.y)
            if ln["kind"] == "door":
                u, width, _, _ = door_axis(ln["geom"])
                off = min(cfg.door_parallel_offset_m, width / 2 - cfg.wall_margin_m - 0.03)
                if off > 0.02:
                    if abs(u[0]) >= abs(u[1]):
                        gx += [c.x - off, c.x + off]
                    else:
                        gy += [c.y - off, c.y + off]
    return gx, gy


def build_context(plan, cfg: Config, mode: str) -> Context:
    G = build_room_graph(plan, cfg)
    space = free_space(plan, G, cfg.wall_margin_m)
    legal = free_space(plan, G, 2 * cfg.tol_m)
    pts, room_of, room_cands, fallback = build_candidates(plan, G, cfg, mode)
    gx, gy = guide_lines(G, cfg)
    router = RoutingGraph(space, pts, gx, gy, cfg.w_bend)
    D = router.all_pairs()

    fixed = {n["room_id"]: n["node_id"] for n in (plan.fixed_nodes or [])} if mode == "fixed" else {}
    node_id_of, used, k = {}, set(fixed.values()), 0
    for r in plan.eligible_rooms:
        if r.id in fixed:
            node_id_of[r.id] = fixed[r.id]
            continue
        k += 1
        while f"N{k}" in used:
            k += 1
        node_id_of[r.id] = f"N{k}"
        used.add(f"N{k}")
    return Context(plan, cfg, G, space, legal, clearance_obstacles(plan, cfg.clearance_from_doors),
                   router, room_of, room_cands,
                   fallback, D, node_id_of, mode)


def single_room_cost(cfg: Config) -> float:
    d = cfg.hub_offset_m
    return cfg.w_length * 2 * d + cfg.w_overlap * d + cfg.w_bend * 2


# --------------------------------------------------------------------------- #
# 4-5. Realise and improve one design (cycles through nodes, no hubs yet)
# --------------------------------------------------------------------------- #
@dataclass
class Leg:
    ring: int
    a: int                 # candidate index (start node)
    b: int                 # candidate index (end node)
    verts: list[int] | None = None
    segs: list[int] | None = None


@dataclass
class Design:
    blocks: list[list[str]]
    plans: list[RingPlan]
    legs: list[Leg]
    cycle_metrics: dict
    out: dict | None = None
    report: dict | None = None

    @property
    def J(self) -> float:
        return (self.out or {}).get("reported_metrics", self.cycle_metrics)["cost_J"]


def realize(ctx: Context, blocks, plans, optimize: bool, t_end: float) -> Design:
    r, cfg = ctx.router, ctx.cfg
    legs: list[Leg] = []
    for zi, rp in enumerate(plans):
        m = len(rp.order)
        if m >= 2:
            for i in range(m):
                legs.append(Leg(zi, rp.order[i], rp.order[(i + 1) % m]))
    term_v = {r.term_vid[c] for rp in plans for c in rp.order if r.term_vid[c] >= 0}

    for k in range(len(legs)):
        _route_leg(ctx, legs, k, term_v, penalize=optimize)
    m = compute_metrics(_cycle_out(ctx, plans, legs), cfg)

    if optimize:  # rip-up and reroute: keep a new path only if the exact J improves
        for _ in range(cfg.reroute_passes):
            improved = False
            for k in range(len(legs)):
                if time.time() > t_end:
                    break
                old = (legs[k].verts, legs[k].segs)
                _route_leg(ctx, legs, k, term_v, penalize=True)
                if legs[k].verts == old[0]:
                    continue
                m2 = compute_metrics(_cycle_out(ctx, plans, legs), cfg)
                if m2["cost_J"] < m["cost_J"] - 1e-9:
                    m, improved = m2, True
                else:
                    legs[k].verts, legs[k].segs = old
            if not improved:
                break
    return Design(blocks, plans, legs, m)


def _route_leg(ctx: Context, legs: list[Leg], k: int, term_v: set[int], penalize: bool) -> None:
    r, cfg, leg = ctx.router, ctx.cfg, legs[k]
    a, b = r.term_vid[leg.a], r.term_vid[leg.b]
    if a < 0 or b < 0:
        leg.verts, leg.segs = None, []
        return
    blocked = np.zeros(r.N, dtype=bool)
    blocked[list(term_v - {a, b})] = True
    used_seg = used_vert = None
    if penalize:
        used_seg = np.zeros(r.S, dtype=bool)
        used_vert = np.zeros(r.N, dtype=bool)
        for j, o in enumerate(legs):
            if j != k and o.verts is not None:
                used_seg[o.segs] = True
                used_vert[o.verts] = True
        used_vert[list(term_v)] = False
    _, verts = r.route(a, b, used_seg, used_vert, blocked, cfg.w_overlap, cfg.w_cross)
    leg.verts = verts
    leg.segs = r.segments_of(verts) if verts else []


def _leg_points(ctx: Context, leg: Leg) -> list[tuple[float, float]]:
    r = ctx.router
    if leg.verts:
        return r.polyline(leg.verts)
    return [r.terminals[leg.a], r.terminals[leg.b]]   # unroutable: the validator will flag it


def _cycle_out(ctx: Context, plans, legs) -> dict:
    """Hub-less view of the design, used to score cycles (hub insertion keeps J)."""
    hubs = []
    for zi, rp in enumerate(plans):
        hubs.append({"location": _xy(ctx.router.terminals[rp.order[0]]),
                     "route_segments": [{"polyline": _pl(_leg_points(ctx, l))} for l in legs if l.ring == zi]})
    nodes = [{"location": _xy(ctx.router.terminals[c])} for rp in plans for c in rp.order]
    return {"hubs": hubs, "nodes": nodes}


# --------------------------------------------------------------------------- #
# 6. Hubs on the cable + final output
# --------------------------------------------------------------------------- #
def finalize(ctx: Context, design: Design) -> dict:
    """Insert one hub per ring and build the output dict (slide-8 schema + nodes)."""
    plan, r = ctx.plan, ctx.router
    cycles = []                                         # per ring: list of (a_cand, b_cand, points)
    for zi, rp in enumerate(design.plans):
        cycles.append([(l.a, l.b, _leg_points(ctx, l)) for l in design.legs if l.ring == zi])
    node_pts = {c: r.terminals[c] for rp in design.plans for c in rp.order}

    hubs, placed_hubs, used_nodes = [], [], set()
    for zi, (rooms, rp) in enumerate(zip(design.blocks, design.plans)):
        hub_id = f"H{zi + 1}"
        others = [pts for zj, cyc in enumerate(cycles) for k, (_, _, pts) in enumerate(cyc)]
        hub = insert_hub(ctx, rooms, rp, cycles[zi], others, list(node_pts.values()), placed_hubs)
        if hub is None:                                 # should not happen; validator will flag it
            c0 = rp.order[0]
            hub = (node_pts[c0], ctx.room_of[c0], [(c0, c0, [node_pts[c0], node_pts[c0]])])
        P, room_id, ring_legs = hub
        placed_hubs.append(P)
        seq = [hub_id]
        segs = []
        for a, b, pts in ring_legs:
            used_nodes |= {c for c in (a, b) if c is not None}
            frm = hub_id if a is None else ctx.node_id_of[ctx.room_of[a]]
            to = hub_id if b is None else ctx.node_id_of[ctx.room_of[b]]
            seq.append(to)
            pl = _pl(pts)
            segs.append({"from": frm, "to": to, "length_m": round(length(pl), 4), "polyline": pl})
        hubs.append({
            "hub_id": hub_id,
            "room_id": room_id,
            "location": _xy(P),
            "assigned_rooms": list(rooms),
            "assigned_nodes": [ctx.node_id_of[x] for x in rooms],
            "closed_sequence": seq,
            "route_segments": segs,
            "ring_length_m": round(sum(s["length_m"] for s in segs), 4),
            "assigned_area_m2": round(sum(plan.room(x).area for x in rooms), 4),
        })
    room_pt = {ctx.room_of[c]: r.terminals[c] for c in used_nodes}    # node points actually cabled
    nodes = [{"node_id": ctx.node_id_of[rm.id], "room_id": rm.id, "location": _xy(room_pt[rm.id])}
             for rm in plan.eligible_rooms if rm.id in room_pt]
    out = {"plan_id": plan.plan_id, "units": "m", "hubs": hubs, "nodes": nodes}
    out["reported_metrics"] = compute_metrics(out, ctx.cfg)
    return out


def insert_hub(ctx: Context, rooms, rp: RingPlan, cycle, all_legs, node_pts, placed_hubs):
    """Find the hub point P on this ring's cable. Returns (P, room_id, legs from P back to P).

    Legs are (from_cand | None for hub, to_cand | None, points).
    """
    cfg, plan = ctx.cfg, ctx.plan
    zone_rooms = [plan.room(x) for x in rooms]

    def ok(P, own_leg_pts, avoid=None):
        p = Point(P)
        room = next((rm for rm in zone_rooms if rm.poly.buffer(cfg.tol_m).covers(p)), None)
        if room is None:
            return None
        c = clearance(plan, ctx.obst, P[0], P[1])
        if c < cfg.clearance_m + 0.002:
            return None
        if any(math.dist(P, q) < 0.01 for q in (node_pts if avoid is None else avoid) + placed_hubs):
            return None
        for pts in all_legs:                  # must not touch any other cable
            if pts is own_leg_pts or len(pts) < 2:
                continue
            if LineString(pts).distance(p) < 0.005:
                return None
        return room, c

    if len(rp.order) == 1:                    # one-room zone: hub right next to the node
        c0 = rp.order[0]
        tries = [c0]
        if ctx.mode != "fixed":               # we choose the node point: may move it to fit the hub
            room_list = ctx.room_cands[ctx.room_of[c0]]
            tries += [c for c in room_list if c != c0]
        for c in tries:
            N = ctx.router.terminals[c]
            others = [q for q in node_pts if math.dist(q, ctx.router.terminals[c0]) > 1e-9]
            for d in (cfg.hub_offset_m, cfg.hub_offset_m / 2, 0.02):
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    P = (N[0] + dx * d, N[1] + dy * d)
                    res = ok(P, None, others)
                    if res:
                        return P, res[0].id, [(None, c, [P, N]), (c, None, [N, P])]
        return None

    best = None                               # (clearance, leg index, segment index, P)
    for li, (_, _, pts) in enumerate(cycle):
        for si, (a, b) in enumerate(zip(pts, pts[1:])):
            L = math.dist(a, b)
            if L < 0.06:
                continue
            for t in (L / 2, L / 4, 3 * L / 4, 0.03, L - 0.03):
                P = (a[0] + (b[0] - a[0]) * t / L, a[1] + (b[1] - a[1]) * t / L)
                res = ok(P, pts)
                if res and (best is None or res[1] > best[0]):
                    best = (res[1], li, si, P, res[0].id)
    if best is None:
        return None
    _, li, si, P, room_id = best
    a_c, b_c, pts = cycle[li]
    first = (None, b_c, [P, *pts[si + 1:]])           # hub -> next node
    last = (a_c, None, [*pts[: si + 1], P])           # previous node -> hub
    middle = cycle[li + 1:] + cycle[:li]
    return P, room_id, [first, *middle, last]


def _xy(p) -> dict:
    return {"x": round(float(p[0]), 6), "y": round(float(p[1]), 6)}


def _pl(points) -> list[list[float]]:
    return [[round(float(x), 6), round(float(y), 6)] for x, y in points]


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def solve(plan, cfg: Config | None = None, baseline: bool = False) -> dict:
    """Solve one plan. Returns the output dict (with `validation` and `solver` info)."""
    cfg = cfg or Config()
    t0 = time.time()
    t_end = t0 + cfg.time_budget_s
    mode = "fixed" if (cfg.node_mode == "fixed" and plan.fixed_nodes) else ("center" if baseline else "optimize")
    ctx = build_context(plan, cfg, mode)
    info = {"mode": "baseline" if baseline else "optimized", "node_mode": mode,
            "hub_lower_bound": None, "zone_splits_feasible": 0, "designs_evaluated": 0,
            "fallback_rooms": sorted(ctx.fallback_rooms),
            "dropped_rooms": plan.source.get("dropped_rooms", [])}

    elig = plan.eligible_rooms
    ids, areas = [r.id for r in elig], [r.area for r in elig]
    if not elig:
        return _finish(ctx, _empty(plan), {"valid": True, "violations": [], "warnings": ["no eligible rooms"]}, info, t0)
    too_big = [r.id for r in elig if r.area > cfg.max_area_m2 + cfg.tol_m]
    if too_big:
        return _infeasible(ctx, f"rooms {too_big} alone exceed {cfg.max_area_m2} m2", info, t0)

    lb = lower_bound(areas, cfg)
    info["hub_lower_bound"] = lb
    ok = valid_groups(ids, areas, ctx.G, cfg)
    cache: dict[int, list[RingPlan]] = {}
    one_room = single_room_cost(cfg)

    def options(mask: int) -> list[RingPlan]:
        if mask not in cache:
            rooms = rooms_of(mask, ids)
            if all(x in ctx.fallback_rooms for x in rooms):
                cache[mask] = []      # no room here can hold a hub 0.5 m from the walls
            else:
                clusters = [ctx.room_cands[x] for x in rooms]
                cache[mask] = best_cycles(ctx.D, clusters, 1 if baseline else cfg.zone_alternatives, one_room)
        return cache[mask]

    # ---- 2. fewest hubs: first k with at least one routable split ---------
    scored = []
    for k in range(lb, len(ids) + 1):
        for part in partitions(len(ids), areas, ok, k, cfg.max_area_m2):
            opts = [options(m) for m in part]
            if any(not o for o in opts):
                continue
            zone_areas = [sum(areas[i] for i in range(len(ids)) if m >> i & 1) for m in part]
            scored.append((sum(o[0].cost for o in opts), max(zone_areas) - min(zone_areas), part, opts))
        if scored:
            break
    info["zone_splits_feasible"] = len(scored)
    if not scored:
        return _infeasible(ctx, "no connected zone split can be routed (rooms unreachable?)", info, t0)
    scored.sort(key=lambda t: (t[0], t[1]))

    # ---- 3-5. realise + improve the most promising splits -----------------
    designs: list[Design] = []
    for _, _, part, opts in scored[: 1 if baseline else cfg.top_partitions]:
        blocks = [rooms_of(m, ids) for m in part]
        choice = [o[0] for o in opts]
        best = realize(ctx, blocks, choice, not baseline, t_end)
        designs.append(best)
        if baseline:
            break
        for zi, o in enumerate(opts):
            for alt in o[1:]:
                if time.time() > t_end:
                    break
                trial = choice.copy()
                trial[zi] = alt
                d = realize(ctx, blocks, trial, True, t_end)
                designs.append(d)
                if d.cycle_metrics["cost_J"] < best.cycle_metrics["cost_J"]:
                    best, choice = d, trial
        if time.time() > t_end:
            break
    info["designs_evaluated"] = len(designs)

    # ---- 6-7. hubs, validate, choose ----------------------------------------
    for d in designs:
        d.out = finalize(ctx, d)
    designs.sort(key=lambda d: (d.out["reported_metrics"]["cost_J"], d.out["reported_metrics"]["area_gap_m2"]))
    chosen = None
    for d in designs:
        d.report = validate(plan, d.out, cfg, ctx.G, ctx.legal)
        if d.report["valid"]:
            chosen = d
            break
    if chosen is None:
        chosen = designs[0]
        if not baseline:  # ---- 8. fallback
            fb = solve(plan, cfg, baseline=True)
            if fb["validation"]["valid"]:
                fb["solver"]["note"] = "optimised designs were invalid; simple baseline used"
                return fb
    return _finish(ctx, chosen.out, chosen.report, info, t0)


def _empty(plan) -> dict:
    return {"plan_id": plan.plan_id, "units": "m", "hubs": [], "nodes": [], "reported_metrics": {}}


def _infeasible(ctx, reason: str, info: dict, t0: float) -> dict:
    report = {"valid": False, "violations": [{"rule": "feasibility", "message": reason, "where": None}],
              "warnings": []}
    return _finish(ctx, _empty(ctx.plan), report, info, t0)


def _finish(ctx, out: dict, report: dict, info: dict, t0: float) -> dict:
    out["reported_metrics"] = {**compute_metrics(out, ctx.cfg), "valid": report["valid"]}
    out["validation"] = report
    info["time_s"] = round(time.time() - t0, 2)
    out["solver"] = info
    return out
