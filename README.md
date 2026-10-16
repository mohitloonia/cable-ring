# Cable Ring Optimizer: Honeywell Cable Ring Challenge

Given a ResPlan floor plan, decide **how many hubs**, **where they go**, **which rooms
each hub serves**, **the order nodes are connected**, and **the physical cable ring**,
so that every hard rule holds, and then minimise

> **fewest hubs first**, then **J = L + 5X + O + 0.5B**
> (L cable length m, X crossings, O overlap m, B bends)

## Quick start (Windows)

```bat
python -m venv .venv
.venv\Scripts\activate.bat
python -m pip install -r requirements.txt
REM put ResPlan.pkl (from HACKHIVE.zip) into data\
python start_here.py               REM solves plan 0, writes outputs\PLAN_00000.json/.png
REM Terminal 1, from the project root:
python -m uvicorn app:app --reload
REM Terminal 2, from the project root:
cd frontend
npm install
npm run dev                         REM React app at http://localhost:5173
REM Run tests in another terminal from the project root:
python -m pytest -q                REM tests
python batch.py --split test --count 300 --baseline   REM benchmark
```

## Project layout

```
cable-ring/
├── app.py                 FastAPI endpoints for plans, solving and floor-plan previews
├── frontend/              React + Vite interface
├── batch.py               parallel benchmark over many plans → outputs/batch/summary.csv
├── start_here.py          first-run check: libraries, dataset, one solved plan
├── requirements.txt
├── cablering/             the engine
│   ├── config.py          every rule, weight and tunable (CONFIRM items marked)
│   ├── model.py           Plan / Room data classes (metres)
│   ├── input_loader.py    pickle or JSON → Plan; official scale sqrt(inner.area / area)
│   ├── geometry.py        cleaning, door passages, free space, clearance
│   ├── room_graph.py      legal links: doors + open passages only
│   ├── assignment.py      zones: exact enumeration of connected, capacity-feasible splits
│   ├── candidates.py      node points per room (≥ 0.5 m from walls, lined up with doors)
│   ├── routing.py         axis-aligned routing graph + bend-aware Dijkstra + penalties
│   ├── sequencing.py      exact ring order + node choice (Held-Karp, generalised TSP)
│   ├── optimizer.py       the engine: zones → cycles → routes → improve → hubs → check
│   ├── quality.py         quality engine: original first, then validated improvements only
│   ├── relocate.py        node sliding (re-solve with zones fixed)
│   ├── metrics.py         L, X, O, B, J from polylines only (1 mm tolerance)
│   ├── validation.py      every hard rule, independent of the solver
│   ├── export_json.py     slide-8 output schema (Pydantic)
│   ├── visualization.py   overlay: zones, hubs, nodes, order, rings, violations in red
│   ├── view3d.py          interactive 3D model (Plotly) + standalone HTML export
│   └── whatif.py          what-if scenarios: close doors, change rules/weights, move a hub
├── tools/make_inputs.py   dataset plans → per-plan input JSON files
├── tests/                 metric examples from slide 7, validator, end-to-end
├── data/                  ResPlan.pkl, split.json (not in git)
├── inputs/                per-plan input JSON
└── outputs/               results (not in git)
```

## 3D view

