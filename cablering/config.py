"""Every rule, weight and tunable in one place.

Hard rules come from the organiser slides (Problem Statement 4) and the original
brief. Values the organisers have not confirmed yet are marked CONFIRM.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = PROJECT_ROOT / "data" / "ResPlan.pkl"
OUTPUT_DIR = PROJECT_ROOT / "outputs"

# Rooms that get a node (and may hold a hub).
ELIGIBLE_TYPES = ("living", "bedroom", "kitchen", "storage")
# Room types that lie inside the inner boundary. Cable may pass through all of them.
ROOM_TYPES = ("living", "bedroom", "kitchen", "storage", "bathroom", "stair")
# Outdoor areas, drawn for context only. Cable may never enter them.
OUTSIDE_TYPES = ("balcony", "garden", "parking", "pool")
# Room polygons smaller than this are tracing artefacts (slivers), not rooms. They are
# dropped on load and listed in the output under solver.dropped_rooms.
MIN_ROOM_AREA_M2 = 0.5


@dataclass
class Config:
    # ---- hard rules ------------------------------------------------------
    max_area_m2: float = 100.0          # eligible-room area per hub
    max_nodes: int = 5                  # nodes per hub (set by the judges; was 20)
    clearance_m: float = 0.5            # hub (and chosen node) distance from walls
    tol_m: float = 1e-3                 # checker tolerance (1 mm)

    # ---- node handling ---------------------------------------------------
    # "optimize": we choose each node's point inside its room (>= clearance from walls)
    # "fixed":    use node points supplied in the input JSON. CONFIRM with organisers
    node_mode: str = "optimize"

    # ---- score: J = L + 5X + O + 0.5B ------------------------------------
    w_length: float = 1.0
    w_cross: float = 5.0
    w_overlap: float = 1.0
    w_bend: float = 0.5

    # ---- rule interpretations (CONFIRM) ----------------------------------
    # False: a zone must be connected through its own rooms only (strict, safe).
    # True:  a zone may also connect through non-eligible rooms (bathroom, stair).
    zone_via_other_rooms: bool = False
    # True: the 0.5 m clearance is also kept from door openings (a hub/node never sits
    # in a doorway). Safer if the checker treats doors like walls.
    clearance_from_doors: bool = True

    # ---- solver tunables -------------------------------------------------
    wall_margin_m: float = 0.05         # cable keeps this far from walls / windows
    min_open_passage_m: float = 0.6     # narrower wall-free contact is not a passage
    door_parallel_offset_m: float = 0.15  # extra lanes through doors (parallel runs)
    candidates_per_room: int = 10
    time_budget_s: float = 30.0         # per plan, after which the best design so far wins

    # ---- engine ----------------------------------------------------------
    # "quality":  run the original engine first (kept as the guaranteed fallback), then
    #             the improvement steps below; keep a result only if it is valid and
    #             better by (validity, hubs, J, area gap). Never worse than "original".
    # "original": the frozen first engine, nothing else.
    engine: str = "quality"
    # Quality-engine settings. `as_original()` resets them to the frozen original.
    hub_offset_m: float = 0.02          # one-room zone: hub this far from the node
    top_partitions: int = 6             # zone splits that get full routing
    zone_alternatives: int = 6          # alternative cycles tried per zone
    reroute_passes: int = 4             # rip-up-and-reroute passes per design
    door_links: str = "both_sides"      # "both_sides" (+ legacy links) | "legacy"
    bend_aware: bool = True             # Held-Karp + routing count bends at nodes
    extra_door_lanes: bool = True       # more legal crossing lanes through each door
    relocate_trials: int = 6            # node-sliding re-solves per plan (0 = off)
    strict_window_J: float = 0.10       # prefer strict-valid designs within this much J

    def to_dict(self) -> dict:
        return asdict(self)

    def as_original(self) -> "Config":
        """The frozen original engine, with this config's rules and limits."""
        from dataclasses import replace
        return replace(self, engine="original", hub_offset_m=0.10, top_partitions=3,
                       zone_alternatives=3, reroute_passes=3, door_links="legacy",
                       bend_aware=False, extra_door_lanes=False, relocate_trials=0,
                       strict_window_J=0.0)

    @classmethod
    def from_dict(cls, d: dict | None) -> "Config":
        d = d or {}
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
