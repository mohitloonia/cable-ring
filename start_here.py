"""First-run check: libraries, dataset, and one solved plan.

    python start_here.py          # plan 0
    python start_here.py 42       # dataset plan 42
    python start_here.py inputs/PLAN_00042.json
"""
import sys
import time

REQUIRED = ["shapely", "numpy", "scipy", "networkx", "matplotlib", "pydantic"]


def main() -> None:
    missing = []
    for mod in REQUIRED:
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        sys.exit(f"missing packages: {missing}. Run: python -m pip install -r requirements.txt")

    from cablering.config import DATA_PATH, OUTPUT_DIR, Config
    from cablering.export_json import write_output
    from cablering.input_loader import load_plan
    from cablering.optimizer import solve
    from cablering.visualization import save

    spec = sys.argv[1] if len(sys.argv) > 1 else "0"
    if spec.isdigit() and not DATA_PATH.exists():
        sys.exit(f"dataset not found at {DATA_PATH}. Unzip ResPlan.pkl from HACKHIVE.zip into data/")

    t = time.time()
    plan = load_plan(spec)
    print(f"loaded {plan.plan_id} in {time.time() - t:.1f}s: {len(plan.rooms)} rooms, "
          f"{len(plan.eligible_rooms)} eligible, scale {plan.scale:.2f} units/m")

    cfg = Config()
    out = solve(plan, cfg)
    base = solve(plan, cfg, baseline=True)
    m, b = out["reported_metrics"], base["reported_metrics"]
    print(f"optimised: valid={m['valid']} hubs={m['hub_count']} (lower bound "
          f"{out['solver']['hub_lower_bound']}) J={m['cost_J']}  [{out['solver']['time_s']}s]")
    print(f"baseline:  valid={b['valid']} hubs={b['hub_count']} J={b['cost_J']}")
    for v in out["validation"]["violations"]:
        print("  VIOLATION:", v["message"])

    OUTPUT_DIR.mkdir(exist_ok=True)
    js = write_output(out, OUTPUT_DIR / f"{plan.plan_id}.json")
    png = save(plan, out, OUTPUT_DIR / f"{plan.plan_id}.png")
    print("wrote", js, "and", png)
    try:
        from cablering.view3d import save_html
        html = save_html(plan, out, OUTPUT_DIR / f"{plan.plan_id}_3d.html")
        print("wrote", html, "(3D view: open it in any browser)")
    except ImportError:
        print("plotly not installed: skipping the 3D view (pip install plotly)")


if __name__ == "__main__":
    main()
