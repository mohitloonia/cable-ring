"""FastAPI backend for the React cable-ring interface."""
from __future__ import annotations

import base64
import io
import uuid

import matplotlib.pyplot as plt
import plotly
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field
from plotly.offline import get_plotlyjs

from cablering.config import DATA_PATH, Config
from cablering.export_json import check_schema
from cablering.input_loader import load_dataset, plan_from_index, plan_from_input_dict
from shapely.ops import unary_union

from cablering.geometry import parts
from cablering.optimizer import solve_with_fixed_points
from cablering.optimizer import solve as solve_plan
from cablering.visualization import draw
from cablering.view3d import build_figure, standalone_html
from cablering import whatif

app = FastAPI(title="Cable Ring Optimizer API")
plans: dict[str, object] = {}
results: dict[str, dict] = {}
scenario_plans: dict[str, object] = {}      # plan_key -> plan as modified by the last what-if
PLOTLY_ASSET_URL = f"/api/assets/plotly-{plotly.__version__}.js"


class HubMoveRequest(BaseModel):
    hub_id: str
    room_id: str
    dx: float = Field(default=0.0, ge=-5, le=5)
    dy: float = Field(default=0.0, ge=-5, le=5)


class WhatIfRequest(BaseModel):
    plan_key: str
    closed_doors: list[int] = []
    max_area_m2: float = Field(default=whatif.OFFICIAL.max_area_m2, ge=10, le=500)
    max_nodes: int = Field(default=whatif.OFFICIAL.max_nodes, ge=1, le=50)
    clearance_m: float = Field(default=whatif.OFFICIAL.clearance_m, ge=0, le=2)
    zone_via_other_rooms: bool = whatif.OFFICIAL.zone_via_other_rooms
    hub_move: HubMoveRequest | None = None


class DragRequest(BaseModel):
    """Current position of every device; the ones that differ from the optimum were dragged."""
    plan_key: str
    nodes: dict[str, list[float]] = {}   # room_id -> [x, y]
    hubs: dict[str, list[float]] = {}    # hub_id  -> [x, y]


class SolveRequest(BaseModel):
    plan_key: str
    max_nodes: int = Field(default=Config.max_nodes, ge=1, le=50)
    node_mode: str = Field(default="optimize", pattern="^(optimize|fixed)$")
    zone_via_other_rooms: bool = False
    time_budget_s: int = Field(default=int(Config.time_budget_s), ge=5, le=120)
    compare: bool = True


def _image_data(plan, output: dict, title: str | None = None, **draw_options) -> str:
    fig, ax = plt.subplots(figsize=(11, 9))
    draw(plan, output, ax, title=title, **draw_options)
    fig.tight_layout()
    image = io.BytesIO()
    fig.savefig(image, format="png", dpi=120, bbox_inches="tight", pad_inches=0.08)
    plt.close(fig)
    return "data:image/png;base64," + base64.b64encode(image.getvalue()).decode("ascii")


def _register_plan(plan) -> dict:
    key = uuid.uuid4().hex
    plans[key] = plan
    eligible = plan.eligible_rooms
    return {
        "plan_key": key,
        "plan_id": plan.plan_id,
        "room_count": len(plan.rooms),
        "eligible_count": len(eligible),
        "eligible_area_m2": sum(room.area for room in eligible),
        "image": _image_data(plan, {"hubs": [], "nodes": [], "reported_metrics": {}}),
    }


@app.get("/api/status")
def status() -> dict:
    return {"dataset_available": DATA_PATH.exists()}


