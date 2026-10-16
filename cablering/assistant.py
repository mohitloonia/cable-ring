"""Deterministic natural-language interface to the existing optimizer."""
from __future__ import annotations

import copy
import logging
import re
from dataclasses import dataclass
from enum import Enum
from threading import RLock
from typing import Callable

from .config import Config
from .export_json import check_schema
from .metrics import compute
from .optimizer import solve
from .validation import validate

logger = logging.getLogger(__name__)


class Intent(str, Enum):
    GREETING = "greeting"
    HELP = "help"
    STATUS = "status"
    NEXT_STEPS = "next_steps"
    EXPLAIN = "explain"
    OPTIMIZE = "optimize"
    CONSTRAIN = "constrain"
    COMPARE = "compare"
    UNDO = "undo"
    RESTORE = "restore"
    CLARIFY = "clarify"
    UNSUPPORTED = "unsupported"


class Objective(str, Enum):
    LENGTH = "total_length_m"
    CROSSINGS = "crossings"
    OVERLAP = "overlap_m"
    BENDS = "bends"
    SCORE = "cost_J"
    PANELS = "hub_count"


@dataclass(frozen=True)
class DesignRequest:
    intent: Intent
    objective: Objective | None = None
    target_value: int | None = None
    target_percent: float | None = None
    version_id: str | None = None
    message: str = ""
    scope: str | None = None
    confirmation_required: bool = False
    compare_metric: Objective | None = None

    @property
    def clarification_required(self) -> bool:
        return self.intent == Intent.CLARIFY


class DesignHistory:
    """In-memory valid snapshots scoped to one loaded plan session."""

    def __init__(self) -> None:
        self.versions: list[dict] = []
        self.current_id: str | None = None
        self._lock = RLock()

    @property
    def current(self) -> dict | None:
        with self._lock:
            version = self._find(self.current_id)
            return copy.deepcopy(version["output"]) if version else None

    def initialize(self, output: dict, request: str = "Initial solve") -> dict:
        with self._lock:
            if self.current_id is None and output.get("validation", {}).get("valid"):
                self._save(output, request)
            return self.list_versions()

    def accept(self, output: dict, request: str) -> dict:
        if not output.get("validation", {}).get("valid"):
            raise ValueError("Only a valid design can be saved.")
        with self._lock:
            self._save(output, request)
            return self.list_versions()

    def undo(self) -> dict | None:
        with self._lock:
            ids = [v["id"] for v in self.versions]
            if self.current_id not in ids:
                return None
            index = ids.index(self.current_id)
            if index == 0:
                return None
            self.current_id = ids[index - 1]
            return copy.deepcopy(self._find(self.current_id)["output"])

    def restore(self, version_id: str) -> dict | None:
        with self._lock:
            version = self._find(version_id)
            if version is None:
                return None
            self.current_id = version_id
            return copy.deepcopy(version["output"])

    def restore_previous(self) -> dict | None:
        with self._lock:
            ids = [v["id"] for v in self.versions]
            if self.current_id not in ids:
                return None
            index = ids.index(self.current_id)
            if index == 0:
                return None
            self.current_id = ids[index - 1]
            return copy.deepcopy(self._find(self.current_id)["output"])

    def previous(self) -> dict | None:
        with self._lock:
            ids = [v["id"] for v in self.versions]
            if self.current_id not in ids:
                return None
            index = ids.index(self.current_id)
            if index == 0:
                return None
            return copy.deepcopy(self._find(ids[index - 1])["output"])

    def list_versions(self) -> list[dict]:
        with self._lock:
            return [
                {"id": v["id"], "request": v["request"],
                 "metrics": copy.deepcopy(v["output"].get("reported_metrics", {})),
                 "current": v["id"] == self.current_id}
                for v in self.versions
            ]

    def _save(self, output: dict, request: str) -> None:
        version_id = str(len(self.versions) + 1)
        self.versions.append({
            "id": version_id,
            "request": request,
            "output": copy.deepcopy(output),
        })
        self.current_id = version_id

    def _find(self, version_id: str | None) -> dict | None:
        return next((v for v in self.versions if v["id"] == version_id), None)


