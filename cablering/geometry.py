"""Geometry helpers: cleaning, buffers, door passages, free space, clearance."""
from __future__ import annotations

import numpy as np
import shapely
from shapely.geometry import Point, Polygon
from shapely.ops import polylabel, unary_union

EMPTY = Polygon()
MITRE = "mitre"
OPEN_STRIP_M = 0.12     # half-width of the strip that bridges an open passage


def parts(geom) -> list[Polygon]:
    """Explode any geometry into its non-empty polygons."""
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    out: list[Polygon] = []
    for g in getattr(geom, "geoms", []):
        out.extend(parts(g))
    return out


def clean(geom):
    """Repair invalid geometry and keep only its polygonal parts."""
    if geom is None or geom.is_empty:
        return EMPTY
    if not geom.is_valid:
        geom = shapely.make_valid(geom)
    ps = [p for p in parts(geom) if p.area > 0]
    if not ps:
        return EMPTY
    return ps[0] if len(ps) == 1 else unary_union(ps)


def largest(geom) -> Polygon:
    ps = parts(geom)
    return max(ps, key=lambda p: p.area) if ps else EMPTY


def grow(geom, d: float):
    """Buffer with square corners (keeps rectilinear shapes rectilinear)."""
    if geom is None or geom.is_empty or d == 0:
        return geom
    return geom.buffer(d, join_style=MITRE)


def shrink(geom, d: float):
    return grow(geom, -d)


def interior_point(geom, tol: float = 0.01) -> Point | None:
    """Point deepest inside the largest part (pole of inaccessibility)."""
    p = largest(geom)
    if p.is_empty:
        return None
    try:
        return polylabel(p, tolerance=tol)
    except Exception:
        return p.representative_point()


def obstacles(plan):
    """What cable may not cross: walls and windows."""
    return unary_union([plan.walls, plan.windows])


def clearance_obstacles(plan, include_doors: bool = True):
    """What hubs/nodes keep their 0.5 m from: walls, windows and (by default) door openings."""
    if not include_doors:
        return obstacles(plan)
    return unary_union([plan.walls, plan.windows, plan.front_door, *plan.doors])


# --------------------------------------------------------------------------- #
# Doors
# --------------------------------------------------------------------------- #
def door_axis(door: Polygon) -> tuple[np.ndarray, float, float, np.ndarray]:
    """(unit vector along the door width, width, thickness, centre)."""
    mrr = door.minimum_rotated_rectangle
    if mrr.geom_type != "Polygon":
        c = np.array(door.centroid.coords[0])
        return np.array([1.0, 0.0]), 0.0, 0.0, c
    c = np.asarray(mrr.exterior.coords)[:4]
    e1, e2 = c[1] - c[0], c[2] - c[1]
    l1, l2 = float(np.hypot(*e1)), float(np.hypot(*e2))
    if l1 >= l2:
        u, width, thick = e1 / max(l1, 1e-12), l1, l2
    else:
        u, width, thick = e2 / max(l2, 1e-12), l2, l1
    return u, width, thick, np.asarray(mrr.centroid.coords[0])


def door_passage(door: Polygon, wall_t: float, margin: float) -> Polygon:
    """Rectangle through the wall where cable may cross between two rooms.

    Width = door width minus the wall margin on both jambs. Depth = door thickness
    plus a bit more than a wall on each side, so it always bridges both rooms.
    """
    u, width, thick, ctr = door_axis(door)
    w = width - 2 * margin
    if w <= 0.02:
        return EMPTY
    v = np.array([-u[1], u[0]])
    d = thick + 2 * (wall_t + 0.15)
    pts = [ctr + u * w / 2 + v * d / 2, ctr - u * w / 2 + v * d / 2,
           ctr - u * w / 2 - v * d / 2, ctr + u * w / 2 - v * d / 2]
    return Polygon(pts)


# --------------------------------------------------------------------------- #
# Free space
# --------------------------------------------------------------------------- #
def free_space(plan, G, margin: float):
    """Where cable may run, kept `margin` away from walls and windows.

    Built as: every room shrunk by `margin` (so touching rooms are separated by
    a small gap), plus door passages, plus open-passage strips, all clipped to the
    inner boundary. Cable can therefore only move between rooms through a door or
    a recognised open passage, never through a wall or the entrance.
    """
    obst = obstacles(plan)
    obst_m = grow(obst, margin)
    rooms = unary_union([shrink(r.poly, margin) for r in plan.rooms]).difference(obst_m)
    grown = {r.id: grow(r.poly, max(margin, 1e-3)) for r in plan.rooms}
    links = []
    for a, b, d in G.edges(data=True):
        # a passage may only touch the two rooms it connects, never a third room
        third = unary_union([g for rid, g in grown.items() if rid not in (a, b)])
        for ln in d["links"]:
            if ln["kind"] == "door":
                p = door_passage(ln["geom"], plan.wall_thickness, margin)
            else:
                # same strip width for router and checker, so router space is always
                # inside the checker's legal space (only the obstacle margin differs)
                p = grow(ln["geom"], OPEN_STRIP_M).difference(obst_m)
            links.append(p.difference(third))
    space = unary_union([rooms, *links]).intersection(plan.inner)
    return clean(space)


def clear_region(room_poly, obst_grown, inner_shrunk, margin: float):
    """Part of a room that is at least `clearance` from walls/windows/boundary."""
    return clean(shrink(room_poly, 2 * margin).intersection(inner_shrunk).difference(obst_grown))


def clearance(plan, obst, x: float, y: float) -> float:
    p = Point(x, y)
    return min(obst.distance(p), plan.inner.boundary.distance(p))
