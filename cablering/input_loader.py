"""Read plans (ResPlan pickle or per-plan JSON) into the internal model, in metres.

Scale follows organiser slide 4:  scale = sqrt(inner.area / area)  [units per metre].
Coordinates are divided by the scale (no flip, no shift).

The organiser's per-plan JSON schema has not been published yet. Until it is, we
use our own JSON format (see `raw_to_input_dict`), which carries the same ResPlan
fields as WKT strings. When the official format arrives, only
`plan_from_input_dict` needs to change.
"""
from __future__ import annotations

import json
import pickle
from functools import lru_cache
from pathlib import Path

import shapely
from shapely import affinity
from shapely.geometry import Point
from shapely.ops import unary_union

from .config import DATA_PATH, ELIGIBLE_TYPES, MIN_ROOM_AREA_M2, OUTSIDE_TYPES, ROOM_TYPES
from .geometry import EMPTY, clean, parts
from .model import Plan, Room

GEOMETRY_KEYS = ("inner", "wall", "window", "door", "front_door", *ROOM_TYPES, *OUTSIDE_TYPES)


@lru_cache(maxsize=1)
def load_dataset(path: str = str(DATA_PATH)) -> list[dict]:
    with open(path, "rb") as f:
        return pickle.load(f)


def plan_scale(raw: dict) -> float:
    area = float(raw.get("area") or 0.0)
    inner = raw.get("inner")
    if area <= 0 or inner is None or inner.is_empty:
        raise ValueError("plan has no usable 'inner' polygon / 'area' for the scale")
    return (inner.area / area) ** 0.5


def plan_from_raw(raw: dict, plan_id: str, nodes: list[dict] | None = None,
                  source: dict | None = None) -> Plan:
    s = plan_scale(raw)

    def m(geom):
        g = clean(geom)
        return EMPTY if g.is_empty else affinity.scale(g, 1 / s, 1 / s, origin=(0, 0))

    rooms: list[Room] = []
    dropped: list[str] = []
    for t in ROOM_TYPES:
        for i, poly in enumerate(parts(raw.get(t))):
            g = m(poly)
            if g.is_empty:
                continue
            if g.area < MIN_ROOM_AREA_M2:
                dropped.append(f"{t}_{i} ({g.area:.2f} m2)")
                continue
            rooms.append(Room(f"R{len(rooms) + 1:02d}", f"{t}_{i}", t, g, t in ELIGIBLE_TYPES))
    source = {**(source or {}), **({"dropped_rooms": dropped} if dropped else {})}

    plan = Plan(
        plan_id=plan_id,
        scale=s,
        inner=m(raw.get("inner")),
        walls=m(raw.get("wall")),
        windows=m(raw.get("window")),
        doors=[d for d in (m(p) for p in parts(raw.get("door"))) if not d.is_empty],
        front_door=m(raw.get("front_door")),
        outside=unary_union([m(raw.get(t)) for t in OUTSIDE_TYPES]),
        rooms=rooms,
        wall_thickness=float(raw.get("wall_depth") or 0.2 * s) / s,
        source=source,
    )
    if nodes:
        plan.fixed_nodes = _fixed_nodes(plan, nodes, s)
    return plan


def _fixed_nodes(plan: Plan, nodes: list[dict], s: float) -> list[dict]:
    """Supplied node points (drawing units) -> metres, matched to the room containing them."""
    out = []
    for k, n in enumerate(nodes):
        x, y = float(n["x"]) / s, float(n["y"]) / s
        room_id = n.get("room_id")
        if room_id is None or room_id not in {r.id for r in plan.rooms}:
            p = Point(x, y)
            hit = [r for r in plan.eligible_rooms if r.poly.buffer(1e-6).covers(p)]
            room_id = hit[0].id if hit else None
        out.append({"node_id": n.get("node_id", f"N{k + 1}"), "room_id": room_id, "x": x, "y": y})
    return out


# --------------------------------------------------------------------------- #
# Dataset access
# --------------------------------------------------------------------------- #
def plan_id_for_index(index: int) -> str:
    return f"PLAN_{index:05d}"


def plan_from_index(index: int, data: list[dict] | None = None) -> Plan:
    raw = (data if data is not None else load_dataset())[index]
    return plan_from_raw(raw, plan_id_for_index(index),
                         source={"dataset": "ResPlan", "index": index, "resplan_id": int(raw["id"])})


# --------------------------------------------------------------------------- #
# JSON input
# --------------------------------------------------------------------------- #
def raw_to_input_dict(raw: dict, plan_id: str, source: dict | None = None) -> dict:
    """Our provisional per-plan input format (drawing units, WKT geometry)."""
    geometry = {}
    for k in GEOMETRY_KEYS:
        g = raw.get(k)
        if g is not None and not g.is_empty:
            geometry[k] = shapely.to_wkt(g, rounding_precision=6)
    return {
        "plan_id": plan_id,
        "units": "drawing",
        "area_m2": float(raw["area"]),
        "wall_depth": float(raw.get("wall_depth") or 0.0),
        "geometry": geometry,
        "nodes": None,          # or [{"node_id": "N1", "x": .., "y": .., "room_id": optional}]
        "source": source or {},
    }


def plan_from_input_dict(d: dict) -> Plan:
    missing = [k for k in ("plan_id", "area_m2", "geometry") if k not in d]
    if missing:
        raise ValueError(f"input JSON is missing {missing}")
    geom = d["geometry"]
    if "inner" not in geom:
        raise ValueError("input JSON geometry has no 'inner' polygon")
    raw = {k: shapely.from_wkt(v) for k, v in geom.items() if v}
    raw["area"] = d["area_m2"]
    raw["wall_depth"] = d.get("wall_depth")
    return plan_from_raw(raw, str(d["plan_id"]), d.get("nodes"), d.get("source"))


def load_plan(spec: str | int) -> Plan:
    """`spec` is a dataset index (e.g. 42) or a path to an input JSON file."""
    if isinstance(spec, int) or str(spec).isdigit():
        return plan_from_index(int(spec))
    return plan_from_input_dict(json.loads(Path(spec).read_text()))