def parse_request(message: str, conversation: list[dict[str, str]] | None = None) -> DesignRequest:
    text = re.sub(r"\s+", " ", message.strip().lower())
    scope = _extract_scope(text)
    recent_user_messages = [
        item.get("content", "").lower()
        for item in (conversation or [])[-12:]
        if item.get("role") == "user"
    ]
    prior_text = recent_user_messages[-1] if recent_user_messages else ""
    if not text:
        return DesignRequest(Intent.CLARIFY, message=message)

    if re.fullmatch(r"(hi|hello|hey|good morning|good afternoon|good evening)[!. ]*", text):
        return DesignRequest(Intent.GREETING, message=message)
    if re.search(r"\b(what can you do|what can i ask|help|show examples|examples)\b", text):
        return DesignRequest(Intent.HELP, message=message)
    if re.search(r"\b(what should i try next|what can i try next|suggest (?:something|a next step)|next steps)\b", text):
        return DesignRequest(Intent.NEXT_STEPS, message=message)

    if re.search(r"\b(is|was|are|does|did)\b.*\b(solution|design|layout)\b.*\b(valid|feasible|pass(?:es|ed)? validation)\b", text) \
            or re.search(r"\b(validation status|is it valid|does it pass validation)\b", text) \
            or re.search(r"\bcheck\b.*\b(solution|design|layout|it)\b.*\b(valid|feasible|validation)\b", text):
        return DesignRequest(Intent.STATUS, message=message)

    if re.search(r"\bwhy\b", text) and re.search(r"\b(increas(?:e|ed|ing)|went up|got worse|worsened)\b", text):
        compare_metric = None
        for terms, objective in (
            (r"\b(crossings?|intersections?)\b", Objective.CROSSINGS),
            (r"\b(overlap(?:ping)?|shared cable)\b", Objective.OVERLAP),
            (r"\b(bends?|turns?)\b", Objective.BENDS),
            (r"\b(length|cable)\b", Objective.LENGTH),
            (r"\b(score|cost|J)\b", Objective.SCORE),
        ):
            if re.search(terms, text):
                compare_metric = objective
                break
        return DesignRequest(Intent.EXPLAIN, message=message, compare_metric=compare_metric)

    if re.search(r"\b(?:try|use|make|what about)\s+(?:with\s+)?(?:just\s+)?one\b"
                 r"|\b(?:can we|could we)\s+(?:(?:use|try|make)\s+)?(?:with\s+)?(?:just\s+)?one\b", text) \
            and re.search(r"\b(panels?|hubs?)\b", prior_text):
        return DesignRequest(Intent.CONSTRAIN, Objective.PANELS, target_value=1, message=message)

    if re.search(r"\bmake it better\b", text):
        return DesignRequest(Intent.OPTIMIZE, Objective.SCORE, message=message)

    if re.search(r"\b(do that again|explain that again|repeat that)\b", text) \
            and re.search(r"\b(why|explain|how|what)\b", prior_text):
        return DesignRequest(Intent.EXPLAIN, message=message)

    if re.search(r"\b(try again|do that again|make it shorter|reduce it further|improve it further|try it again)\b", text):
        objective = _context_objective(prior_text)
        if objective is not None and objective != Objective.PANELS:
            return DesignRequest(Intent.OPTIMIZE, objective, message=message)
        if objective == Objective.PANELS and re.search(r"\b(try again|do that again|improve it further)\b", text):
            return DesignRequest(Intent.OPTIMIZE, Objective.PANELS, message=message)
        if re.search(r"\b(shorter|cable)\b", text):
            return DesignRequest(Intent.OPTIMIZE, Objective.LENGTH, message=message)
        return DesignRequest(Intent.CLARIFY, message=message)

    if re.search(r"\b(reduce|shorten|lower|make)\b.*\b(it|that|further|shorter|better)\b", text) \
            and re.search(r"\b(cable|length)\b", prior_text):
        return DesignRequest(Intent.OPTIMIZE, Objective.LENGTH, message=message)

    if re.search(r"\b(undo|revert|go back)\b", text):
        return DesignRequest(Intent.UNDO, message=message)
    if re.search(r"\brestore\b.*\b(previous|last)\b.*\b(design|solution|version)\b", text):
        return DesignRequest(Intent.RESTORE, version_id="previous", message=message)
    restore = re.search(r"\brestore\s+(?:version\s+)?(?:#?(\d+))\b", text)
    if restore:
        return DesignRequest(Intent.RESTORE, version_id=restore.group(1), message=message)
    if re.search(r"\b(restore|reinstate)\b", text):
        return DesignRequest(Intent.CLARIFY, message=message)
    if re.search(r"\b(compare|alternative designs?|previous solution|trade.off)\b", text):
        return DesignRequest(Intent.COMPARE, message=message)
    if re.search(r"\b(weight|change the scoring|prioriti[sz]e|prefer)\b", text):
        return DesignRequest(Intent.UNSUPPORTED, message=message)
    if re.search(r"\b(why|explain|how come|what prevents|what is the reason|tell me about|describe)\b", text):
        return DesignRequest(Intent.EXPLAIN, message=message, scope=scope)

    if re.search(r"\b(smaller|fewer|less)\b.*\b(panel count|panels?|hubs?)\b", text):
        return DesignRequest(Intent.OPTIMIZE, Objective.PANELS, message=message)

    if re.search(r"\b(can you|could you|please|try|use|with|using)\b.*\b(one|two|three|four|five|\d+)\b", text) \
            and re.search(r"\b(panels?|hubs?)\b", text):
        number = re.search(r"\b(one|two|three|four|five|\d+)\b", text)
        word = number.group(1)
        target = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}.get(word)
        return DesignRequest(
            Intent.CONSTRAIN, Objective.PANELS,
            target_value=target if target is not None else int(word), message=message,
        )

    panel_match = re.search(
        r"\b(?:using|with|use|exactly|to)\s+(one|two|three|four|five|\d+)\s+panels?\b"
        r"|\b(one|two|three|four|five|\d+)\s+panels?\b",
        text,
    )
    if panel_match:
        word = next((group for group in panel_match.groups() if group), None)
        target = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}.get(word)
        if target is None:
            target = int(word)
        return DesignRequest(Intent.CONSTRAIN, Objective.PANELS, target_value=target, message=message)

    if re.search(r"\b(minimi[sz]e|reduce|fewer|less)\b.*\b(panels?|hubs?)\b", text):
        return DesignRequest(Intent.OPTIMIZE, Objective.PANELS, message=message)

    if re.search(r"\b(optim(?:i[sz]e|ise|ization)|minimi[sz]e|improve|better design|make it better|reduce|fewer|less|shorter|decrease)\b", text):
        if scope:
            return DesignRequest(Intent.UNSUPPORTED, message=message, scope=scope)
        if re.search(r"\b(overlap(?:ping)?|shared cable)\b", text):
            return DesignRequest(Intent.OPTIMIZE, Objective.OVERLAP, message=message)
        if re.search(r"\b(length|cable|shorter)\b", text):
            reduction = re.search(r"\bby\s+(\d+(?:\.\d+)?)\s*%", text)
            return DesignRequest(
                Intent.OPTIMIZE, Objective.LENGTH,
                target_percent=float(reduction.group(1)) if reduction else None,
                message=message,
            )
        if re.search(r"\b(crossings?|intersections?)\b", text):
            return DesignRequest(Intent.OPTIMIZE, Objective.CROSSINGS, message=message)
        if re.search(r"\b(bends?|turns?)\b", text):
            return DesignRequest(Intent.OPTIMIZE, Objective.BENDS, message=message)
        if re.search(r"\b(score|cost|overall)\b", text) or "better design" in text or "improve" in text:
            return DesignRequest(Intent.OPTIMIZE, Objective.SCORE, message=message)
        if re.search(r"\b(energy|electricity|power consumption|installation cost|price|noise)\b", text):
            return DesignRequest(Intent.UNSUPPORTED, message=message)
        return DesignRequest(Intent.OPTIMIZE, Objective.SCORE, message=message)

    if re.search(r"\b(route|coordinates|room assignment|assigned to|node order|current design|current layout|panel count|how many panels|how many hubs|device count|how many devices|score|cost|crossings?|intersections?|bends?|overlap)\b", text) \
            and (re.search(r"\b(what|how|is|are|tell|show|current|my|the)\b", text)
                 or re.fullmatch(r"(score|cost|status|validity)[?.! ]*", text)):
        return DesignRequest(Intent.EXPLAIN, message=message, scope=scope)
    if re.search(r"\b(energy|electricity|power consumption|installation cost|price|noise)\b", text):
        return DesignRequest(Intent.UNSUPPORTED, message=message)
    return DesignRequest(Intent.CLARIFY, message=message)


