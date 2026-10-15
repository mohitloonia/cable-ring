"""Candidate points for nodes and hubs.

Every candidate lies inside an eligible room, at least `clearance_m` from walls,
windows and the inner boundary. The same pool serves both nodes and hubs (a hub
must sit in an eligible room of its zone; it may share a room with a node).

Modes
  "optimize": the room's deepest point + points lined up with each door/opening
              (so the ring can pass straight through) + corner points between pairs
              of doors. The sequencer then picks one point per room.
  "center":   only the room's deepest point (simple baseline).
  "fixed":    the node point supplied in the input (nodes are not moved).
"""
from __future__ import annotations

from shapely.geometry import LineString, Point
from shapely.ops import nearest_points

from .geometry import clear_region, clearance_obstacles, grow, interior_point, shrink
from .room_graph import portals


def build_candidates(plan, G, cfg, mode: str):
    """Returns (points, room_of_point, room -> [point indices], fallback_rooms)."""
    obst_c = grow(clearance_obstacles(plan, cfg.clearance_from_doors), cfg.clearance_m + 0.02)
    inner_c = shrink(plan.inner, cfg.clearance_m + 0.02)
    fixed = {n["room_id"]: n for n in (plan.fixed_nodes or [])}

    points: list[tuple[float, float]] = []
    room_of: list[str] = []
    room_cands: dict[str, list[int]] = {}
    fallback: set[str] = set()

    for room in plan.eligible_rooms:
        if mode == "fixed" and room.id in fixed:
            pts = [(fixed[room.id]["x"], fixed[room.id]["y"])]
        else:
            region = clear_region(room.poly, obst_c, inner_c, cfg.wall_margin_m)
            if region.is_empty:
                # Room too small for the clearance: use its deepest point and report it.
                c = interior_point(room.poly)
                pts = [(c.x, c.y)]
                fallback.add(room.id)
            else:
                pts = _room_points(region, portals(G, room.id), mode)
                # Overlapping / duplicated room polygons (a data defect) would otherwise get
                # identical points; every node point must be unique across rooms.
                free = [p for p in pts if not _near_any(p, points, 0.05)]
                pts = free or _spread(region, points) or pts
        pts = _dedupe(pts, 0.05)[: cfg.candidates_per_room]
        room_cands[room.id] = []
        for p in pts:
            room_cands[room.id].append(len(points))
            points.append(p)
            room_of.append(room.id)
    return points, room_of, room_cands, fallback


def _room_points(region, room_portals, mode: str) -> list[tuple[float, float]]:
    c = interior_point(region)
    pts = [(c.x, c.y)]
    if mode != "optimize":
        return pts
    minx, miny, maxx, maxy = region.bounds
    for p in room_portals:
        # lined up with the door: cable can enter straight and reach the node with no bend
        for line in (LineString([(p.x, miny - 1), (p.x, maxy + 1)]),
                     LineString([(minx - 1, p.y), (maxx + 1, p.y)])):
            seg = region.intersection(line)
            if not seg.is_empty:
                q = nearest_points(seg, p)[0]
                pts.append((q.x, q.y))
    for i, a in enumerate(room_portals):     # corner between two doors: one bend at the node
        for b in room_portals[i + 1:]:
            for x, y in ((a.x, b.y), (b.x, a.y)):
                if region.covers(Point(x, y)):
                    pts.append((x, y))
    for p in room_portals:                   # simply closest to each door
        q = nearest_points(region, p)[0]
        pts.append((q.x, q.y))
    return pts


def _near_any(p, others, tol: float) -> bool:
    return any(abs(p[0] - q[0]) <= tol and abs(p[1] - q[1]) <= tol for q in others)


def _spread(region, taken) -> list[tuple[float, float]]:
    """Free points inside `region` away from already-used points (for duplicated rooms)."""
    minx, miny, maxx, maxy = region.bounds
    out = []
    steps = 8
    for i in range(steps + 1):
        for j in range(steps + 1):
            p = (minx + (maxx - minx) * i / steps, miny + (maxy - miny) * j / steps)
            if region.covers(Point(p)) and not _near_any(p, taken + out, 0.25):
                out.append(p)
    return out[:3]


def _dedupe(pts, tol: float):
    out: list[tuple[float, float]] = []
    for p in pts:
        if all(abs(p[0] - q[0]) > tol or abs(p[1] - q[1]) > tol for q in out):
            out.append((float(p[0]), float(p[1])))
    return out
