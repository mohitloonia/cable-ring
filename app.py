"""Streamlit demo: pick or upload a plan -> Generate -> overlay, scores, JSON download.

    python -m streamlit run app.py
"""
import json

import matplotlib.pyplot as plt
import streamlit as st

from cablering.config import DATA_PATH, Config
from cablering.export_json import check_schema
from cablering.input_loader import load_dataset, plan_from_index, plan_from_input_dict
from cablering.optimizer import solve
from cablering.visualization import draw

st.set_page_config(page_title="Cable Ring Optimizer", layout="wide")
st.title("Cable Ring Optimizer")
st.caption("Fewest hubs first, then lowest J = L + 5X + O + 0.5B")


@st.cache_resource(show_spinner="Loading ResPlan dataset ...")
def dataset():
    return load_dataset()


FROM_DATASET = "Pick a floor plan from the dataset"
FROM_FILE = "Upload a plan JSON file"

with st.sidebar:
    st.header("1. Choose a floor plan")
    source = st.radio("Where does the plan come from?", [FROM_DATASET, FROM_FILE],
                      help="The dataset (data/ResPlan.pkl) holds 17,000 floor plans, numbered 0 to 16999. "
                           "A plan JSON file is one floor plan on its own, e.g. inputs/PLAN_00042.json.")
    plan = None
    if source == FROM_DATASET:
        if DATA_PATH.exists():
            idx = st.number_input("Plan number (0 – 16999)", 0, 16999, 0, step=1,
                                  help="Each number is a different apartment. Try 0, 2, 42, 593.")
            plan = plan_from_index(int(idx), dataset())
        else:
            st.error("data/ResPlan.pkl not found")
    else:
        up = st.file_uploader("Plan JSON file", type="json",
                              help="Ready-made files are in the project's inputs/ folder.")
        if up is not None:
            try:
                plan = plan_from_input_dict(json.load(up))
            except Exception as e:
                st.error(f"Could not read this file: {e}")

    st.header("2. Solve it")
    go = st.button("Generate", type="primary", disabled=plan is None, use_container_width=True)

    with st.expander("Settings (optional)"):
        max_nodes = st.number_input("Max nodes per hub", 1, 50, Config.max_nodes)
        node_mode = st.selectbox("Node points", ["optimize", "fixed"],
                                 help="optimize = we choose node points; fixed = use node points from the input JSON")
        via_other = st.checkbox("Zones may connect through bathrooms etc.", value=False)
        budget = st.slider("Time budget per plan (s)", 5, 120, int(Config.time_budget_s))
        compare = st.checkbox("Also run the simple baseline (for comparison)", value=True)

if plan is None:
    st.info("Upload a plan JSON file in the sidebar (e.g. inputs/PLAN_00042.json), "
            "or switch to picking a plan from the dataset.")
    st.stop()

cfg = Config(max_nodes=int(max_nodes), node_mode=node_mode, zone_via_other_rooms=via_other,
             time_budget_s=float(budget))

if go:
    with st.spinner("Optimising ..."):
        st.session_state["result"] = (plan.plan_id, solve(plan, cfg),
                                      solve(plan, cfg, baseline=True) if compare else None)

res = st.session_state.get("result")
if not res or res[0] != plan.plan_id:
    eligible = plan.eligible_rooms
    st.write(f"**{plan.plan_id}**: {len(plan.rooms)} rooms, {len(eligible)} that need a node "
             f"(living, bedroom, kitchen, storage), {sum(r.area for r in eligible):.1f} m² of eligible area.")
    st.info("This is the empty floor plan. Press **Generate** in the sidebar to place hubs, nodes and cable.")
    fig, ax = plt.subplots(figsize=(9, 8))
    draw(plan, {"hubs": [], "nodes": [], "reported_metrics": {}}, ax, title=plan.plan_id)
    st.pyplot(fig)
    st.stop()

_, out, base = res
m, s = out["reported_metrics"], out["solver"]
cols = st.columns(7)
cols[0].metric("Valid", "yes" if m["valid"] else "NO")
cols[1].metric("Hubs", m["hub_count"], help=f"lower bound {s['hub_lower_bound']}")
cols[2].metric("Cost J", m["cost_J"],
               delta=None if not base else round(m["cost_J"] - base["reported_metrics"]["cost_J"], 2),
               delta_color="inverse")
cols[3].metric("Length L (m)", m["total_length_m"])
cols[4].metric("Crossings X", m["crossings"])
cols[5].metric("Overlap O (m)", m["overlap_m"])
cols[6].metric("Bends B", m["bends"])

tabs = st.tabs(["Optimised", "Baseline"] if base else ["Optimised"])
with tabs[0]:
    fig, ax = plt.subplots(figsize=(11, 9))
    draw(plan, out, ax)
    st.pyplot(fig)
if base:
    with tabs[1]:
        fig, ax = plt.subplots(figsize=(11, 9))
        draw(plan, base, ax, title=f"{plan.plan_id} (baseline)")
        st.pyplot(fig)

for v in out["validation"]["violations"]:
    st.error(f"{v['rule']}: {v['message']}")
for w in out["validation"]["warnings"]:
    st.warning(w)

with st.expander("Hubs"):
    st.dataframe([{"hub": h["hub_id"], "room": h["room_id"], "rooms": ", ".join(h["assigned_rooms"]),
                   "area m²": h["assigned_area_m2"], "sequence": " → ".join(h["closed_sequence"]),
                   "ring m": h["ring_length_m"]} for h in out["hubs"]])
with st.expander("Solver info"):
    st.json(s)

st.subheader("Output file")
st.write("This JSON is the submission file for this plan: hubs, nodes, visiting order and every cable polyline.")
payload = json.dumps(check_schema(out).model_dump(by_alias=True), indent=2)
st.download_button("Download output JSON", payload, file_name=f"{plan.plan_id}_output.json",
                   mime="application/json", type="primary")