In the app, pick **3D view** after Generate: walls raised to room height (doors are
openings with a lintel, windows have glass), floors coloured by hub zone, every ring at
ceiling height with visiting-order numbers, hubs as wall panels with a riser to the
cable. Sliders lower the walls (doll's-house view) or make them see-through; **▶ Trace the
cable** animates a pulse round every ring. **Download this 3D view (HTML)** saves a single
file that opens offline in any browser, for the presentation.

From the command line: `python start_here.py 42` also writes `outputs/PLAN_00042_3d.html`,
and `python batch.py --count 10 --html3d` writes one per plan. Heights are presentation
only; scoring uses the 2D polylines. Code: `cablering/view3d.py`.

## What-if simulator

After **Generate**, open the **What-if simulator** tab. The score always stays official
(J = L + 5X + O + 0.5B); it cannot be changed here.

**Drag devices** (default): grab any node (circle) or hub (diamond) on the floor plan with
the mouse and drop it somewhere else. The other devices stay put, the visiting order and
every cable route are re-optimised through the doors, and the panel shows the cost against
the optimizer's design, e.g. "+8.8 J worse than the optimizer". Undo and Reset to optimal
are one click; a device dropped in a wall or outside is refused with the reason. With nothing
moved the cost is exactly the optimum, so every drag shows how much the optimizer saves.

**Building & rules**:

| Control | What happens |
|---|---|
| **Close a door** (D1, D2, ... tagged on the official plan) | The door becomes wall: no cable through it, and the two rooms lose that zone link |
| **Rules** | Max area / nodes per hub, hub-node clearance, zones through bathrooms |

Every difference from the official benchmark is listed in a yellow banner, so a scenario is
never mistaken for a submitted result, with an official-vs-scenario table (worse in red,
better in green) and both drawings side by side. The 3D view shows the latest drag or scenario.

API: `GET /api/plans/{plan_key}/geometry` (plan shapes for the editor),
`POST /api/drag` (re-route with devices where they were dropped),
`GET /api/plans/{plan_key}/whatif` (doors, rooms, hubs, labelled door map) and
`POST /api/whatif` (run a scenario). Code: `cablering/whatif.py`,
`optimizer.solve_with_fixed_points`, `frontend/src/DragEditor.jsx`, `frontend/src/WhatIf.jsx`.

If port 8000 is busy, run the backend elsewhere and point the frontend at it:
`set API_PORT=8001` before `npm run dev`.

## Quality engine (default)

`solve()` runs the **original engine first** and keeps its validated result as the
guaranteed fallback, then tries improvements and keeps one only if it is valid and better
by (validity, hubs, J, area gap). So the result is **never worse than the original**.
`Config(engine="original")` (or `cfg.as_original()`) gives exactly the first engine.

| Step | What it does | Code |
|---|---|---|
| Wider search | 6 zone splits × 6 alternative cycles per zone, 4 reroute passes | `config.py` |
| Bend-aware ordering | routing graph with 4 travel directions (a reversal costs a bend); Held-Karp over (rooms, point, arrival direction) counts the bends at nodes | `routing.DirRoutingGraph`, `sequencing.best_cycles_dir` |
| Junction-aware rerouting | each leg is rerouted knowing how the neighbouring legs arrive and leave | `optimizer._route_leg` |
| Door lanes | extra crossing lanes every 0.10 m through doors (≥ wall margin + 0.05 m from the frame), ±0.15 / 0.30 m through openings | `optimizer.guide_lines` |
| Both-sides doors | also links the rooms found straight across each door (added to the old links) | `room_graph._door_links_both_sides` |
| One-room zones | hub 2 cm from the node (was 10 cm) | `config.hub_offset_m` |
| Node sliding | moves nodes onto cable already passing through their room / onto adjacent corners, re-solved with zones fixed | `relocate.py` |
| Strict check | among designs within 0.10 J of the best, prefer one that passes a stricter check (5 mm tolerance, 3 cm wall margin, narrower doors) | `validation.validate_strict` |

Each output's `solver` section shows `original` (the fallback's metrics), `steps` (every
candidate tried) and `improved_by` (which step won).

**Results, full test split (1,632 unseen plans):**

| | Greedy baseline | Original | Quality |
|---|---|---|---|
| Valid | 1,582 | 1,588 | 1,588 (the other 44 are unsolvable: 42 rooms > 100 m², 2 with no door) |
| Better / same / **worse** than original | | | 1,302 / 330 / **0** |
| Mean J | 60.61 | 20.13 | **19.55 (−2.86 % vs original, −67.7 % vs greedy)** |
| Median J | | 20.22 | 19.79 |
| Total hubs | | 2,840 | 2,838 |
| Strict-valid | | | 1,588 / 1,588 |
| Cable inside a wall / window (independent audit) | | | **0** |
| Time per plan | ~0.1 s | ~0.5 s | median 3.1 s, p95 11.1 s, max 30.4 s |

Wins by step: wider bend-aware search 961 plans, node sliding 341 plans.
(Dev set, 200 `val` plans, step by step: search + 2 cm hub −1.50 %, + bend-aware −2.16 %,
+ door lanes / both-sides doors −2.55 %, + node sliding −3.15 %.)

Benchmark any change against the original:
`python batch.py --split val --count 200 --out outputs/dev` prints better / same / worse,
mean and median J and improvements by step; `--set key=value` switches a step off or on
(e.g. `--set relocate_trials=0 bend_aware=false`).
Door-link audit against the dataset's own graph: `python tools/audit_links.py --split test`.

