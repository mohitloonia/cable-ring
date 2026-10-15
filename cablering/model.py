"""Internal data model. Everything here is in metres."""
from __future__ import annotations

from dataclasses import dataclass, field

from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry


@dataclass
class Room:
    id: str            # stable output ID, e.g. "R03"
    label: str         # dataset label, e.g. "bedroom_1"
    type: str          # "bedroom"
    poly: BaseGeometry
    eligible: bool

    @property
    def area(self) -> float:
        return self.poly.area


@dataclass
class Plan:
    plan_id: str
    scale: float                       # drawing units per metre
    inner: BaseGeometry                # interior footprint (rooms + partition walls)
    walls: BaseGeometry
    windows: BaseGeometry
    doors: list[Polygon]
    front_door: BaseGeometry           # the entrance: never a cable passage
    outside: BaseGeometry              # balconies, gardens, ... (drawing only)
    rooms: list[Room]
    wall_thickness: float
    fixed_nodes: list[dict] | None = None   # [{"node_id", "room_id", "x", "y"}] in metres
    source: dict = field(default_factory=dict)

    @property
    def eligible_rooms(self) -> list[Room]:
        return [r for r in self.rooms if r.eligible]

    def room(self, room_id: str) -> Room:
        for r in self.rooms:
            if r.id == room_id:
                return r
        raise KeyError(room_id)
