"""FastAPI backend for the React cable-ring interface."""
from __future__ import annotations

import base64
import io
import uuid

import matplotlib.pyplot as plt
from fastapi import Body, FastAPI, HTTPException
from pydantic import BaseModel, Field

from cablering.config import DATA_PATH, Config
from cablering.export_json import check_schema
from cablering.input_loader import load_dataset, plan_from_index, plan_from_input_dict
from cablering.optimizer import solve as solve_plan
from cablering.visualization import draw

app = FastAPI(title="Cable Ring Optimizer API")
plans: dict[str, object] = {}


class SolveRequest(BaseModel):
    plan_key: str
    max_nodes: int = Field(default=Config.max_nodes, ge=1, le=50)
    node_mode: str = Field(default="optimize", pattern="^(optimize|fixed)$")
    zone_via_other_rooms: bool = False
    time_budget_s: int = Field(default=int(Config.time_budget_s), ge=5, le=120)
    compare: bool = True


def _image_data(plan, output: dict, title: str | None = None) -> str:
    fig, ax = plt.subplots(figsize=(11, 9))
    draw(plan, output, ax, title=title)
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
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Optimisation failed: {error}") from error

    return {
        "output": output,
        "baseline": baseline,
        "optimized_image": _image_data(plan, output),
        "baseline_image": _image_data(plan, baseline, f"{plan.plan_id} (baseline)") if baseline else None,
        "output_json": check_schema(output).model_dump_json(by_alias=True, indent=2),
    }