def _context_objective(previous_message: str) -> Objective | None:
    if re.search(r"\b(panels?|hubs?)\b", previous_message):
        return Objective.PANELS
    for terms, objective in (
        (r"\b(overlap(?:ping)?|shared cable)\b", Objective.OVERLAP),
        (r"\b(crossings?|intersections?)\b", Objective.CROSSINGS),
        (r"\b(bends?|turns?)\b", Objective.BENDS),
        (r"\b(length|cable|shorter)\b", Objective.LENGTH),
        (r"\b(score|cost|objective|J)\b", Objective.SCORE),
    ):
        if re.search(terms, previous_message):
            return objective
    return None


def _extract_scope(text: str) -> str | None:
    route = re.search(r"\b(?:route|cable)\s+(?:from\s+)?([a-z]\d+)\s+(?:to|through)\s+([a-z]\d+)\b", text)
    if route:
        return f"{route.group(1).upper()}->{route.group(2).upper()}"
    named = re.search(r"\b(room|panel|hub|node)\s+([a-z]?\d+)\b", text)
    if named:
        kind, identifier = named.groups()
        identifier = identifier.upper()
        if identifier.isdigit():
            identifier = ("R" if kind == "room" else "N" if kind == "node" else "H") + identifier
        return identifier
    identifier = re.search(r"\b([rhn]\d+)\b", text)
    return identifier.group(1).upper() if identifier else None


