"""FastAPI backend for the React cable-ring interface."""
from __future__ import annotations
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

import base64
import copy
import io
import logging
import re
import sqlite3
import uuid
from dataclasses import asdict, replace
from typing import Literal

import matplotlib.pyplot as plt
import plotly
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field
from plotly.offline import get_plotlyjs
from shapely import affinity

from cablering.assistant import DesignHistory, Intent, handle_request, parse_request
from cablering.config import DATA_PATH, Config
from cablering import conversation as conversation_ai
from cablering.export_json import check_schema
from cablering.input_loader import (
    load_dataset,
    plan_from_index,
    plan_from_input_dict,
    raw_to_input_dict,
)
from shapely.ops import unary_union

from cablering.geometry import parts
from cablering.optimizer import solve_with_fixed_points
from cablering.optimizer import solve as solve_plan
from cablering.plan_history import PlanHistoryStore
from cablering.visualization import draw
from cablering.view3d import build_figure, standalone_html
from cablering import whatif

app = FastAPI(title="Cable Ring Optimizer API")
logger = logging.getLogger(__name__)
plans: dict[str, object] = {}
plan_inputs: dict[str, dict] = {}
design_histories: dict[str, DesignHistory] = {}
plan_configs: dict[str, Config] = {}
results: dict[str, dict] = {}
scenario_plans: dict[str, object] = {}      # plan_key -> plan as modified by the last what-if
plan_history_store = PlanHistoryStore(DATA_PATH.parent / "plan_history.sqlite3")
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
    generation_id: str = Field(
        default_factory=lambda: uuid.uuid4().hex,
        min_length=1,
        max_length=64,
        pattern="^[A-Za-z0-9_-]+$",
    )
    max_nodes: int = Field(default=Config.max_nodes, ge=1, le=20)
    node_mode: str = Field(default="optimize", pattern="^(optimize|fixed)$")
    zone_via_other_rooms: bool = False
    time_budget_s: int = Field(default=int(Config.time_budget_s), ge=5, le=120)
    compare: bool = True


class RenamePlanRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class ConversationMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=4000)


class AssistantRequest(BaseModel):
    plan_key: str
    message: str = Field(min_length=1, max_length=8000)
    conversation: list[ConversationMessage] = Field(default_factory=list, max_length=30)
    view: Literal["optimized", "greedy_baseline"] = "optimized"


def _image_data(plan, output: dict, title: str | None = None, **draw_options) -> str:
    fig, ax = plt.subplots(figsize=(11, 9))
    draw(plan, output, ax, title=title, **draw_options)
    fig.tight_layout()
    image = io.BytesIO()
    fig.savefig(image, format="png", dpi=120, bbox_inches="tight", pad_inches=0.08)
    plt.close(fig)
    return "data:image/png;base64," + base64.b64encode(image.getvalue()).decode("ascii")


def _plan_input_dict(plan) -> dict:
    scale = plan.scale

    def drawing_units(geometry):
        return affinity.scale(geometry, scale, scale, origin=(0, 0))

    raw = {
        "area": plan.inner.area * scale * scale,
        "wall_depth": plan.wall_thickness * scale,
        "inner": drawing_units(plan.inner),
        "wall": drawing_units(plan.walls),
        "window": drawing_units(plan.windows),
        "door": drawing_units(unary_union(plan.doors)) if plan.doors else None,
        "front_door": drawing_units(plan.front_door),
        "balcony": drawing_units(plan.outside),
    }
    room_geometries: dict[str, list] = {}
    for room in plan.rooms:
        room_geometries.setdefault(room.type, []).append(drawing_units(room.poly))
    for room_type, geometries in room_geometries.items():
        raw[room_type] = unary_union(geometries)
    nodes = None
    if plan.fixed_nodes:
        nodes = [
            {
                **{key: value for key, value in node.items() if key not in {"x", "y"}},
                "x": node["x"] * scale,
                "y": node["y"] * scale,
            }
            for node in plan.fixed_nodes
        ]
    raw["area"] = plan.inner.area * scale * scale
    input_data = raw_to_input_dict(raw, plan.plan_id, plan.source)
    input_data["nodes"] = nodes
    return input_data


