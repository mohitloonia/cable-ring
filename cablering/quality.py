"""Quality engine: the original engine first, then validated improvements only.

Workflow for one plan
  1. original   run the frozen original engine; its validated result is the incumbent
  2. search     wider search with the quality settings (more zone splits / cycles /
                reroute passes, bend-aware ordering, extra door lanes, 2 cm one-room hub)
  3. relocate   slide nodes onto the cable they sit beside (re-solved with the zones fixed)
  4. strict     among valid designs within `strict_window_J` of the best, prefer one that
                also passes the stricter safety check
A candidate replaces the incumbent only if it is valid and better by
(validity, hub count, J, area gap). So the result is never worse than the original.
"""
from __future__ import annotations

import time

from .config import Config
from .optimizer import solve_original


def design_key(out: dict) -> tuple:
    m = out.get("reported_metrics", {})
    valid = bool(m.get("valid"))
    return (not valid, m.get("hub_count", 10 ** 6) if valid else 10 ** 6,
            round(float(m.get("cost_J", 0.0)), 6), round(float(m.get("area_gap_m2", 0.0)), 6))


def _summary(out: dict) -> dict:
    m = out.get("reported_metrics", {})
    return {"valid": m.get("valid"), "hub_count": m.get("hub_count"), "cost_J": m.get("cost_J")}


def solve_quality(plan, cfg: Config) -> dict:
    t0 = time.time()
    deadline = t0 + cfg.time_budget_s
    steps = []

    # 1. the original engine is the incumbent and the guaranteed fallback
    best = solve_original(plan, cfg.as_original())
    best_step = "original"
    original = _summary(best)
    steps.append({"step": "original", **original})
    candidates = [("original", best)]

    def consider(name: str, out: dict) -> None:
        nonlocal best, best_step
        steps.append({"step": name, **_summary(out)})
        if out.get("reported_metrics", {}).get("valid"):
            candidates.append((name, out))
        if design_key(out) < design_key(best):
            best, best_step = out, name

    # 2. wider, bend-aware search with the quality settings
    if time.time() < deadline:
        consider("quality_search", solve_original(plan, cfg))

    # 3. node sliding (re-solve with the zones of the incumbent fixed)
    if cfg.relocate_trials > 0 and best.get("hubs") and time.time() < deadline:
        from .relocate import relocation_candidates
        for name, out in relocation_candidates(plan, cfg, best, deadline):
            consider(name, out)

    # 4. strict-mode preference within a small J window
    if cfg.strict_window_J > 0 and best.get("hubs"):
        from .validation import validate_strict
        best, best_step = _prefer_strict(plan, cfg, best, best_step, candidates, validate_strict)

    best["solver"] = {
        **best.get("solver", {}),
        "engine": "quality",
        "improved_by": best_step,
        "original": original,
        "steps": steps,
        "time_s": round(time.time() - t0, 2),
    }
    return best


def _prefer_strict(plan, cfg, best, best_step, candidates, validate_strict):
    hubs = best["reported_metrics"]["hub_count"]
    J0 = best["reported_metrics"]["cost_J"]
    pool = [(n, o) for n, o in candidates
            if o["reported_metrics"]["hub_count"] == hubs
            and o["reported_metrics"]["cost_J"] <= J0 + cfg.strict_window_J]
    pool.sort(key=lambda t: design_key(t[1]))
    for name, out in pool:
        ok = validate_strict(plan, out, cfg)["valid"]
        out.setdefault("solver", {})["strict_valid"] = ok
        if ok:
            return out, name if out is not best else best_step
    best.setdefault("solver", {})["strict_valid"] = False
    return best, best_step