_OBJECTIVE_LABELS = {
    Objective.LENGTH: "cable length",
    Objective.CROSSINGS: "crossings",
    Objective.OVERLAP: "overlap",
    Objective.BENDS: "bends",
    Objective.SCORE: "overall score J",
    Objective.PANELS: "panel count",
}


def compare_designs(before: dict, after: dict) -> dict:
    """Compare metrics as reported by solver outputs; never synthesize missing values."""
    old = before.get("reported_metrics", {})
    new = after.get("reported_metrics", {})
    fields = {
        "hub_count": True,
        "total_length_m": True,
        "crossings": True,
        "overlap_m": True,
        "bends": True,
        "cost_J": True,
        "area_gap_m2": True,
    }
    result = {}
    for name, lower_is_better in fields.items():
        a, b = old.get(name), new.get(name)
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            result[name] = {"before": a, "after": b, "delta": None, "percent_change": None,
                            "trend": "unavailable"}
            continue
        delta = b - a
        percent = (delta / abs(a) * 100) if a != 0 else (0.0 if b == 0 else None)
        trend = "unchanged" if delta == 0 else (
            "improved" if (delta < 0 if lower_is_better else delta > 0) else "worsened"
        )
        result[name] = {"before": a, "after": b, "delta": delta,
                        "percent_change": percent, "trend": trend}
    before_validation = before.get("validation")
    after_validation = after.get("validation")
    before_valid = before_validation.get("valid") if isinstance(before_validation, dict) else None
    after_valid = after_validation.get("valid") if isinstance(after_validation, dict) else None
    if isinstance(before_valid, bool) and isinstance(after_valid, bool):
        valid_trend = ("unchanged" if before_valid == after_valid
                       else "improved" if after_valid else "worsened")
        result["valid"] = {"before": before_valid, "after": after_valid, "delta": None,
                           "percent_change": None, "trend": valid_trend}
    else:
        result["valid"] = {"before": before_valid, "after": after_valid, "delta": None,
                           "percent_change": None, "trend": "unavailable"}
    before_solver = before.get("solver")
    after_solver = after.get("solver")
    before_time = before_solver.get("time_s") if isinstance(before_solver, dict) else None
    after_time = after_solver.get("time_s") if isinstance(after_solver, dict) else None
    if isinstance(before_time, (int, float)) and isinstance(after_time, (int, float)):
        delta = after_time - before_time
        percent = (delta / abs(before_time) * 100) if before_time != 0 else (0.0 if after_time == 0 else None)
        trend = "unchanged" if delta == 0 else "faster" if delta < 0 else "slower"
        result["solver_time_s"] = {"before": before_time, "after": after_time, "delta": delta,
                                   "percent_change": percent, "trend": trend}
    else:
        result["solver_time_s"] = {"before": before_time, "after": after_time, "delta": None,
                                   "percent_change": None, "trend": "unavailable"}
    return result


