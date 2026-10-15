"""Solve many plans in parallel and summarise validity, hubs and cost.

    python batch.py --start 0 --count 100                 # dataset plans 0..99
    python batch.py --split test --count 200 --baseline    # test split, also run the baseline
    python batch.py --inputs inputs                        # every *.json in a folder

Writes outputs/batch/<plan_id>.json (+ .png with --png) and outputs/batch/summary.csv.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from cablering.config import OUTPUT_DIR, PROJECT_ROOT, Config
from cablering.export_json import write_output
from cablering.input_loader import load_dataset, plan_from_input_dict, plan_from_raw, plan_id_for_index


def run_one(job: dict) -> dict:
    cfg = Config.from_dict(job["cfg"])
    if "raw" in job:
        i = job["index"]
        plan = plan_from_raw(job["raw"], plan_id_for_index(i), source={"dataset": "ResPlan", "index": i})
    else:
        plan = plan_from_input_dict(json.loads(Path(job["path"]).read_text()))
    out_dir = Path(job["out_dir"])
    row = {"plan_id": plan.plan_id, "eligible_rooms": len(plan.eligible_rooms),
           "eligible_area_m2": round(sum(r.area for r in plan.eligible_rooms), 2)}
    try:
        from cablering.optimizer import solve
        out = solve(plan, cfg)
        write_output(out, out_dir / f"{plan.plan_id}.json")
        if job["png"]:
            from cablering.visualization import save
            save(plan, out, out_dir / f"{plan.plan_id}.png")
        m, s = out["reported_metrics"], out["solver"]
        row.update(valid=m["valid"], hubs=m["hub_count"], hub_lower_bound=s["hub_lower_bound"],
                   J=m["cost_J"], L=m["total_length_m"], X=m["crossings"], O=m["overlap_m"], B=m["bends"],
                   time_s=s["time_s"], note=s.get("note", ""),
                   first_violation=(out["validation"]["violations"] or [{"message": ""}])[0]["message"])
        if job["baseline"]:
            b = solve(plan, cfg, baseline=True)
            row.update(baseline_valid=b["reported_metrics"]["valid"], baseline_J=b["reported_metrics"]["cost_J"])
    except Exception as e:  # never let one plan kill the batch
        row.update(valid=False, error=repr(e))
    return row


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--count", type=int, default=50)
    ap.add_argument("--split", choices=["train", "val", "test"], help="take plans from split.json")
    ap.add_argument("--inputs", help="folder of input JSON files instead of the dataset")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    ap.add_argument("--baseline", action="store_true", help="also solve with the simple baseline")
    ap.add_argument("--png", action="store_true", help="save an overlay PNG per plan")
    ap.add_argument("--out", default=str(OUTPUT_DIR / "batch"))
    args = ap.parse_args()

    cfg = Config().to_dict()
    common = {"cfg": cfg, "out_dir": args.out, "png": args.png, "baseline": args.baseline}
    if args.inputs:
        jobs = [{**common, "path": str(p)} for p in sorted(Path(args.inputs).glob("*.json"))]
    else:
        data = load_dataset()
        if args.split:
            ids = set(json.loads((PROJECT_ROOT / "data" / "split.json").read_text())[args.split])
            idx = [i for i, p in enumerate(data) if p["id"] in ids][args.start: args.start + args.count]
        else:
            idx = list(range(args.start, min(len(data), args.start + args.count)))
        jobs = [{**common, "raw": data[i], "index": i} for i in idx]

    print(f"solving {len(jobs)} plans with {args.workers} workers ...")
    t0 = time.time()
    rows = []
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(run_one, j) for j in jobs]
        for k, f in enumerate(as_completed(futs), 1):
            rows.append(f.result())
            if k % 25 == 0 or k == len(jobs):
                print(f"  {k}/{len(jobs)} done ({time.time() - t0:.0f}s)")
    rows.sort(key=lambda r: r["plan_id"])

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    keys = sorted({k for r in rows for k in r}, key=lambda k: list(rows[0]).index(k) if k in rows[0] else 99)
    with open(out_dir / "summary.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)

    ok = [r for r in rows if r.get("valid")]
    print(f"\nvalid: {len(ok)}/{len(rows)}  ({100 * len(ok) / max(1, len(rows)):.1f}%)")
    if ok:
        at_lb = sum(1 for r in ok if r["hubs"] == r["hub_lower_bound"])
        print(f"hubs at lower bound: {at_lb}/{len(ok)}")
        print(f"mean J: {statistics.mean(r['J'] for r in ok):.2f}   "
              f"mean time: {statistics.mean(r['time_s'] for r in ok):.2f}s   "
              f"max time: {max(r['time_s'] for r in ok):.2f}s")
        both = [r for r in ok if r.get("baseline_valid")]
        if both:
            gain = statistics.mean(1 - r["J"] / r["baseline_J"] for r in both if r["baseline_J"] > 0)
            print(f"optimised vs baseline: J {gain:.1%} lower on average ({len(both)} plans)")
    bad = [r for r in rows if not r.get("valid")]
    for r in bad[:10]:
        print("  INVALID", r["plan_id"], r.get("error") or r.get("first_violation"))
    print("summary:", out_dir / "summary.csv")


if __name__ == "__main__":
    main()
