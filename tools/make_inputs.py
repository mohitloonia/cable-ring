"""Turn dataset plans into per-plan input JSON files (our provisional input format).

    python tools/make_inputs.py 0 1 2 42        # specific dataset indices
    python tools/make_inputs.py --range 0 20    # indices 0..19

Files go to inputs/PLAN_xxxxx.json. Use them with the app (upload) or
`python batch.py --inputs inputs`.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cablering.config import PROJECT_ROOT  # noqa: E402
from cablering.input_loader import load_dataset, plan_id_for_index, raw_to_input_dict  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("indices", nargs="*", type=int)
    ap.add_argument("--range", nargs=2, type=int, metavar=("START", "STOP"))
    ap.add_argument("--out", default=str(PROJECT_ROOT / "inputs"))
    args = ap.parse_args()
    idx = list(args.indices) + (list(range(*args.range)) if args.range else [])
    if not idx:
        ap.error("give indices or --range")
    data = load_dataset()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for i in idx:
        raw = data[i]
        d = raw_to_input_dict(raw, plan_id_for_index(i), {"dataset": "ResPlan", "index": i, "resplan_id": int(raw["id"])})
        p = out / f"{d['plan_id']}.json"
        p.write_text(json.dumps(d))
        print("wrote", p)


if __name__ == "__main__":
    main()
