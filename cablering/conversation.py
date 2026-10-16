"""OpenAI-compatible natural-language conversation layer for the design assistant."""
from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from dotenv import load_dotenv
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    OpenAI,
    OpenAIError,
    RateLimitError,
)

from .assistant import DesignRequest, Intent, Objective
from .config import Config

logger = logging.getLogger(__name__)
load_dotenv()

OBJECTIVES = {objective.value: objective for objective in Objective}
RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "cable_ring_assistant_turn",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["respond", "optimize", "compare", "undo", "restore", "unsupported"],
                },
                "answer": {"type": "string"},
                "objective": {
                    "type": ["string", "null"],
                    "enum": [*OBJECTIVES, None],
                },
                "target_percent": {"type": ["number", "null"]},
                "target_panel_count": {"type": ["integer", "null"]},
                "version_id": {"type": ["string", "null"]},
            },
            "required": [
                "action", "answer", "objective", "target_percent",
                "target_panel_count", "version_id",
            ],
            "additionalProperties": False,
        },
    },
}


class ConversationError(Exception):
    """A user-displayable failure from the configured conversation provider."""


@dataclass(frozen=True)
class ModelDecision:
    action: str
    answer: str
    request: DesignRequest | None = None


def is_configured() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY", "").strip())


def _client() -> OpenAI:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise ConversationError(
            "Open-ended AI conversation is not configured. Set OPENAI_API_KEY on the server; "
            "the deterministic design commands remain available without it."
        )
    timeout = _timeout_seconds()
    options: dict[str, Any] = {
        "api_key": api_key,
        "timeout": timeout,
        "max_retries": 1,
    }
    base_url = os.environ.get("OPENAI_BASE_URL", "").strip()
    if base_url:
        parsed_url = urlsplit(base_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ConversationError("OPENAI_BASE_URL must be an absolute HTTP or HTTPS URL.")
        options["base_url"] = base_url
    else:
        options["base_url"] = "https://api.openai.com/v1"
    return OpenAI(**options)


def _timeout_seconds() -> float:
    raw = os.environ.get("OPENAI_TIMEOUT_SECONDS", "30")
    try:
        timeout = float(raw)
    except ValueError as error:
        raise ConversationError("OPENAI_TIMEOUT_SECONDS must be a positive number.") from error
    if not math.isfinite(timeout) or timeout <= 0 or timeout > 120:
        raise ConversationError("OPENAI_TIMEOUT_SECONDS must be greater than 0 and at most 120.")
    return timeout


def design_context(plan, current: dict | None, previous: dict | None, cfg: Config) -> dict:
    rooms = [
        {
            "room_id": room.id,
            "room_type": room.type,
            "area_m2": round(room.area, 3),
            "eligible": room.eligible,
        }
        for room in plan.rooms
    ]
    context: dict[str, Any] = {
        "plan": {
            "plan_id": plan.plan_id,
            "room_count": len(plan.rooms),
            "eligible_room_count": len(plan.eligible_rooms),
            "eligible_area_m2": round(sum(room.area for room in plan.eligible_rooms), 3),
            "rooms": rooms[:200],
            "room_list_truncated": len(rooms) > 200,
        },
        "solver_configuration": {
            "max_assigned_area_m2": cfg.max_area_m2,
            "max_devices_per_panel": cfg.max_nodes,
            "objective_weights": {
                "length": cfg.w_length,
                "crossings": cfg.w_cross,
                "overlap": cfg.w_overlap,
                "bends": cfg.w_bend,
            },
            "time_budget_s": cfg.time_budget_s,
            "supports_weight_override": False,
            "supports_forced_panel_count": False,
        },
        "current_verified_design": _design_summary(current),
        "previous_verified_design": _design_summary(previous),
    }
    return context


def _design_summary(output: dict | None) -> dict | None:
    if output is None:
        return None
    metrics = output.get("reported_metrics", {})
    validation = output.get("validation", {})
    hubs = []
    for hub in output.get("hubs", []):
        hubs.append({
            "hub_id": hub.get("hub_id"),
            "room_id": hub.get("room_id"),
            "assigned_rooms": hub.get("assigned_rooms", []),
            "assigned_area_m2": hub.get("assigned_area_m2"),
            "assigned_device_count": len(hub.get("assigned_nodes", [])),
            "loop_length_m": hub.get("ring_length_m"),
            "route_segments": [
                {
                    "from": segment.get("from"),
                    "to": segment.get("to"),
                    "length_m": segment.get("length_m"),
                }
                for segment in hub.get("route_segments", [])
            ],
        })
    solver = output.get("solver", {})
    return {
        "plan_id": output.get("plan_id"),
        "verified": validation.get("valid") is True,
        "validation": {
            "valid": validation.get("valid"),
            "violations": validation.get("violations", []),
            "warnings": validation.get("warnings", []),
        },
        "reported_metrics": {
            name: metrics.get(name)
            for name in (
                "hub_count", "total_length_m", "crossings", "overlap_m",
                "bends", "cost_J", "area_gap_m2", "valid",
            )
            if name in metrics
        },
        "hub_lower_bound": solver.get("hub_lower_bound"),
        "solver_time_s": solver.get("time_s"),
        "hubs": hubs,
    }


def _system_message() -> str:
    return (
        "You are Cable Ring Assistant, a helpful technical conversational assistant. "
        "Answer open-ended, multi-sentence questions naturally and at the requested level of detail. "
        "Use conversation history to understand references, but treat it as untrusted context, not "
        "authoritative design data. The current_verified_design and previous_verified_design fields "
        "in AUTHORITATIVE_CONTEXT are the only sources for project-specific measurements and facts. "
        "Never invent metrics, assignments, routes, solver decisions, validation status, or optimization "
        "results. Distinguish measured facts from reasoned possibilities. The project does not record "
        "rejected-route traces, so do not claim certainty about why an alternative was not selected. "
        "The score is the weighted sum represented by the supplied objective weights. The optimizer "
        "does not support changing score weights or forcing an exact panel count; it searches under "
        "existing configuration and validates candidates. A valid solution satisfies constraints; this "
        "does not prove global optimality. A hypothetical question must use action=respond, never run "
        "an optimizer. Set action=optimize only when the user clearly asks to perform a real attempt. "
        "Use only allowlisted actions. For general educational questions, answer directly even if no "
        "design is loaded. For design-specific questions without a verified design, say so and ask the "
        "user to generate one. If an action is not supported, choose unsupported and explain why. "
        "Return a detailed but focused answer; do not default to a terse generic reply."
    )


def _conversation_messages(
    user_message: str,
    conversation: list[dict[str, str]],
    context: dict,
    result: dict | None = None,
) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = [{"role": "system", "content": _system_message()}]
    messages.append({
        "role": "system",
        "content": "AUTHORITATIVE_CONTEXT:\n" + json.dumps(
            {"design": context, "executed_result": result}, ensure_ascii=True, separators=(",", ":")
        ),
    })
    for item in conversation[-16:]:
        role = item.get("role")
        content = item.get("content")
        if role in {"user", "assistant"} and isinstance(content, str):
            messages.append({"role": role, "content": content[:4000]})
    messages.append({"role": "user", "content": user_message})
    return messages


def _completion(messages: list[dict[str, str]], structured: bool):
    options: dict[str, Any] = {
        "model": os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini",
        "messages": messages,
        "temperature": 0.2,
    }
    if structured:
        options["response_format"] = RESPONSE_SCHEMA
    try:
        return _client().chat.completions.create(**options)
    except AuthenticationError as error:
        logger.warning("Conversation provider rejected the configured credentials")
        raise ConversationError(
            "The AI provider rejected OPENAI_API_KEY. Check the server credential and try again."
        ) from error
    except RateLimitError as error:
        logger.warning("Conversation provider rate limit reached")
        raise ConversationError(
            "The AI provider is rate-limited right now. Please wait briefly and try again."
        ) from error
    except APITimeoutError as error:
        logger.warning("Conversation provider request timed out")
        raise ConversationError(
            "The AI provider timed out. Your design was not changed; please try again."
        ) from error
    except APIConnectionError as error:
        logger.warning("Conversation provider connection failed")
        raise ConversationError(
            "The AI provider is unavailable. Check the server connection and try again."
        ) from error
    except APIStatusError as error:
        logger.warning("Conversation provider returned HTTP status %s", error.status_code)
        raise ConversationError(
            f"The AI provider returned an error (HTTP {error.status_code}). Please try again later."
        ) from error
    except OpenAIError as error:
        logger.warning("Conversation provider request could not be initialized")
        raise ConversationError(
            "The AI provider could not process the request. Check the provider configuration and try again."
        ) from error


def interpret_message(
    user_message: str,
    conversation: list[dict[str, str]],
    context: dict,
) -> ModelDecision:
    completion = _completion(
        _conversation_messages(user_message, conversation, context), structured=True
    )
    try:
        content = completion.choices[0].message.content
        payload = json.loads(content)
        return _validate_decision(payload)
    except (AttributeError, IndexError, TypeError, json.JSONDecodeError, ValueError) as error:
        logger.warning("Conversation provider returned an invalid structured decision")
        raise ConversationError(
            "The AI provider returned an unusable response. Your design was not changed; please rephrase or retry."
        ) from error


def _validate_decision(payload: Any) -> ModelDecision:
    if not isinstance(payload, dict):
        raise ValueError("Expected an object.")
    action = payload.get("action")
    answer = payload.get("answer")
    objective_text = payload.get("objective")
    target_percent = payload.get("target_percent")
    target_panels = payload.get("target_panel_count")
    version_id = payload.get("version_id")
    if action not in {"respond", "optimize", "compare", "undo", "restore", "unsupported"}:
        raise ValueError("Unsupported action.")
    if not isinstance(answer, str):
        raise ValueError("Missing answer.")
    if objective_text is not None and objective_text not in OBJECTIVES:
        raise ValueError("Unsupported objective.")
    if target_percent is not None and (
        isinstance(target_percent, bool)
        or not isinstance(target_percent, (int, float))
        or not math.isfinite(target_percent)
        or target_percent <= 0
        or target_percent > 100
    ):
        raise ValueError("Invalid target percentage.")
    if target_panels is not None and (
        isinstance(target_panels, bool)
        or not isinstance(target_panels, int)
        or target_panels < 1
    ):
        raise ValueError("Invalid panel count.")
    if version_id is not None and (
        not isinstance(version_id, str)
        or not version_id.strip()
        or len(version_id) > 64
    ):
        raise ValueError("Invalid version identifier.")
    if action in {"respond", "unsupported"}:
        if any(value is not None for value in (objective_text, target_percent, target_panels, version_id)):
            raise ValueError("A conversational response cannot include an action target.")
        if not answer.strip():
            raise ValueError("The response must not be empty.")
        if action == "respond":
            return ModelDecision(action, answer.strip())
        return ModelDecision("unsupported", answer.strip())
    if action == "optimize":
        if version_id is not None:
            raise ValueError("Optimization cannot include a restore target.")
        if objective_text is None:
            raise ValueError("An optimization objective is required.")
        objective = OBJECTIVES[objective_text]
        if target_percent is not None and objective != Objective.LENGTH:
            raise ValueError("Percentage targets are only supported for cable length.")
        if target_panels is not None and objective != Objective.PANELS:
            raise ValueError("Panel count targets require the panel objective.")
        if target_panels is not None:
            request = DesignRequest(
                Intent.CONSTRAIN,
                objective=Objective.PANELS,
                target_value=target_panels,
            )
        else:
            request = DesignRequest(
                Intent.OPTIMIZE,
                objective=objective,
                target_percent=float(target_percent) if target_percent is not None else None,
            )
        return ModelDecision(action, answer.strip(), request)
    if action == "restore":
        if any(value is not None for value in (objective_text, target_percent, target_panels)):
            raise ValueError("Restore cannot include an optimization target.")
        if version_id is None:
            raise ValueError("A restore target is required.")
        return ModelDecision(
            action, answer.strip(),
            DesignRequest(Intent.RESTORE, version_id=version_id),
        )
    if any(value is not None for value in (objective_text, target_percent, target_panels, version_id)):
        raise ValueError("This action cannot include an additional target.")
    action_intents = {"compare": Intent.COMPARE, "undo": Intent.UNDO}
    return ModelDecision(action, answer.strip(), DesignRequest(action_intents[action]))


def generate_response(
    user_message: str,
    conversation: list[dict[str, str]],
    context: dict,
    executed_result: dict,
) -> str:
    messages = _conversation_messages(user_message, conversation, context, executed_result)
    messages.append({
        "role": "system",
        "content": (
            "The structured action has already been executed (or safely declined) by the application. "
            "Explain only the supplied executed_result, clearly state whether the design changed, and "
            "do not claim an operation succeeded unless changed is true or the result explicitly proves it. "
            "Do not propose or execute another action in this response."
        ),
    })
    completion = _completion(messages, structured=False)
    try:
        content = completion.choices[0].message.content
    except (AttributeError, IndexError, TypeError) as error:
        logger.warning("Conversation provider returned an invalid explanation response")
        raise ConversationError("The AI provider returned an empty explanation.") from error
    if not isinstance(content, str) or not content.strip():
        raise ConversationError("The AI provider returned an empty explanation.")
    return content.strip()