def handle_request(
    plan,
    cfg: Config,
    state: DesignHistory,
    request: DesignRequest,
    optimizer: Callable = solve,
    *,
    current_override: dict | None = None,
    previous_override: dict | None = None,
) -> dict:
    current = state.current if current_override is None else current_override
    previous = state.previous() if current_override is None else previous_override
    if request.intent == Intent.GREETING:
        response = "Hi! I can explain your current layout, check its validation status, ask the existing optimizer to search for improvements, and compare, undo, or restore valid designs."
        if current is None:
            response += " Load a floor plan and press Generate first."
        return {"response": response, "output": current, "comparison": None, "changed": False}
    if request.intent == Intent.HELP:
        return {
            "response": (
                "I can explain your design, panel assignments, routes, score, and validation; "
                "run the existing optimizer; compare designs; and undo or restore valid versions. "
                "Try “Explain my current design”, “Reduce cable length”, "
                "“Find a design with fewer crossings”, “Can we use one panel?”, "
                "“Compare with the previous design”, or “Undo that change”. "
                "Exact panel counts are attempts, not guaranteed constraints."
            ),
            "output": current, "comparison": None, "changed": False,
        }
    if request.intent == Intent.NEXT_STEPS:
        if current is None:
            response = "Load a floor plan and press Generate first. Then I can explain or optimize its verified design."
        else:
            response = (
                "You could ask me to reduce cable length, find a design with fewer crossings, "
                "explain the score, check validation, or compare the current design with the previous valid one."
            )
        return {"response": response, "output": current, "comparison": None, "changed": False}
    if request.intent == Intent.CLARIFY:
        return {"response": "I’m not sure which change you mean. Do you mean panel count, cable length, crossings, or something else?",
                "output": current, "comparison": None, "changed": False}
    if request.intent == Intent.UNSUPPORTED:
        if current is None:
            return {
                "response": (
                    "That metric or operation is not supported by the current solver, and I don't have "
                    "a verified design to analyze yet. Load a floor plan and run Generate first; then "
                    "I can explain the design or ask the optimizer to search for a lower cable length "
                    "or fewer crossings."
                ),
                "output": None, "comparison": None, "changed": False,
            }
        if request.scope:
            return {"response": "Scoped optimization is not supported: the existing optimizer solves and "
                    "validates the whole plan, not an individual room, panel, node, or cable segment. "
                    "The current valid design was retained.",
                    "output": current, "comparison": None, "changed": False}
        if re.search(r"\b(weight|change the scoring|prioriti[sz]e|prefer)\b", request.message.lower()):
            response = ("Changing the scoring weights is not supported. The official score remains "
                        "J = L + 5X + O + 0.5B, and the optimizer's existing settings are unchanged.")
        else:
            response = ("That metric or operation is not supported by the current solver. "
                        "I can explain the current design or ask the optimizer to search for a "
                        "lower cable length or fewer crossings.")
        return {"response": response,
                "output": current, "comparison": None, "changed": False}
    if request.intent in (Intent.EXPLAIN, Intent.STATUS, Intent.COMPARE, Intent.UNDO, Intent.RESTORE) and current is None:
        return {"response": "I don't have a verified design to analyze yet. Load a floor plan and run Generate first.",
                "output": None, "comparison": None, "changed": False}

    if request.intent == Intent.STATUS:
        validation = current.get("validation", {})
        if not isinstance(validation, dict) or not isinstance(validation.get("valid"), bool):
            response = "Validation status is unavailable for the current design."
        elif validation["valid"]:
            response = "Yes. The current design passed validation and is valid."
        else:
            violations = validation.get("violations", [])
            details = "; ".join(item.get("message", "validation rule failed") for item in violations[:3])
            response = "No. The current design did not pass validation."
            if details:
                response += f" The validator reports: {details}"
        return {"response": response, "output": current, "comparison": None, "changed": False}

    if request.intent == Intent.EXPLAIN:
        if request.compare_metric is not None:
            if previous is None:
                return {"response": "I can see the current metric, but there is no earlier valid design "
                        "stored to compare it with.",
                        "output": current, "comparison": None, "changed": False}
            change = compare_designs(previous, current)[request.compare_metric.value]
            metric_label = _OBJECTIVE_LABELS[request.compare_metric]
            if change["trend"] == "unavailable":
                response = f"The {metric_label} comparison is unavailable because one design is missing that metric."
            elif change["trend"] == "improved":
                response = (f"{metric_label.capitalize()} changed from {change['before']} to "
                            f"{change['after']} in the current design, so it decreased rather than increased.")
            else:
                response = (f"{metric_label.capitalize()} changed from {change['before']} to "
                            f"{change['after']} ({change['delta']:+g}) between the previous and current "
                            "valid designs. The solver does not keep a rejected-route trace, so it cannot "
                            "prove which routing choice caused that change.")
            return {"response": response, "output": current, "comparison": change, "changed": False}
        return {"response": explain_solution(plan, current, cfg, request.message, request.scope),
                "output": current, "comparison": None, "changed": False}
    if request.intent == Intent.COMPARE:
        if previous is None:
            return {"response": "There is no earlier valid version to compare with this design.",
                    "output": current, "comparison": None, "changed": False}
        return {"response": format_comparison(compare_designs(previous, current), "previous design", "current design"),
                "output": current, "alternative_output": previous,
                "comparison": compare_designs(previous, current), "changed": False}
    if request.intent == Intent.UNDO:
        restored = state.undo()
        if restored is None:
            return {"response": "There is no earlier valid design to undo to.",
                    "output": current, "comparison": None, "changed": False}
        comparison = compare_designs(current, restored)
        return {"response": "I restored the previous valid design.\n" +
                format_comparison(comparison, "before undo", "restored"),
                "output": restored, "alternative_output": current,
                "comparison": comparison, "changed": True}
    if request.intent == Intent.RESTORE:
        restored = (state.restore_previous() if request.version_id == "previous"
                    else state.restore(request.version_id or ""))
        if restored is None:
            return {"response": f"Valid design version {request.version_id!r} was not found.",
                    "output": current, "comparison": None, "changed": False}
        comparison = compare_designs(current, restored)
        return {"response": "I restored the requested valid design.\n" +
                format_comparison(comparison, "before restore", "restored"),
                "output": restored, "alternative_output": current,
                "comparison": comparison, "changed": True}

    if request.intent not in (Intent.OPTIMIZE, Intent.CONSTRAIN):
        return {"response": "I cannot safely perform that request with the current optimizer.",
                "output": current, "comparison": None, "changed": False}
    if current is None:
        return {"response": "There is no valid current design to protect or compare. Run Generate first.",
                "output": None, "comparison": None, "changed": False}

    try:
        candidate = optimizer(plan, cfg)
        check_schema(candidate)
        report = validate(plan, candidate, cfg)
        candidate["validation"] = report
        candidate["reported_metrics"] = {**compute(candidate, cfg), "valid": report["valid"]}
    except Exception as error:
        logger.exception("Assistant optimization failed")
        return {"response": f"The optimizer could not complete this request ({type(error).__name__}). "
                "The current valid design was retained.",
                "output": current, "comparison": None, "changed": False}

    comparison = compare_designs(current, candidate)
    if not report["valid"]:
        details = "; ".join(v["message"] for v in report["violations"][:3])
        return {"response": "I couldn't apply that change because the candidate failed validation. "
                f"{details or 'The validator provided no further detail.'} I kept the current valid design.",
                "output": current, "comparison": comparison, "changed": False}

    if request.intent == Intent.CONSTRAIN:
        target = request.target_value
        if candidate["reported_metrics"]["hub_count"] != target:
            return {"response": f"I tried for {target} panels, but the optimizer returned "
                    f"{candidate['reported_metrics']['hub_count']}. "
                    "It searches for the fewest feasible panels, but cannot force an exact count; this "
                    "doesn't prove the requested count is impossible. I kept the current design.",
                    "output": current, "alternative_output": candidate,
                    "comparison": comparison, "changed": False}
        state.accept(candidate, request.message)
        return {"response": f"I asked the optimizer to try {target} panel{'s' if target != 1 else ''}. "
                f"It found a valid {target}-panel design, and your request is met. "
                "The design passed validation and is now current.",
                "output": candidate, "alternative_output": current,
                "comparison": comparison, "changed": True}

    objective = request.objective or Objective.SCORE
    change = comparison[objective.value]
    if change["trend"] != "improved":
        request_text = ""
        if request.target_percent is not None:
            request_text = f" Requested reduction: {request.target_percent:g}%."
        detail = _objective_result(comparison[objective.value], objective)
        return {"response": f"I tried to improve {_OBJECTIVE_LABELS[objective]}.{request_text} "
                f"{detail} The candidate passed validation, but I kept the current design.",
                "output": current, "alternative_output": candidate,
                "comparison": comparison, "changed": False}

    state.accept(candidate, request.message)
    outcome = f"The optimizer found a valid alternative. {_objective_result(change, objective)}"
    if request.target_percent is not None:
        achieved = -change["percent_change"] if change["percent_change"] is not None else None
        target_result = ("target met" if achieved is not None and achieved >= request.target_percent
                         else "target not met")
        outcome += f" Your requested {request.target_percent:g}% reduction was {target_result}."
    return {"response": outcome + " The new design passed validation.",
            "output": candidate, "alternative_output": current,
            "comparison": comparison, "changed": True}


