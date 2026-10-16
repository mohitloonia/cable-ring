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
├── app.py                 FastAPI endpoints for plans, solving, previews and design assistant
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
│   ├── assistant.py       deterministic request parsing, explanations, comparisons and valid design history
│   ├── metrics.py         L, X, O, B, J from polylines only (1 mm tolerance)
│   ├── validation.py      every hard rule, independent of the solver
│   ├── export_json.py     slide-8 output schema (Pydantic)
│   └── visualization.py   overlay: zones, hubs, nodes, order, rings, violations in red
├── tools/make_inputs.py   dataset plans → per-plan input JSON files
├── tests/                 metric examples from slide 7, validator, end-to-end
├── data/                  ResPlan.pkl, split.json (not in git)
├── inputs/                per-plan input JSON
└── outputs/               results (not in git)
```

## Natural-language design assistant

After loading a plan and pressing **Generate**, use the assistant in the main
interface to ask about the actual solution, request a new optimizer run, compare
designs, undo the most recent valid version, or restore a saved version. Clickable
starter prompts are available when a plan is loaded; after each reply, the assistant
offers a few context-sensitive next steps. Examples:

- "Why did you use two panels?"
- "Reduce cable length by 10%."
- "Find a design with fewer crossings."
- "Compare this solution with the previous one."
- "Undo the last change."

The assistant is a conversational React UI backed by FastAPI (this project does
not use Streamlit). When configured, a pretrained instruction-following model
provides open-ended conversation and returns a strict, allowlisted action
decision. The backend alone dispatches supported operations. Current and previous
design facts are supplied separately from untrusted transcript text. Model
responses cannot create routes, metrics, or validation results. Design changes
still go through the existing optimizer, validator, and metric calculator, and
only a fully valid candidate can become current.

To enable AI conversation, copy `.env.example` to `.env` in the project root and
set `OPENAI_API_KEY` to a key you provide. The key is read only by FastAPI and is
never sent to the browser. `OPENAI_MODEL` selects the model (default
`gpt-4o-mini`); optionally set `OPENAI_BASE_URL` for an OpenAI-compatible endpoint
and `OPENAI_TIMEOUT_SECONDS` for the request timeout (default 30; maximum 120).
Install dependencies with `python -m pip install -r requirements.txt`, then
restart the API. Alternatively, set these variables in the server's environment
instead of using a `.env` file. There is no model training or fine-tuning step.

Without a working `OPENAI_API_KEY`, greetings, help, status checks, next-step
suggestions, and supported optimizer/history commands remain available through
the deterministic assistant. Recognized design questions also fall back to
deterministic handling if the provider is unavailable. Other free-form requests
report that the AI layer needs a working credential instead of pretending a
model answered. Provider errors never discard a verified design. The
optimizer's existing per-plan budget is unchanged; the assistant makes one
solver call per optimization request.
Valid design snapshots are kept in memory, isolated by the random plan-session
key; design history lasts only for the backend process lifetime. A candidate is
accepted only after the existing validator confirms it is valid. The original
scoring formula and weights remain unchanged.

The current optimizer always searches for the fewest feasible panels and does
not expose a parameter for forcing an exact panel count. A panel-count request
therefore triggers an actual solve and reports whether its result met the
requested count, without claiming the count is impossible if it did not. The
optimizer likewise has no objective-weight override or decision trace; requests
to prioritize a metric rerun its existing search and compare the actual result,
and explanations distinguish recorded facts from unrecorded alternatives.

Competition capacity limits are 100 m² and 20 devices per panel. The API and UI
cap the configurable device limit at 20; lower limits remain available.

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
