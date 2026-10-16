"""What-if engineering simulator: change the building or the rules and re-solve.

A Scenario can
  * close doors          a closed door becomes wall: no cable, no zone link through it
  * change rules         max area / nodes per hub, wall clearance, zone connectivity
  * change score weights crossing and bend weights in J
  * move a hub by hand   put one hub in a chosen room (+ nudge), keep the zones,
                         re-route that ring through it

Every difference from the official benchmark rules is listed in `deviations`, so a
scenario is never mistaken for an official result.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

from shapely.ops import unary_union

from .config import Config
from .geometry import clear_region, clearance_obstacles, grow, interior_point, shrink
from .optimizer import solve, solve_with_fixed_hubs
from .room_graph import build_room_graph

OFFICIAL = Config()


@dataclass
class HubMove:
    hub_id: str
    room_id: str
    dx: float = 0.0
    dy: float = 0.0


@dataclass
class Scenario:
    closed_doors: list[int] = field(default_factory=list)   # indices into plan.doors
    max_area_m2: float = OFFICIAL.max_area_m2
    max_nodes: int = OFFICIAL.max_nodes
    clearance_m: float = OFFICIAL.clearance_m
    zone_via_other_rooms: bool = OFFICIAL.zone_via_other_rooms
    w_cross: float = OFFICIAL.w_cross
    w_bend: float = OFFICIAL.w_bend
    hub_move: HubMove | None = None


# --------------------------------------------------------------------------- #
# What can be changed
# --------------------------------------------------------------------------- #
def door_list(plan, cfg: Config | None = None) -> list[dict]:
    """Interior doors (the ones cable can use), labelled D1, D2, ... by plan.doors index."""
    G = build_room_graph(plan, cfg or OFFICIAL)
    rooms = {r.id: r for r in plan.rooms}
    out = []
    for k, door in enumerate(plan.doors):
        link = next(((a, b) for a, b, d in G.edges(data=True)
                     for ln in d["links"] if ln["geom"] is door), None)
        if link is None:
            continue                                   # exterior door: cable never uses it
        a, b = sorted(link)
        c = door.centroid
        out.append({"index": k, "label": f"D{k + 1}", "rooms": [a, b],
                    "description": f"{a} {rooms[a].type} ↔ {b} {rooms[b].type}",
                    "x": round(c.x, 3), "y": round(c.y, 3)})
    return out


def hub_target(plan, room_id: str, cfg: Config, dx: float = 0.0, dy: float = 0.0) -> tuple[float, float]:
    """The deepest point of the room that keeps the wall clearance, nudged by (dx, dy)."""
    room = plan.room(room_id)
    obst = grow(clearance_obstacles(plan, cfg.clearance_from_doors), cfg.clearance_m + 0.02)
    region = clear_region(room.poly, obst, shrink(plan.inner, cfg.clearance_m + 0.02), cfg.wall_margin_m)
    p = interior_point(region) if not region.is_empty else interior_point(room.poly)
    return (p.x + dx, p.y + dy)


# --------------------------------------------------------------------------- #
# Applying a scenario
# --------------------------------------------------------------------------- #
def apply_plan(plan, scenario: Scenario):
    """Copy of the plan with the chosen doors turned into wall."""
    if not scenario.closed_doors:
        return plan
    closed = set(scenario.closed_doors)
    shut = [d for i, d in enumerate(plan.doors) if i in closed]
    return dataclasses.replace(
        plan,
        doors=[d for i, d in enumerate(plan.doors) if i not in closed],
        walls=unary_union([plan.walls, *shut]),
    )


def apply_config(base: Config, scenario: Scenario) -> Config:
    return dataclasses.replace(
        base,
        max_area_m2=scenario.max_area_m2,
        max_nodes=scenario.max_nodes,
        clearance_m=scenario.clearance_m,
        zone_via_other_rooms=scenario.zone_via_other_rooms,
        w_cross=scenario.w_cross,
        w_bend=scenario.w_bend,
    )


def deviations(plan, scenario: Scenario) -> list[str]:
    """Plain-language list of everything that differs from the official benchmark."""
    out = []
    if scenario.closed_doors:
        labels = ", ".join(f"D{i + 1}" for i in sorted(scenario.closed_doors))
        out.append(f"Building changed: door(s) {labels} closed (treated as wall)")
    checks = [
        ("max_area_m2", "Max area per hub", "m²"), ("max_nodes", "Max nodes per hub", ""),
        ("clearance_m", "Hub/node clearance from walls", "m"),
        ("w_cross", "Crossing weight in J", ""), ("w_bend", "Bend weight in J", ""),
    ]
    for key, label, unit in checks:
        new, old = getattr(scenario, key), getattr(OFFICIAL, key)
        if abs(float(new) - float(old)) > 1e-9:
            out.append(f"{label}: {new:g}{unit} (official {old:g}{unit})")
    if scenario.zone_via_other_rooms != OFFICIAL.zone_via_other_rooms:
        out.append("Zones may connect through bathrooms / non-eligible rooms (official: own rooms only)")
    if scenario.hub_move:
        m = scenario.hub_move
        nudge = f", nudged ({m.dx:+.1f}, {m.dy:+.1f}) m" if (m.dx or m.dy) else ""
        out.append(f"{m.hub_id} placed by hand in {m.room_id}{nudge} (official: optimizer chooses)")
    return out


def run(plan, scenario: Scenario, base: Config | None = None) -> tuple[object, dict]:
    """Solve the scenario. Returns (scenario plan, output dict with a `scenario` section)."""
    base = base or Config()
    plan2 = apply_plan(plan, scenario)
    cfg = apply_config(base, scenario)
    out = solve(plan2, cfg)
    note = None
    if scenario.hub_move:
        m = scenario.hub_move
        hub_ids = [h["hub_id"] for h in out["hubs"]]
        if m.hub_id in hub_ids:
            zi = hub_ids.index(m.hub_id)
            blocks = [h["assigned_rooms"] for h in out["hubs"]]
            point = hub_target(plan2, m.room_id, cfg, m.dx, m.dy)
            out = solve_with_fixed_hubs(plan2, cfg, blocks, {zi: point})
        else:
            note = (f"{m.hub_id} does not exist in this scenario "
                    f"(it has {len(hub_ids)} hub(s)); the hub move was skipped")
    devs = deviations(plan, scenario)
    out["scenario"] = {
        "official": not devs,
        "deviations": devs,
        "closed_doors": sorted(scenario.closed_doors),
        "note": note,
    }
    return plan2, out
