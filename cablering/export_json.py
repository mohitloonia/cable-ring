"""Output JSON in the organiser format (slide 8), checked with Pydantic before writing.

Extra fields beyond slide 8 (`nodes`, `assigned_area_m2`, `validation`, `solver`)
are needed because we choose node points ourselves; checkers ignore unknown keys.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class XY(BaseModel):
    x: float
    y: float


class RouteSegment(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    from_: str = Field(alias="from")
    to: str
    length_m: float
    polyline: list[list[float]]


class HubOut(BaseModel):
    hub_id: str
    room_id: str
    location: XY
    assigned_rooms: list[str]
    assigned_nodes: list[str]
    closed_sequence: list[str]
    route_segments: list[RouteSegment]
    ring_length_m: float
    assigned_area_m2: float | None = None


class NodeOut(BaseModel):
    node_id: str
    room_id: str
    location: XY


class PlanOutput(BaseModel):
    plan_id: str
    units: Literal["m"] = "m"
    hubs: list[HubOut]
    nodes: list[NodeOut] = []
    reported_metrics: dict
    validation: dict | None = None
    solver: dict | None = None


def check_schema(out: dict) -> PlanOutput:
    return PlanOutput.model_validate(out)


def write_output(out: dict, path: str | Path) -> Path:
    model = check_schema(out)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(model.model_dump(by_alias=True), indent=2))
    return path