def _register_plan(plan, input_data: dict | None = None) -> dict:
    key = uuid.uuid4().hex
    plans[key] = plan
    plan_inputs[key] = copy.deepcopy(input_data) if input_data is not None else _plan_input_dict(plan)
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
    return {
        "dataset_available": DATA_PATH.exists(),
        "assistant_ai_available": conversation_ai.is_configured(),
    }


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
        dataset = load_dataset()
        plan = plan_from_index(index, dataset)
        input_data = raw_to_input_dict(dataset[index], plan.plan_id, plan.source)
        return _register_plan(plan, input_data)
    except (IndexError, ValueError, KeyError) as error:
        raise HTTPException(status_code=400, detail=f"Could not load plan: {error}") from error


@app.post("/api/plans/upload")
def upload_plan(payload: dict = Body(...)) -> dict:
    try:
        plan = plan_from_input_dict(payload)
    except Exception as error:
        raise HTTPException(status_code=400, detail=f"Could not read this file: {error}") from error
    return _register_plan(plan, payload)


@app.get("/api/history")
def list_plan_history(
    q: str = Query(default="", max_length=120),
    sort: Literal["newest", "oldest", "recently_accessed"] = "newest",
) -> dict:
    try:
        return {"plans": plan_history_store.list(q, sort)}
    except (sqlite3.Error, ValueError) as error:
        logger.exception("Could not list saved plans")
        raise HTTPException(status_code=500, detail="Could not load saved plan history.") from error


@app.get("/api/history/by-building/{building_ref}")
def latest_saved_plan_for_building(building_ref: str) -> dict:
    try:
        metadata = plan_history_store.latest_for_building_ref(building_ref)
    except sqlite3.Error as error:
        logger.exception("Could not find saved plan for %s", building_ref)
        raise HTTPException(status_code=500, detail="Could not search saved plan history.") from error
    if metadata is None:
        raise HTTPException(status_code=404, detail="No saved plan exists for this plan number.")
    return metadata


@app.get("/api/history/{record_id}")
def open_saved_plan(record_id: str) -> dict:
    try:
        record = plan_history_store.get(record_id)
    except (sqlite3.Error, ValueError) as error:
        logger.exception("Could not read saved plan %s", record_id)
        raise HTTPException(status_code=500, detail="Could not open the saved plan.") from error
    if record is None:
        raise HTTPException(status_code=404, detail="Saved plan not found.")

    try:
        plan = plan_from_input_dict(record["input"])
        config = Config(**record["metadata"]["generation_settings"])
    except (KeyError, TypeError, ValueError) as error:
        logger.exception("Saved plan %s could not be restored", record_id)
        raise HTTPException(status_code=500, detail="Saved plan data could not be restored.") from error

    registered = _register_plan(plan, record["input"])
    plan_key = registered["plan_key"]
    saved_result = record["result"]
    plans[plan_key] = plan
    results[plan_key] = {
        "output": saved_result["output"],
        "baseline": saved_result["baseline"],
        "cfg": config,
    }
    plan_configs[plan_key] = config
    design_history = DesignHistory()
    design_history.initialize(saved_result["output"])
    design_histories[plan_key] = design_history

    try:
        metadata = plan_history_store.record_access(record_id)
    except (sqlite3.Error, ValueError) as error:
        for registry in (plans, plan_inputs, results, plan_configs, design_histories):
            registry.pop(plan_key, None)
        logger.exception("Could not update access stats for saved plan %s", record_id)
        raise HTTPException(status_code=500, detail="Could not record saved plan access.") from error
    if metadata is None:
        for registry in (plans, plan_inputs, results, plan_configs, design_histories):
            registry.pop(plan_key, None)
        raise HTTPException(status_code=404, detail="Saved plan not found.")

    return {
        "metadata": metadata,
        "plan": registered,
        "result": {**saved_result, "history_id": record_id},
    }