## How the engine works

1. **Load + scale.** `scale = sqrt(inner.area / area)` (slide 4), coordinates ÷ scale.
   Polygons are repaired; eligible slivers < 0.5 m² (tracing artefacts) are dropped and reported.
2. **Room graph.** Rooms linked only by doors (door polygon touching two rooms) or open
   passages (shared boundary with no wall, ≥ 0.6 m). Windows, wall-only contact and the
   entrance never count.
3. **Free space.** Each room shrunk 5 cm, plus door passages and open-passage strips,
   inside the inner boundary. Cable physically cannot cross a wall, window or room
   boundary anywhere else.
4. **Zones (fewest hubs).** Lower bound `max(ceil(area/100), ceil(nodes/max_nodes))`.
   Every split into k connected groups ≤ 100 m² is enumerated exactly; k increases
   only if no split works.
5. **Candidates.** Per room: the deepest point plus points lined up with each door and
   corners between doors, all ≥ 0.5 m from walls (in `optimize` node mode).
6. **Routing graph.** Horizontal/vertical lines through free-space corners, door centre
   lines (+ parallel lanes) and every candidate. Dijkstra on (point, heading) so each turn
   costs 0.5, exactly like the score.
7. **Cycles.** Per zone, the exact best "one point per room + order" cycle (Held-Karp).
8. **Realise + improve.** Legs routed with penalties for touching (crossing, 5) or sharing
   (overlap, 1/m) existing cable; rip-up-and-reroute passes; alternative cycles and the
   next-best zone splits are tried; a change is kept only if the exact J drops.
9. **Hubs for free.** Each hub is placed **on its own cable**, on a straight piece inside
   an eligible room of its zone, ≥ 0.5 m from walls. Splitting a straight run adds no
   length and no bend. One-room zone: hub 10 cm from the node.
10. **Check.** Every candidate design goes through the validator; the best valid one
    wins (fewest hubs, then J, then smaller area gap). If none is valid, the simple
    baseline is used if it is valid.

## Results (full test split: 1,632 unseen plans)

`python batch.py --split test --count 2000 --baseline`

| | |
|---|---|
| Valid | **1,588 / 1,588 solvable plans (100 %)**. The other 44 cannot be solved by anyone: 42 have a single room > 100 m², 2 have a room with no door (organisers exclude such plans) |
| Hubs at the area lower bound | 1,220 / 1,588. The rest are forced up by zone connectivity (e.g. every bedroom opens only onto the living room) |
| Mean J | 20.1 |
| Optimised vs simple baseline | J **58.7 % lower** on average |
| Crossings | 16 in total over 1,588 plans; mean overlap 0.08 m per plan |
| Time per plan | 0.5 s mean, < 5 s max |

Data defects handled automatically: eligible slivers < 0.5 m² are dropped (reported in
`solver.dropped_rooms`), duplicated/overlapping room polygons get distinct node points,
rooms too small for 0.5 m clearance never get their own hub.

## Interpretations to confirm with the organisers

All are switches in `cablering/config.py`:

| Setting | Current | Question |
|---|---|---|
| `max_nodes` | 20 | Brief says 20 per panel, slides say 5 per hub |
| `node_mode` | `optimize` | Are node points fixed inputs (`fixed`) or ours to choose? |
| `zone_via_other_rooms` | False | May a zone connect through a bathroom / non-zone room? |
| `wall_margin_m` | 0.05 | How close may cable run to a wall? |
| `clearance_from_doors` | True | Does the 0.5 m clearance also apply to doorways? (True is the safe choice) |
| coordinates | units ÷ scale | Exactly this, no flip / shift? ("origin lower-left, y up") |
| input JSON | our WKT format | Need a sample organiser input file; only `plan_from_input_dict` changes |

## Output JSON

Slide-8 schema (`plan_id`, `units`, `hubs[]` with `hub_id`, `room_id`, `location`,
`assigned_rooms`, `assigned_nodes`, `closed_sequence`, `route_segments[]`
{`from`, `to`, `length_m`, `polyline`}, `ring_length_m`) plus:
`nodes[]` (we choose node points), `assigned_area_m2`, `reported_metrics`
(hub_count, length, crossings, overlap, bends, J, valid), `validation`
(violations + warnings) and `solver` (lower bound, time, notes).