@app.get(PLOTLY_ASSET_URL)
def plotly_asset() -> Response:
    return Response(
        content=get_plotlyjs(),
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.post("/api/plans/index/{index}")
def dataset_plan(index: int) -> dict:
    if index < 0 or index > 16999:
        raise HTTPException(status_code=400, detail="Plan number must be between 0 and 16999.")
    if not DATA_PATH.exists():
        raise HTTPException(status_code=404, detail="data/ResPlan.pkl not found")
    try:
        return _register_plan(plan_from_index(index, load_dataset()))
    except (IndexError, ValueError, KeyError) as error:
        raise HTTPException(status_code=400, detail=f"Could not load plan: {error}") from error


@app.post("/api/plans/upload")
def upload_plan(payload: dict = Body(...)) -> dict:
    try:
        plan = plan_from_input_dict(payload)
    except Exception as error:
        raise HTTPException(status_code=400, detail=f"Could not read this file: {error}") from error
    return _register_plan(plan)


@app.get("/api/plans/{plan_key}/3d", response_class=HTMLResponse)
def plan_3d(
    plan_key: str,
    baseline: bool = False,
    scenario: bool = False,
    wall_height: float = Query(default=2.7, ge=1.8, le=4.0),
    wall_transparency: int = Query(default=50, ge=0, le=100),
) -> HTMLResponse:
    plan = plans.get(plan_key)
    result = results.get(plan_key)
    if plan is None or result is None:
        raise HTTPException(status_code=404, detail="Solve this plan before opening its 3D view.")
    if scenario:
        plan = scenario_plans.get(plan_key)
        output = result.get("scenario")
        if plan is None or output is None:
            raise HTTPException(status_code=404, detail="Run a what-if scenario first.")
    else:
        output = result["baseline"] if baseline else result["output"]
    if output is None:
        raise HTTPException(status_code=404, detail="Baseline was not generated for this plan.")
    figure = build_figure(
        plan,
        output,
        wall_h=wall_height,
        wall_opacity=1 - wall_transparency / 100,
        height=None,
    )
    return HTMLResponse(standalone_html(
        figure,
        include_plotlyjs=PLOTLY_ASSET_URL,
        notify_parent=True,
    ))


@app.post("/api/solve")
def solve(request: SolveRequest) -> dict:
    plan = plans.get(request.plan_key)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan session expired. Load the plan again.")

    cfg = Config(
        max_nodes=request.max_nodes,
        node_mode=request.node_mode,
        zone_via_other_rooms=request.zone_via_other_rooms,
        time_budget_s=float(request.time_budget_s),
    )
    try:
        output = solve_plan(plan, cfg)
        baseline = solve_plan(plan, cfg, baseline=True) if request.compare else None
        output = check_schema(output).model_dump(by_alias=True)
        if baseline is not None:
            baseline = check_schema(baseline).model_dump(by_alias=True)
        results[request.plan_key] = {"output": output, "baseline": baseline, "cfg": cfg}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Optimisation failed: {error}") from error

    return {
        "output": output,
        "baseline": baseline,
        "optimized_image": _image_data(plan, output),
        "baseline_image": _image_data(plan, baseline, f"{plan.plan_id} (baseline)") if baseline else None,
        "output_json": check_schema(output).model_dump_json(by_alias=True, indent=2),
        "plotly_asset_url": PLOTLY_ASSET_URL,
    }


# --------------------------------------------------------------------------- #
# What-if simulator
# --------------------------------------------------------------------------- #
def _solved(plan_key: str):
    plan, result = plans.get(plan_key), results.get(plan_key)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan session expired. Load the plan again.")
    if result is None:
        raise HTTPException(status_code=409, detail="Generate the official solution first.")
    return plan, result


def _metric_summary(output: dict) -> dict:
    m = output["reported_metrics"]
    keys = ("valid", "hub_count", "cost_J", "total_length_m", "crossings", "overlap_m", "bends")
    return {k: m.get(k) for k in keys}


@app.get("/api/plans/{plan_key}/whatif")
def whatif_options(plan_key: str) -> dict:
    """What a scenario can change, plus the official solution drawn with door labels."""
    plan, result = _solved(plan_key)
    doors = whatif.door_list(plan)
    official = result["output"]
    return {
        "doors": doors,
        "rooms": [{"id": r.id, "type": r.type, "area_m2": round(r.area, 1)} for r in plan.eligible_rooms],
        "hubs": [{"hub_id": h["hub_id"], "room_id": h["room_id"], "assigned_rooms": h["assigned_rooms"]}
                 for h in official["hubs"]],
        "official_rules": {
            "max_area_m2": whatif.OFFICIAL.max_area_m2, "max_nodes": whatif.OFFICIAL.max_nodes,
            "clearance_m": whatif.OFFICIAL.clearance_m,
            "zone_via_other_rooms": whatif.OFFICIAL.zone_via_other_rooms,
            "w_cross": whatif.OFFICIAL.w_cross, "w_bend": whatif.OFFICIAL.w_bend,
        },
        "official_metrics": _metric_summary(official),
        "door_map": _image_data(plan, official, f"{plan.plan_id} (official)", door_labels=doors),
    }


@app.post("/api/whatif")
def run_whatif(request: WhatIfRequest) -> dict:
    plan, result = _solved(request.plan_key)
    valid_doors = {d["index"] for d in whatif.door_list(plan)}
    bad = [i for i in request.closed_doors if i not in valid_doors]
    if bad:
        raise HTTPException(status_code=400, detail=f"Unknown door index {bad}.")
    if request.hub_move and request.hub_move.room_id not in {r.id for r in plan.eligible_rooms}:
        raise HTTPException(status_code=400, detail="A hub can only be moved into an eligible room.")

    scenario = whatif.Scenario(
        closed_doors=request.closed_doors, max_area_m2=request.max_area_m2, max_nodes=request.max_nodes,
        clearance_m=request.clearance_m, zone_via_other_rooms=request.zone_via_other_rooms,
        hub_move=whatif.HubMove(**request.hub_move.model_dump()) if request.hub_move else None,
    )
    try:
        plan2, output = whatif.run(plan, scenario)
        scenario_info = output.pop("scenario")
        output = check_schema(output).model_dump(by_alias=True)
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Scenario failed: {error}") from error

    result["scenario"] = output
    scenario_plans[request.plan_key] = plan2
    doors = whatif.door_list(plan)
    closed = [plan.doors[i] for i in request.closed_doors]
    title = f"{plan.plan_id} ({'official rules' if scenario_info['official'] else 'what-if scenario'})"
    return {
        "output": output,
        "scenario": scenario_info,
        "metrics": _metric_summary(output),
        "official_metrics": _metric_summary(result["output"]),
        "image": _image_data(plan2, output, title, door_labels=doors, closed_doors=closed),
    }


def _rings(geom) -> list[list[list[float]]]:
    """Polygon(s) -> list of rings, each a list of [x, y] (for SVG paths)."""
    rings = []
    for p in parts(geom):
        for ring in (p.exterior, *p.interiors):
            rings.append([[round(x, 4), round(y, 4)] for x, y in ring.coords])
    return rings


@app.get("/api/plans/{plan_key}/geometry")
def plan_geometry(plan_key: str) -> dict:
    """Floor-plan shapes in metres, for the interactive drag editor."""
    plan = plans.get(plan_key)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan session expired. Load the plan again.")
    minx, miny, maxx, maxy = plan.inner.union(plan.outside).bounds
    return {
        "bounds": [minx, miny, maxx, maxy],
        "rooms": [{"id": r.id, "type": r.type, "eligible": r.eligible, "area_m2": round(r.area, 1),
                   "rings": _rings(r.poly), "label_at": list(r.poly.representative_point().coords[0])}
                  for r in plan.rooms],
        "walls": _rings(plan.walls),
        "windows": _rings(plan.windows),
        "doors": _rings(unary_union(plan.doors)) if plan.doors else [],
        "front_door": _rings(plan.front_door),
        "outside": _rings(plan.outside),
    }


@app.post("/api/drag")
def drag_devices(request: DragRequest) -> dict:
    """Re-route with devices where the user dropped them; compare with the optimum."""
    plan, result = _solved(request.plan_key)
    official = result["output"]
    blocks = [h["assigned_rooms"] for h in official["hubs"]]
    hub_index = {h["hub_id"]: i for i, h in enumerate(official["hubs"])}
    known_rooms = {n["room_id"] for n in official["nodes"]}
    if set(request.hubs) - set(hub_index) or set(request.nodes) - known_rooms:
        raise HTTPException(status_code=400, detail="Unknown device in the request.")

    def moved(old: dict, new: list[float] | None) -> bool:
        return new is not None and (abs(old["x"] - new[0]) > 0.01 or abs(old["y"] - new[1]) > 0.01)

    moved_ids = [h["hub_id"] for h in official["hubs"] if moved(h["location"], request.hubs.get(h["hub_id"]))]
    moved_ids += [n["node_id"] for n in official["nodes"] if moved(n["location"], request.nodes.get(n["room_id"]))]
    if not moved_ids:
        output = official
    else:
        # every device stays where it is (optimum or dropped); order and routes are re-optimised
        hub_pts = {hub_index[h["hub_id"]]: tuple(request.hubs.get(h["hub_id"], (h["location"]["x"], h["location"]["y"])))
                   for h in official["hubs"]}
        node_pts = {n["room_id"]: tuple(request.nodes.get(n["room_id"], (n["location"]["x"], n["location"]["y"])))
                    for n in official["nodes"]}
        try:
            output = solve_with_fixed_points(plan, result.get("cfg") or Config(), blocks, hub_pts, node_pts)
            output = check_schema(output).model_dump(by_alias=True)
        except Exception as error:
            raise HTTPException(status_code=500, detail=f"Re-routing failed: {error}") from error
    result["scenario"] = output                 # so the 3D view can show it
    scenario_plans[request.plan_key] = plan
    return {
        "output": output,
        "moved": moved_ids,
        "metrics": _metric_summary(output),
        "official_metrics": _metric_summary(official),
    }