@app.patch("/api/history/{record_id}")
def rename_saved_plan(record_id: str, request: RenamePlanRequest) -> dict:
    name = request.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Plan name cannot be blank.")
    try:
        metadata = plan_history_store.rename(record_id, name)
    except sqlite3.Error as error:
        logger.exception("Could not rename saved plan %s", record_id)
        raise HTTPException(status_code=500, detail="Could not rename the saved plan.") from error
    if metadata is None:
        raise HTTPException(status_code=404, detail="Saved plan not found.")
    return metadata


@app.delete("/api/history/{record_id}")
def delete_saved_plan(record_id: str) -> dict:
    try:
        deleted = plan_history_store.delete(record_id)
    except sqlite3.Error as error:
        logger.exception("Could not delete saved plan %s", record_id)
        raise HTTPException(status_code=500, detail="Could not delete the saved plan.") from error
    if not deleted:
        raise HTTPException(status_code=404, detail="Saved plan not found.")
    return {"deleted": True}


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

    try:
        previous_generation = plan_history_store.find_by_idempotency_key(request.generation_id)
    except sqlite3.Error as error:
        logger.exception("Could not check saved generation %s", request.generation_id)
        raise HTTPException(status_code=500, detail="Could not check saved plan history.") from error
    if previous_generation is not None:
        saved_result = previous_generation["result"]
        cfg = Config(**previous_generation["metadata"]["generation_settings"])
        results[request.plan_key] = {
            "output": saved_result["output"],
            "baseline": saved_result["baseline"],
            "cfg": cfg,
        }
        plan_configs[request.plan_key] = cfg
        history = design_histories.setdefault(request.plan_key, DesignHistory())
        history.initialize(saved_result["output"])
        return {**saved_result, "history_id": previous_generation["id"]}

    if plan.source.get("dataset"):
        try:
            existing_plan = plan_history_store.latest_for_building_ref(plan.plan_id)
            if existing_plan is not None:
                plan_history_store.record_access(existing_plan["id"])
                saved_plan = plan_history_store.get(existing_plan["id"])
                if saved_plan is None:
                    raise HTTPException(
                        status_code=404, detail="The saved plan no longer exists."
                    )
                saved_result = saved_plan["result"]
                cfg = Config(**saved_plan["metadata"]["generation_settings"])
                results[request.plan_key] = {
                    "output": saved_result["output"],
                    "baseline": saved_result["baseline"],
                    "cfg": cfg,
                }
                plan_configs[request.plan_key] = cfg
                history = design_histories.setdefault(request.plan_key, DesignHistory())
                history.initialize(saved_result["output"])
                return {**saved_result, "history_id": saved_plan["id"]}
        except sqlite3.Error as error:
            logger.exception("Could not check saved plan %s", plan.plan_id)
            raise HTTPException(
                status_code=500, detail="Could not check saved plan history."
            ) from error

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

    history = design_histories.setdefault(request.plan_key, DesignHistory())
    if output.get("validation", {}).get("valid"):
        if history.current is None:
            history.initialize(output)
        else:
            history.accept(output, "Generate with current settings")
    plan_configs[request.plan_key] = copy.deepcopy(cfg)
    response = {
        "output": output,
        "baseline": baseline,
        "optimized_image": _image_data(plan, output),
        "baseline_image": _image_data(plan, baseline, f"{plan.plan_id} (greedy baseline)") if baseline else None,
        "output_json": check_schema(output).model_dump_json(by_alias=True, indent=2),
        "versions": history.list_versions(),
        "plotly_asset_url": PLOTLY_ASSET_URL,
    }
    if output.get("validation", {}).get("valid"):
        input_data = plan_inputs.get(request.plan_key)
        try:
            record = plan_history_store.save(
                name=plan.plan_id.strip() or "Saved plan",
                building_ref=plan.plan_id,
                input_data=input_data if input_data is not None else _plan_input_dict(plan),
                result=response,
                metadata={
                    "source": plan.source,
                    "generation_settings": asdict(cfg),
                },
                metrics=output.get("reported_metrics", {}),
                idempotency_key=request.generation_id,
            )
        except sqlite3.IntegrityError:
            try:
                record = plan_history_store.find_by_idempotency_key(request.generation_id)
            except sqlite3.Error as error:
                logger.exception("Could not retrieve repeated saved generation")
                raise HTTPException(
                    status_code=500, detail="Could not save the generated plan."
                ) from error
            if record is None:
                logger.exception("Saved generation idempotency conflict without a record")
                raise HTTPException(
                    status_code=500, detail="Could not save the generated plan."
                )
        except (sqlite3.Error, TypeError, ValueError) as error:
            logger.exception("Could not persist generated plan %s", plan.plan_id)
            raise HTTPException(
                status_code=500, detail="Could not save the generated plan."
            ) from error
        response["history_id"] = record["id"]
    return response