def explain_solution(plan, output: dict, cfg: Config, question: str = "",
                     scope: str | None = None) -> str:
    metrics = output.get("reported_metrics", {})
    solver = output.get("solver", {})
    question = question.lower()
    hubs = output.get("hubs", [])
    if re.search(r"\b(score|cost|objective|J)\b", question):
        length = metrics.get("total_length_m")
        crossings = metrics.get("crossings")
        overlap = metrics.get("overlap_m")
        bends = metrics.get("bends")
        score = metrics.get("cost_J")
        if all(isinstance(value, (int, float)) for value in (length, crossings, overlap, bends, score)):
            contributions = (
                cfg.w_length * length, cfg.w_cross * crossings,
                cfg.w_overlap * overlap, cfg.w_bend * bends,
            )
            return (
                f"The current design has {length:.3f} m of cable, {crossings} crossings, "
                f"{overlap:.3f} m of overlap, and {bends} bends. Its score is {score:.3f}: "
                f"cable contributes {contributions[0]:.3f}, crossings {contributions[1]:.3f}, "
                f"overlap {contributions[2]:.3f}, and bends {contributions[3]:.3f} "
                f"under J = {cfg.w_length:g}L + {cfg.w_cross:g}X + "
                f"{cfg.w_overlap:g}O + {cfg.w_bend:g}B."
            )
        return "Some score metrics are unavailable in the current design, so I can't give a complete breakdown."

    if re.search(r"\b(panel|hub|room|assigned|device|node)\b", question):
        count = metrics.get("hub_count", len(hubs))
        lower = solver.get("hub_lower_bound")
        target = scope.upper() if scope else None
        matched = [
            hub for hub in hubs
            if target is None
            or target in (hub["hub_id"], hub.get("room_id"))
            or target in hub.get("assigned_rooms", [])
            or target in hub.get("assigned_nodes", [])
        ]
        if target and not matched:
            return f"I couldn't find {target} among the current panels, rooms, or devices."
        selected = matched if target else hubs
        per_panel = [
            f"{hub['hub_id']} serves {hub.get('assigned_area_m2', 0):.2f} m² "
            f"and {len(hub.get('assigned_nodes', []))} devices"
            for hub in selected
        ]
        response = f"The current design uses {count} panel{'s' if count != 1 else ''}."
        if lower is not None and count == lower:
            response += f" That matches the calculated lower bound of {lower}."
        elif lower is not None:
            response += f" The calculated lower bound is {lower}; the solver selected {count}."
        if per_panel:
            response += " " + "; ".join(per_panel) + "."
        response += (f" Each panel is limited to {cfg.max_area_m2:g} m² and "
                     f"{cfg.max_nodes} devices. The assignments shown are confirmed, "
                     "but the solver doesn't record why every alternative grouping was rejected.")
        return response

    if re.search(r"\b(route|path|cable|long)\b", question):
        route_ends = tuple(scope.split("->")) if scope and "->" in scope else None
        segments = [
            segment
            for hub in hubs
            for segment in hub.get("route_segments", [])
            if route_ends is None or (segment["from"], segment["to"]) == route_ends
        ]
        if not segments:
            return "I couldn't find a matching route in the current design."
        lengths = ", ".join(
            f"{segment['from']} to {segment['to']}: {segment['length_m']:.3f} m"
            for segment in segments[:5]
        )
        response = f"The selected route lengths are {lengths}."
        if re.search(r"\b(why|long|alternative)\b", question):
            response += " The solver doesn't retain rejected-route comparisons, so I can't prove why an alternative path lost."
        if re.search(r"\b(show|trace|coordinates|vertices)\b", question):
            vertices = segments[0]["polyline"]
            response += " The selected path goes through " + " → ".join(
                f"({x:g}, {y:g})" for x, y in vertices
            ) + "."
        return response

    count = metrics.get("hub_count", len(hubs))
    length = metrics.get("total_length_m")
    score = metrics.get("cost_J")
    summary = f"The current valid design uses {count} panel{'s' if count != 1 else ''}"
    if length is not None and score is not None:
        summary += f", with {length:.3f} m of cable and a score of {score:.3f}"
    return summary + ". What would you like to know about it?"


