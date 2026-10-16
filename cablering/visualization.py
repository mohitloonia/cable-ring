"""Floor-plan overlay: zones, hubs, nodes, visiting order, full rings, violations in red."""
from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from shapely.geometry import LineString

from .geometry import parts

ZONE_COLORS = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#17becf", "#8c564b", "#e377c2"]


def _fill(ax, geom, **kw):
    for p in parts(geom):
        ax.fill(*p.exterior.xy, **kw)
        for hole in p.interiors:
            ax.fill(*hole.xy, color="white", zorder=kw.get("zorder", 1))


def draw(plan, out: dict, ax=None, title: str | None = None, show_room_labels: bool = True,
         door_labels: list[dict] | None = None, closed_doors: list | None = None):
    """`door_labels`: [{"label", "x", "y"}] drawn as tags; `closed_doors`: polygons drawn red."""
    if ax is None:
        _, ax = plt.subplots(figsize=(10, 10))
    zone_of = {r: i for i, h in enumerate(out.get("hubs", [])) for r in h["assigned_rooms"]}

    _fill(ax, plan.outside, color="#e6f2dc", ec="#9cbf7a", lw=0.6, zorder=0)
    _fill(ax, plan.inner, color="#f4f4f4", zorder=0)
    for r in plan.rooms:
        if r.id in zone_of:
            c = ZONE_COLORS[zone_of[r.id] % len(ZONE_COLORS)]
            _fill(ax, r.poly, color=c, alpha=0.16, ec=c, lw=0.8, zorder=1)
        else:
            _fill(ax, r.poly, color="#e2e2e2", ec="#bbbbbb", lw=0.5, hatch="..", zorder=1)
        if show_room_labels:
            p = r.poly.representative_point()
            ax.text(p.x, p.y, f"{r.id} {r.type}\n{r.area:.1f} m²", fontsize=6.5, color="#555",
                    ha="center", va="top", zorder=3)
    _fill(ax, plan.walls, color="#2b2b2b", zorder=4)
    _fill(ax, plan.windows, color="#4aa8ff", zorder=5)
    for d in plan.doors:
        _fill(ax, d, color="#ff9f1c", zorder=5)
    _fill(ax, plan.front_door, color="#c1121f", zorder=5)
    for d in closed_doors or []:                       # what-if: door turned into wall
        _fill(ax, d.buffer(0.08), color="#d62828", zorder=6)
        c = d.centroid
        ax.scatter([c.x], [c.y], marker="x", s=90, color="white", linewidth=2.5, zorder=7)
    for t in door_labels or []:
        ax.text(t["x"], t["y"], t["label"], fontsize=7.5, fontweight="bold", color="#7a4a00",
                ha="center", va="center", zorder=8,
                bbox=dict(boxstyle="round,pad=0.18", fc="#fff3d6", ec="#e0a030", lw=0.8))

    for i, h in enumerate(out.get("hubs", [])):
        c = ZONE_COLORS[i % len(ZONE_COLORS)]
        for k, s in enumerate(h["route_segments"], start=1):
            xs, ys = zip(*s["polyline"])
            ax.plot(xs, ys, color=c, lw=2.2, alpha=0.9, zorder=7, solid_capstyle="round")
            line = LineString(s["polyline"]) if len(s["polyline"]) > 1 else None
            if line is not None and line.length > 0:
                m = line.interpolate(0.5, normalized=True)
                ax.text(m.x, m.y, str(k), fontsize=7, color="white", ha="center", va="center", zorder=9,
                        bbox=dict(boxstyle="circle,pad=0.2", fc=c, ec="none"))
        x, y = h["location"]["x"], h["location"]["y"]
        ax.scatter([x], [y], marker="*", s=420, color=c, edgecolor="black", zorder=10)
        ax.text(x, y + 0.35, h["hub_id"], fontsize=9, fontweight="bold", ha="center", zorder=11)
    for n in out.get("nodes", []):
        i = zone_of.get(n["room_id"], 0)
        x, y = n["location"]["x"], n["location"]["y"]
        ax.scatter([x], [y], s=120, color="white", edgecolor=ZONE_COLORS[i % len(ZONE_COLORS)],
                   linewidth=2.2, zorder=10)
        ax.text(x, y - 0.38, n["node_id"], fontsize=8, ha="center", zorder=11)

    for v in (out.get("validation") or {}).get("violations", []):
        w = v.get("where")
        if not w:
            continue
        if isinstance(w[0], list):
            (x1, y1), (x2, y2) = w
            ax.plot([x1, x2], [y1, y2], color="red", lw=3, zorder=12)
        else:
            ax.scatter([w[0]], [w[1]], marker="x", s=160, color="red", linewidth=3, zorder=12)

    m = out.get("reported_metrics", {})
    head = title or out.get("plan_id", "")
    if "validation" not in out:          # nothing solved yet: just the floor plan
        ax.set_title(f"{head}   (not solved yet)", fontsize=10)
    else:
        valid = out["validation"].get("valid")
        status = "VALID" if valid else "INVALID"
        ax.set_title(f"{head}   [{status}]\nhubs {m.get('hub_count')}   J = {m.get('cost_J')}   "
                     f"L = {m.get('total_length_m')} m   X = {m.get('crossings')}   "
                     f"O = {m.get('overlap_m')} m   B = {m.get('bends')}",
                     fontsize=10, color="black" if valid else "#c1121f")
    ax.legend(handles=[
        Line2D([], [], marker="*", ls="", ms=14, mfc="#888", mec="black", label="hub"),
        Line2D([], [], marker="o", ls="", ms=8, mfc="white", mec="#888", mew=2, label="node"),
        Line2D([], [], color="#888", lw=2, label="cable (numbers = visiting order)"),
        Line2D([], [], color="#ff9f1c", lw=6, label="door"),
        Line2D([], [], color="#4aa8ff", lw=6, label="window"),
        Line2D([], [], marker="x", ls="", ms=10, color="red", mew=3, label="violation"),
    ], loc="upper left", bbox_to_anchor=(1.0, 1.0), fontsize=8, frameon=False)
    ax.margins(y=0.015)
    ax.set_aspect("equal")
    ax.tick_params(labelsize=7)
    return ax


def save(plan, out: dict, path, title: str | None = None):
    fig, ax = plt.subplots(figsize=(11, 9))
    draw(plan, out, ax, title)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path
