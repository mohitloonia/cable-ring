"""Hard-rule checker. Works only from the plan geometry and the output JSON dict.

A design is valid only if every rule below passes (slide 3):
 1 coverage   every eligible room + its node assigned to exactly one hub
 2 capacity   <= max_area_m2 and <= max_nodes per hub
 3 zone       a hub's rooms connected through doors / open passages
 4 ring       H -> every assigned node once -> same H, legs chain end to end
 5 free space horizontal/vertical only, inside the building, never through walls,
              windows or outside, between rooms only through door / opening gaps,
              never through another node or hub
 6 hub point  inside an eligible room of its zone, >= clearance from walls
"""
from __future__ import annotations

import math

import shapely
from shapely.geometry import LineString, Point

from .geometry import clearance, clearance_obstacles, free_space, obstacles
from .room_graph import build_room_graph, zone_connected


def validate(plan, out: dict, cfg, G=None, legal=None) -> dict:
    tol = cfg.tol_m
    G = G if G is not None else build_room_graph(plan, cfg)
    legal = legal if legal is not None else free_space(plan, G, 2 * tol)
    shapely.prepare(legal)
    obst = clearance_obstacles(plan, cfg.clearance_from_doors)
    # independent of how the legal space is built: no cable may run inside a wall/window
    solid = obstacles(plan).buffer(-tol)
    shapely.prepare(solid)
    violations: list[dict] = []
    warnings: list[str] = []

    def bad(rule, msg, where=None):
        violations.append({"rule": rule, "message": msg, "where": where})

    rooms = {r.id: r for r in plan.rooms}
    eligible = {r.id for r in plan.eligible_rooms}
    hubs = out.get("hubs", [])
    nodes = {n["node_id"]: n for n in out.get("nodes", [])}
    hub_ids = {h["hub_id"] for h in hubs}

    # ---- 1 coverage -------------------------------------------------------
    assigned = [r for h in hubs for r in h["assigned_rooms"]]
    for r in sorted(eligible - set(assigned)):
        bad("coverage", f"eligible room {r} is not assigned to any hub")
    for r in sorted({r for r in assigned if assigned.count(r) > 1}):
        bad("coverage", f"room {r} is assigned to more than one hub")
    for r in sorted(set(assigned) - eligible):
        bad("coverage", f"room {r} is not an eligible room")
    node_rooms = [n["room_id"] for n in nodes.values()]
    for r in sorted(eligible):
        if node_rooms.count(r) != 1:
            bad("coverage", f"room {r} has {node_rooms.count(r)} nodes (needs exactly 1)")
    assigned_nodes = [n for h in hubs for n in h["assigned_nodes"]]
    for n in sorted(set(nodes) - set(assigned_nodes)):
        bad("coverage", f"node {n} is not assigned to any hub")
    for n in sorted({n for n in assigned_nodes if assigned_nodes.count(n) > 1}):
        bad("coverage", f"node {n} is assigned to more than one hub")

    for h in hubs:
        hid = h["hub_id"]
        zone = h["assigned_rooms"]
        # ---- 2 capacity -----------------------------------------------------
        area = sum(rooms[r].area for r in zone if r in rooms)
        if area > cfg.max_area_m2 + tol:
            bad("capacity", f"{hid} serves {area:.2f} m2 (max {cfg.max_area_m2})")
        if len(h["assigned_nodes"]) > cfg.max_nodes:
            bad("capacity", f"{hid} serves {len(h['assigned_nodes'])} nodes (max {cfg.max_nodes})")
        for n in h["assigned_nodes"]:
            if n in nodes and nodes[n]["room_id"] not in zone:
                bad("coverage", f"node {n} lies in room {nodes[n]['room_id']}, outside {hid}'s zone")
        # ---- 3 zone ----------------------------------------------------------
        if all(r in rooms for r in zone) and not zone_connected(G, zone, cfg.zone_via_other_rooms):
            bad("zone", f"{hid}'s rooms are not connected through doors / open passages")
        # ---- 6 hub point ----------------------------------------------------
        hx, hy = h["location"]["x"], h["location"]["y"]
        hroom = h.get("room_id")
        if hroom not in zone or hroom not in eligible:
            bad("hub", f"{hid} room {hroom} is not an eligible room of its zone", [hx, hy])
        elif not rooms[hroom].poly.buffer(tol).covers(Point(hx, hy)):
            bad("hub", f"{hid} is not inside room {hroom}", [hx, hy])
        c = clearance(plan, obst, hx, hy)
        if c < cfg.clearance_m - tol:
            bad("hub", f"{hid} is {c:.3f} m from a wall (min {cfg.clearance_m})", [hx, hy])
        # ---- 4 ring ----------------------------------------------------------
        seq = h["closed_sequence"]
        if len(seq) < 3 or seq[0] != hid or seq[-1] != hid:
            bad("ring", f"{hid}'s ring must start and end at {hid}: {seq}")
        middle = seq[1:-1]
        if sorted(middle) != sorted(h["assigned_nodes"]) or len(set(middle)) != len(middle):
            bad("ring", f"{hid}'s ring must visit each assigned node exactly once: {seq}")
        if any(s in hub_ids for s in middle):
            bad("ring", f"{hid}'s ring passes through another hub: {seq}")
        segs = h["route_segments"]
        if len(segs) != len(seq) - 1:
            bad("ring", f"{hid} has {len(segs)} route segments for {len(seq) - 1} legs")
        locs = {hid: (hx, hy), **{k: (v["location"]["x"], v["location"]["y"]) for k, v in nodes.items()}}
        for i, s in enumerate(segs):
            if i < len(seq) - 1 and (s["from"] != seq[i] or s["to"] != seq[i + 1]):
                bad("ring", f"{hid} leg {i + 1} is {s['from']}->{s['to']}, expected {seq[i]}->{seq[i + 1]}")
            pl = s["polyline"]
            if len(pl) < 2:
                bad("ring", f"{hid} leg {s['from']}->{s['to']} has no polyline")
                continue
            for end, key in ((pl[0], s["from"]), (pl[-1], s["to"])):
                if key in locs and math.dist(end, locs[key]) > tol:
                    bad("ring", f"{hid} leg {s['from']}->{s['to']} does not end at {key}", list(end))
            _check_geometry(plan, h, s, legal, locs, tol, bad, solid)

    for r in eligible:
        for n in nodes.values():
            if n["room_id"] == r:
                p = Point(n["location"]["x"], n["location"]["y"])
                if not rooms[r].poly.buffer(tol).covers(p):
                    bad("node", f"node {n['node_id']} is not inside room {r}", [p.x, p.y])
                elif cfg.node_mode == "optimize":
                    c = clearance(plan, obst, p.x, p.y)
                    if c < cfg.clearance_m - tol:
                        warnings.append(f"node {n['node_id']} is {c:.2f} m from a wall (room too small)")
    return {"valid": not violations, "violations": violations, "warnings": warnings}