def _objective_result(change: dict, objective: Objective) -> str:
    before, after = change["before"], change["after"]
    label = _OBJECTIVE_LABELS[objective]
    if change["trend"] == "unavailable":
        return f"The {label} comparison is unavailable."
    if objective == Objective.LENGTH:
        percent = change["percent_change"]
        if percent is None:
            return f"Cable length changed from {before} m to {after} m; a percentage isn't defined from a zero starting length."
        direction = "reduction" if percent < 0 else "increase"
        if change["trend"] == "unchanged":
            return f"Cable length stayed at {after} m."
        return f"Cable length changed from {before} m to {after} m, a {abs(percent):.2f}% {direction}."
    if change["trend"] == "unchanged":
        return f"{label.capitalize()} stayed at {after}."
    direction = "decreased" if change["trend"] == "improved" else "increased"
    return f"{label.capitalize()} {direction} from {before} to {after}."


def format_comparison(comparison: dict, before_name: str, after_name: str) -> str:
    labels = {
        "hub_count": "panels",
        "total_length_m": "length (m)",
        "crossings": "crossings",
        "overlap_m": "overlap (m)",
        "bends": "bends",
        "cost_J": "J",
    }
    relevant = ("hub_count", "total_length_m", "crossings", "overlap_m", "bends", "cost_J")
    rows = []
    for name in relevant:
        change = comparison.get(name, {"trend": "unavailable"})
        if change["trend"] == "unavailable":
            rows.append(f"{labels[name]}: unavailable")
            continue
        if name == "valid":
            before = "valid" if change["before"] else "invalid"
            after = "valid" if change["after"] else "invalid"
            rows.append(f"validation: {before} → {after} ({change['trend']})")
            continue
        delta = change["delta"]
        percentage = change["percent_change"]
        pct = f", {percentage:+.2f}%" if percentage is not None else ", percentage unavailable (zero baseline)"
        rows.append(f"{labels[name]}: {change['before']} → {change['after']} "
                    f"({delta:+g}{pct}; {change['trend']})")
    return f"{before_name} → {after_name}:\n" + "\n".join(rows)