@app.post("/api/assistant")
def assistant(request: AssistantRequest) -> dict:
    plan = plans.get(request.plan_key)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan session expired. Load the plan again.")
    history = design_histories.setdefault(request.plan_key, DesignHistory())
    cfg = plan_configs.get(request.plan_key, Config())
    conversation = [message.model_dump() for message in request.conversation]
    parsed = parse_request(request.message, conversation)
    greedy_output = (results.get(request.plan_key) or {}).get("baseline")
    use_greedy_view = request.view == "greedy_baseline" and greedy_output is not None
    selected_output = greedy_output if use_greedy_view else history.current
    selected_previous = history.current if use_greedy_view else history.previous()
    view_overrides = (
        {"current_override": selected_output, "previous_override": selected_previous}
        if use_greedy_view else {}
    )
    local_intents = {Intent.GREETING, Intent.HELP, Intent.STATUS, Intent.NEXT_STEPS}
    read_only_intents = local_intents | {Intent.EXPLAIN, Intent.COMPARE, Intent.CLARIFY, Intent.UNSUPPORTED}
    if parsed.intent in local_intents:
        result = handle_request(plan, cfg, history, parsed, **view_overrides)
    elif conversation_ai.is_configured():
        current = selected_output
        previous = selected_previous
        context = conversation_ai.design_context(plan, current, previous, cfg)
        try:
            decision = conversation_ai.interpret_message(request.message, conversation, context)
        except conversation_ai.ConversationError as error:
            logger.info("AI conversation request could not be completed: %s", error)
            if parsed.intent != Intent.CLARIFY:
                result = handle_request(
                    plan, cfg, history, parsed,
                    **(view_overrides if parsed.intent in read_only_intents else {}),
                )
                if parsed.intent in {
                    Intent.OPTIMIZE,
                    Intent.CONSTRAIN,
                    Intent.COMPARE,
                    Intent.UNDO,
                    Intent.RESTORE,
                }:
                    result["response"] += (
                        f"\n\n(The AI could not interpret this request: {error})"
                    )
            else:
                result = {
                    "response": str(error),
                    "output": current,
                    "comparison": None,
                    "changed": False,
                }
        else:
            if decision.action == "respond":
                result = {
                    "response": decision.answer,
                    "output": current,
                    "comparison": None,
                    "changed": False,
                }
            elif decision.action == "unsupported":
                result = {
                    "response": decision.answer or (
                        "That operation is not supported by the current optimizer."
                    ),
                    "output": current,
                    "comparison": None,
                    "changed": False,
                }
            elif decision.request is not None and _is_hypothetical_action(
                request.message, decision.action
            ):
                result = {
                    "response": decision.answer or (
                        "I’ll treat that as a hypothetical; no design change was made."
                    ),
                    "output": current,
                    "comparison": None,
                    "changed": False,
                }
            elif decision.request is not None:
                design_request = replace(decision.request, message=request.message)
                if design_request.intent.value == "restore":
                    available = {version["id"] for version in history.list_versions()}
                    target = design_request.version_id
                    if target != "previous" and target not in available:
                        result = {
                            "response": (
                                f"I couldn't identify valid saved design version {target!r}. "
                                "No design was changed."
                            ),
                            "output": current,
                            "comparison": None,
                            "changed": False,
                        }
                    else:
                        result = handle_request(plan, cfg, history, design_request)
                else:
                    result = handle_request(plan, cfg, history, design_request)

                if result.get("changed") or decision.action in {"compare", "optimize", "undo", "restore"}:
                    operation_context = conversation_ai.design_context(
                        plan, history.current, history.previous(), cfg
                    )
                    try:
                        result["response"] = conversation_ai.generate_response(
                            request.message,
                            conversation,
                            operation_context,
                            {
                                "action": decision.action,
                                "response": result["response"],
                                "changed": result.get("changed", False),
                                "comparison": result.get("comparison"),
                                "current_verified_design": conversation_ai.design_context(
                                    plan, result.get("output"), result.get("alternative_output"), cfg
                                )["current_verified_design"],
                            },
                        )
                    except conversation_ai.ConversationError as error:
                        logger.info("AI result explanation failed: %s", error)
                        result["response"] += (
                            "\n\n(The AI explanation service was unavailable; the verified "
                            "operation result above is from the existing optimizer.)"
                        )
            else:
                result = {
                    "response": "The AI returned an action that this application cannot execute.",
                    "output": current,
                    "comparison": None,
                    "changed": False,
                }
    else:
        result = handle_request(
            plan, cfg, history, parsed,
            **(view_overrides if parsed.intent in read_only_intents else {}),
        )
        if parsed.intent.value == "clarify":
            result["response"] = (
                "Open-ended AI conversation is not configured. Set OPENAI_API_KEY on the server "
                "to enable natural-language answers; supported deterministic design commands still work."
            )
        elif parsed.intent.value == "explain":
            if result["output"] is None:
                result["response"] = (
                    "Open-ended AI answers are not configured. Set OPENAI_API_KEY on the server "
                    "for general explanations; load a plan and run Generate for deterministic, "
                    "design-specific answers."
                )
            else:
                result["response"] += (
                    "\n\nFor broader conversational explanations, configure OPENAI_API_KEY on the server."
                )
    if result["output"] is None:
        return {**result, "versions": history.list_versions()}

    output = check_schema(result["output"]).model_dump(by_alias=True)
    alternative = result.get("alternative_output")
    alternative_image = None
    if alternative is not None:
        alternative = check_schema(alternative).model_dump(by_alias=True)
        alternative_image = _image_data(plan, alternative, f"{plan.plan_id} (compared design)")
    return {
        "response": result["response"],
        "output": output,
        "optimized_image": _image_data(plan, output),
        "alternative_image": alternative_image,
        "output_json": check_schema(output).model_dump_json(by_alias=True, indent=2),
        "comparison": result["comparison"],
        "changed": result["changed"],
        "versions": history.list_versions(),
    }


def _is_hypothetical_action(message: str, action: str) -> bool:
    if action not in {"optimize", "undo", "restore"}:
        return False
    text = message.lower()
    return bool(re.search(
        r"\bwhat\s+(?:would|will|could)\s+happen\b|\bwhat\s+if\b|\bhypothetically\b"
        r"|\bif\s+i\s+(?:asked|were to|wanted to)\b|\bwould\s+(?:the|this|it)\s+(?:change|happen|mean)\b",
        text,
    ) or re.search(
        r"\bif\s+(?:we|i|you)\s+(?:reduc(?:e|ed)|shorten|optimi[sz]e|use|try|undo|restore)\b",
        text,
    ))


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