def validate_strict(plan, out: dict, cfg) -> dict:
    """Safety check, stricter than the rules: 5 mm tolerance, cable >= 3 cm from walls and
    windows, door openings 3 cm narrower on each side. A design passing this is robust to
    small differences between our checker and the organisers'."""
    import dataclasses
    strict = dataclasses.replace(cfg, tol_m=0.005)
    G = build_room_graph(plan, cfg)
    return validate(plan, out, strict, G, free_space(plan, G, 0.03))


def _check_geometry(plan, h, s, legal, locs, tol, bad, solid=None) -> None:
    pl = s["polyline"]
    label = f"{h['hub_id']} leg {s['from']}->{s['to']}"
    for a, b in zip(pl, pl[1:]):
        if abs(a[0] - b[0]) > tol and abs(a[1] - b[1]) > tol:
            bad("free_space", f"{label}: segment is not horizontal/vertical", [list(a), list(b)])
            continue
        seg = LineString([a, b]) if math.dist(a, b) > tol else None
        if seg is not None and solid is not None and not solid.is_empty and seg.intersects(solid):
            inside = seg.intersection(solid)
            if inside.length > tol:
                mid = inside.representative_point()
                bad("free_space", f"{label}: runs {inside.length:.3f} m inside a wall or window",
                    [mid.x, mid.y])
                continue
        if seg is not None and not legal.covers(seg):
            off = seg.difference(legal)
            mid = off.representative_point() if not off.is_empty else seg.centroid
            bad("free_space", f"{label}: crosses a wall, window, closed room boundary or leaves the building",
                [mid.x, mid.y])
    line = LineString(pl) if len(pl) >= 2 else None
    if line is None or line.length <= tol:
        return
    for key, p in locs.items():
        if key in (s["from"], s["to"]):
            continue
        if line.distance(Point(p)) <= tol:
            bad("ring", f"{label}: passes through {key}", list(p))
