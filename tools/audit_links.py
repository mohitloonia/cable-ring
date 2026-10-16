"""Audit our room links (doors / open passages) against the dataset's own graph.

    python tools/audit_links.py --split test            # plans of the test split
    python tools/audit_links.py --split test --only-extra-hubs outputs/batch_test_full/summary.csv

For every plan, each dataset `via_door` edge between two of our rooms that our room graph
does NOT have is listed with a likely reason. A missed link can split a zone and cost a
whole extra hub. Writes outputs/link_audit.csv.
"""
import argparse
import collections
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shapely.geometry import Point  # noqa: E402

from cablering.config import OUTPUT_DIR, PROJECT_ROOT, Config  # noqa: E402
from cablering.input_loader import load_dataset, plan_from_raw, plan_id_for_index  # noqa: E402
from cablering.room_graph import build_room_graph  # noqa: E402


def match_rooms(raw, plan) -> dict:
    """dataset graph node -> our room id, by where the node's polygon centre falls."""
    s = plan.scale
    out = {}
    for name, data in raw["graph"].nodes(data=True):
        g = data.get("geometry")
        if g is None or g.is_empty:
            continue
        p = g.representative_point()
        q = Point(p.x / s, p.y / s)
        hit = [r.id for r in plan.rooms if r.poly.buffer(0.05).covers(q)]
        if hit:
            out[name] = hit[0]
    return out


def reason(plan, a, b, cfg) -> str:
    ra, rb = plan.room(a).poly, plan.room(b).poly
    near = [d for d in plan.doors
            if d.buffer(plan.wall_thickness * 2 + 0.3).intersects(ra)
            and d.buffer(plan.wall_thickness * 2 + 0.3).intersects(rb)]
    if not near:
        return "no door polygon between them"
    for d in near:
        hits = [r for r in plan.rooms if d.buffer(plan.wall_thickness * 0.75 + 0.05).intersects(r.poly)]
        if len(hits) >= 3:
            return "door touches 3+ rooms (linked to another pair)"
        if len(hits) < 2:
            return "door too far from one room (reach too small)"
    return "door near both but not linked"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["train", "val", "test"], default="test")
    ap.add_argument("--only-extra-hubs", help="summary.csv: audit only plans with hubs > lower bound")
    ap.add_argument("--door-links", default="legacy", help="room graph to audit: legacy | both_sides")
    args = ap.parse_args()

    data = load_dataset()
    ids = set(json.loads((PROJECT_ROOT / "data" / "split.json").read_text())[args.split])
    idx = [i for i, p in enumerate(data) if p["id"] in ids]
    if args.only_extra_hubs:
        rows = list(csv.DictReader(open(args.only_extra_hubs)))
        extra = {r["plan_id"] for r in rows if r.get("valid") == "True" and r["hubs"] != r["hub_lower_bound"]}
        idx = [i for i in idx if plan_id_for_index(i) in extra]

    cfg = Config(door_links=args.door_links)
    out_rows, reasons, plans_hit, plans_elig_hit = [], collections.Counter(), 0, 0
    for i in idx:
        raw = data[i]
        try:
            plan = plan_from_raw(raw, plan_id_for_index(i))
        except Exception:
            continue
        G = build_room_graph(plan, cfg)
        names = match_rooms(raw, plan)
        missing = []
        for u, v, d in raw["graph"].edges(data=True):
            if d.get("type") != "via_door" or u not in names or v not in names:
                continue
            a, b = names[u], names[v]
            if a != b and not G.has_edge(a, b):
                missing.append((a, b))
        missing = sorted(set(tuple(sorted(p)) for p in missing))
        if missing:
            plans_hit += 1
            elig = {r.id for r in plan.eligible_rooms}
            plans_elig_hit += any(a in elig and b in elig for a, b in missing)
        for a, b in missing:
            why = reason(plan, a, b, cfg)
            reasons[why] += 1
            out_rows.append({"plan_id": plan.plan_id, "room_a": a, "type_a": plan.room(a).type,
                             "room_b": b, "type_b": plan.room(b).type, "reason": why})

    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / "link_audit.csv"
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["plan_id", "room_a", "type_a", "room_b", "type_b", "reason"])
        w.writeheader()
        w.writerows(out_rows)
    print(f"plans audited: {len(idx)}")
    print(f"plans with a dataset door link we miss: {plans_hit} "
          f"({plans_elig_hit} where both rooms need a node)")
    print("missed links by reason:", dict(reasons))
    print("details:", path)


if __name__ == "__main__":
    main()
