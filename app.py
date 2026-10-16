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
import uuid
from dataclasses import replace
from typing import Literal

import matplotlib.pyplot as plt
from fastapi import Body, FastAPI, HTTPException
from pydantic import BaseModel, Field

from cablering.assistant import DesignHistory, Intent, handle_request, parse_request
from cablering.config import DATA_PATH, Config
from cablering import conversation as conversation_ai
from cablering.export_json import check_schema
from cablering.input_loader import load_dataset, plan_from_index, plan_from_input_dict
from cablering.optimizer import solve as solve_plan
from cablering.visualization import draw

app = FastAPI(title="Cable Ring Optimizer API")
logger = logging.getLogger(__name__)
plans: dict[str, object] = {}
design_histories: dict[str, DesignHistory] = {}
plan_configs: dict[str, Config] = {}


class SolveRequest(BaseModel):
    plan_key: str
    max_nodes: int = Field(default=Config.max_nodes, ge=1, le=20)
    node_mode: str = Field(default="optimize", pattern="^(optimize|fixed)$")
    zone_via_other_rooms: bool = False
    time_budget_s: int = Field(default=int(Config.time_budget_s), ge=5, le=120)
    compare: bool = True


class ConversationMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=4000)


class AssistantRequest(BaseModel):
    plan_key: str
    message: str = Field(min_length=1, max_length=8000)
    conversation: list[ConversationMessage] = Field(default_factory=list, max_length=30)


def _image_data(plan, output: dict, title: str | None = None) -> str:
    fig, ax = plt.subplots(figsize=(11, 9))
    draw(plan, output, ax, title=title)
    fig.tight_layout()
    image = io.BytesIO()
    fig.savefig(image, format="png", dpi=120)
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
    return {
        "dataset_available": DATA_PATH.exists(),
        "assistant_ai_available": conversation_ai.is_configured(),
    }


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

    history = design_histories.setdefault(request.plan_key, DesignHistory())
    if output.get("validation", {}).get("valid"):
        if history.current is None:
            history.initialize(output)
        else:
            history.accept(output, "Generate with current settings")
    plan_configs[request.plan_key] = copy.deepcopy(cfg)
    return {
        "output": output,
        "baseline": baseline,
        "optimized_image": _image_data(plan, output),
        "baseline_image": _image_data(plan, baseline, f"{plan.plan_id} (baseline)") if baseline else None,
        "output_json": check_schema(output).model_dump_json(by_alias=True, indent=2),
        "versions": history.list_versions(),
    }


@app.post("/api/assistant")
def assistant(request: AssistantRequest) -> dict:
    plan = plans.get(request.plan_key)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan session expired. Load the plan again.")
    history = design_histories.setdefault(request.plan_key, DesignHistory())
    cfg = plan_configs.get(request.plan_key, Config())
    conversation = [message.model_dump() for message in request.conversation]
    parsed = parse_request(request.message, conversation)
    local_intents = {Intent.GREETING, Intent.HELP, Intent.STATUS, Intent.NEXT_STEPS}
    if parsed.intent in local_intents:
        result = handle_request(plan, cfg, history, parsed)
    elif conversation_ai.is_configured():
        current = history.current
        previous = history.previous()
        context = conversation_ai.design_context(plan, current, previous, cfg)
        try:
            decision = conversation_ai.interpret_message(request.message, conversation, context)
        except conversation_ai.ConversationError as error:
            logger.info("AI conversation request could not be completed: %s", error)
            if parsed.intent != Intent.CLARIFY:
                result = handle_request(plan, cfg, history, parsed)
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
        result = handle_request(plan, cfg, history, parsed)
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