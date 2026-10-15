"""Score metrics, recomputed from the output polylines only (like the organiser checker).

  L  cable length: every leg of every ring, return legs included        (1 per m)
  X  crossings: points where two cables meet away from a node or hub     (5 each)
  O  overlap: length where legs lie on the same line; k legs -> (k-1)x   (1 per m)
  B  bends: direction changes along each closed ring, nodes and hub
     included; a reversal counts once                                    (0.5 each)
  J  = L + 5X + O + 0.5B
Tolerance: points / lines closer than 1 mm are the same.
"""
from __future__ import annotations

import math

from shapely.geometry import LineString

Pt = tuple[float, float]


def _dist(a: Pt, b: Pt) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def dedupe(pl, tol: float) -> list[Pt]:
    out: list[Pt] = []
    for p in pl:
        p = (float(p[0]), float(p[1]))
        if not out or _dist(p, out[-1]) > tol:
            out.append(p)
    return out


def length(pl) -> float:
    return sum(_dist(a, b) for a, b in zip(pl, pl[1:]))


# --------------------------------------------------------------------------- #
def overlap(polylines, tol: float) -> float:
    """Sum over lines of (coverage - 1) x length wherever >= 2 legs share the line."""
    horiz: list[tuple[float, float, float]] = []   # (y, x1, x2)
    vert: list[tuple[float, float, float]] = []    # (x, y1, y2)
    for pl in polylines:
        for a, b in zip(pl, pl[1:]):
            if abs(a[1] - b[1]) <= tol and abs(a[0] - b[0]) > tol:
                horiz.append((a[1], min(a[0], b[0]), max(a[0], b[0])))
            elif abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) > tol:
                vert.append((a[0], min(a[1], b[1]), max(a[1], b[1])))
    return _line_overlap(horiz, tol) + _line_overlap(vert, tol)


def _line_overlap(segs, tol: float) -> float:
    total = 0.0
    segs = sorted(segs)
    i = 0
    while i < len(segs):
        j = i
        while j + 1 < len(segs) and segs[j + 1][0] - segs[i][0] <= tol:
            j += 1
        group = segs[i:j + 1]
        if len(group) > 1:
            events = sorted([(s[1], 1) for s in group] + [(s[2], -1) for s in group],
                            key=lambda e: (e[0], e[1]))
            cover, prev = 0, None
            for x, d in events:
                if prev is not None and cover >= 2:
                    total += (cover - 1) * (x - prev)
                cover += d
                prev = x
        i = j + 1
    return total


# --------------------------------------------------------------------------- #
def _points(geom):
    if geom.is_empty:
        return
    t = geom.geom_type
    if t == "Point":
        yield (geom.x, geom.y)
    elif t in ("MultiPoint", "GeometryCollection"):
        for g in geom.geoms:
            yield from _points(g)
    # LineStrings are shared runs -> overlap, not crossings


def crossings(polylines, terminals, tol: float) -> list[Pt]:
    """Unique points where two different legs meet, away from nodes and hubs."""
    lines = [LineString(pl) for pl in polylines if len(pl) >= 2 and length(pl) > tol]
    found: dict[tuple[int, int], Pt] = {}
    for i in range(len(lines)):
        for j in range(i + 1, len(lines)):
            if not lines[i].intersects(lines[j]):
                continue
            for p in _points(lines[i].intersection(lines[j])):
                if any(_dist(p, t) <= tol for t in terminals):
                    continue
                key = (round(p[0] / tol), round(p[1] / tol))
                found.setdefault(key, p)
    return list(found.values())


# --------------------------------------------------------------------------- #
def ring_bends(leg_polylines, tol: float) -> int:
    pts: list[Pt] = []
    for pl in leg_polylines:
        for p in pl:
            p = (float(p[0]), float(p[1]))
            if not pts or _dist(p, pts[-1]) > tol:
                pts.append(p)
    if len(pts) > 1 and _dist(pts[0], pts[-1]) <= tol:
        pts.pop()
    n = len(pts)
    if n < 2:
        return 0
    dirs = []
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        d = _dist(a, b)
        dirs.append(((b[0] - a[0]) / d, (b[1] - a[1]) / d))
    return sum(1 for i in range(n)
               if abs(dirs[i][0] - dirs[(i + 1) % n][0]) > 1e-6 or abs(dirs[i][1] - dirs[(i + 1) % n][1]) > 1e-6)


# --------------------------------------------------------------------------- #
def compute(out: dict, cfg) -> dict:
    tol = cfg.tol_m
    hubs = out.get("hubs", [])
    terminals = [(h["location"]["x"], h["location"]["y"]) for h in hubs]
    terminals += [(n["location"]["x"], n["location"]["y"]) for n in out.get("nodes", [])]
    all_legs, B, L = [], 0, 0.0
    for h in hubs:
        legs = [dedupe(s["polyline"], tol) for s in h["route_segments"]]
        all_legs.extend(legs)
        L += sum(length(pl) for pl in legs)
        B += ring_bends(legs, tol)
    X = len(crossings(all_legs, terminals, tol))
    O = overlap(all_legs, tol)
    J = cfg.w_length * L + cfg.w_cross * X + cfg.w_overlap * O + cfg.w_bend * B
    areas = [h.get("assigned_area_m2", 0.0) for h in hubs]
    return {
        "hub_count": len(hubs),
        "total_length_m": round(L, 4),
        "crossings": X,
        "overlap_m": round(O, 4),
        "bends": B,
        "cost_J": round(J, 4),
        "area_gap_m2": round(max(areas) - min(areas), 4) if areas else 0.0,
    }
