"""Floor-plan overlay: zones, hubs, nodes, visiting order, full rings, violations in red."""
from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from shapely.geometry import LineString

from .geometry import parts

ZONE_COLORS = ["#38bdf8", "#34d399", "#a78bfa", "#f59e0b", "#f472b6", "#22d3ee", "#fb7185", "#a3e635"]


def _fill(ax, geom, **kw):
    for p in parts(geom):
        ax.fill(*p.exterior.xy, **kw)
        for hole in p.interiors:
            ax.fill(*hole.xy, color="#111827", zorder=kw.get("zorder", 1))


def draw(plan, out: dict, ax=None, title: str | None = None, show_room_labels: bool = True,
         door_labels: list[dict] | None = None, closed_doors: list | None = None):
    """`door_labels`: [{"label", "x", "y"}] drawn as tags; `closed_doors`: polygons drawn red."""
    if ax is None:
        _, ax = plt.subplots(figsize=(10, 10))
    ax.figure.patch.set_facecolor("#0b1120")
    ax.set_facecolor("#0b1120")
    zone_of = {r: i for i, h in enumerate(out.get("hubs", [])) for r in h["assigned_rooms"]}

    _fill(ax, plan.outside, color="#102331", ec="#334155", lw=0.6, zorder=0)
    _fill(ax, plan.inner, color="#111827", zorder=0)
    for r in plan.rooms:
        if r.id in zone_of:
            c = ZONE_COLORS[zone_of[r.id] % len(ZONE_COLORS)]
            _fill(ax, r.poly, color=c, alpha=0.19, ec=c, lw=0.85, zorder=1)
        else:
            _fill(ax, r.poly, color="#172235", ec="#334155", lw=0.55, hatch="..", zorder=1)
        if show_room_labels:
            p = r.poly.representative_point()
            ax.text(p.x, p.y, f"{r.id} {r.type}\n{r.area:.1f} m²", fontsize=6.5, color="#cbd5e1",
                    ha="center", va="top", zorder=3)
    _fill(ax, plan.walls, color="#334155", zorder=4)
    _fill(ax, plan.windows, color="#38bdf8", zorder=5)
    for d in plan.doors:
        _fill(ax, d, color="#f59e0b", zorder=5)
    _fill(ax, plan.front_door, color="#ef4444", zorder=5)
    for d in closed_doors or []:                       # what-if: door turned into wall
        _fill(ax, d.buffer(0.08), color="#ef4444", zorder=6)
        c = d.centroid
        ax.scatter([c.x], [c.y], marker="x", s=90, color="#f8fafc", linewidth=2.5, zorder=7)
    for t in door_labels or []:
        ax.text(t["x"], t["y"], t["label"], fontsize=7.5, fontweight="bold", color="#f6d38a",
                ha="center", va="center", zorder=8,
                bbox=dict(boxstyle="round,pad=0.18", fc="#342b1c", ec="#f59e0b", lw=0.8))

    for i, h in enumerate(out.get("hubs", [])):
        c = ZONE_COLORS[i % len(ZONE_COLORS)]
        for k, s in enumerate(h["route_segments"], start=1):
            xs, ys = zip(*s["polyline"])
            ax.plot(xs, ys, color=c, lw=2.6, alpha=0.98, zorder=7, solid_capstyle="round")
            line = LineString(s["polyline"]) if len(s["polyline"]) > 1 else None
            if line is not None and line.length > 0:
                m = line.interpolate(0.5, normalized=True)
                ax.text(m.x, m.y, str(k), fontsize=7, color="#07111f", fontweight="bold", ha="center", va="center", zorder=9,
                    bbox=dict(boxstyle="circle,pad=0.2", fc=c, ec="#e2e8f0", lw=0.35))
        x, y = h["location"]["x"], h["location"]["y"]
        ax.scatter([x], [y], marker="*", s=360, color=c, edgecolor="#f8fafc", linewidth=1.1, zorder=10)
        ax.text(x, y + 0.35, h["hub_id"], fontsize=9, color="#f8fafc", fontweight="bold", ha="center", zorder=11)
    for n in out.get("nodes", []):
        i = zone_of.get(n["room_id"], 0)
        x, y = n["location"]["x"], n["location"]["y"]
        ax.scatter([x], [y], s=120, color="#f8fafc", edgecolor=ZONE_COLORS[i % len(ZONE_COLORS)],
                   linewidth=2.2, zorder=10)
        ax.text(x, y - 0.38, n["node_id"], fontsize=8, color="#f8fafc", ha="center", zorder=11)

    for v in (out.get("validation") or {}).get("violations", []):
        w = v.get("where")
        if not w:
            continue
        if isinstance(w[0], list):
            (x1, y1), (x2, y2) = w
            ax.plot([x1, x2], [y1, y2], color="#ef4444", lw=3, zorder=12)
        else:
            ax.scatter([w[0]], [w[1]], marker="x", s=160, color="#ef4444", linewidth=3, zorder=12)

    m = out.get("reported_metrics", {})
    head = title or out.get("plan_id", "")
    if "validation" not in out:          # nothing solved yet: just the floor plan
        ax.set_title(f"{head}   (not solved yet)", fontsize=10, color="#cbd5e1")
    else:
        valid = out["validation"].get("valid")
        status = "VALID" if valid else "INVALID"
        ax.set_title(f"{head}   [{status}]\nhubs {m.get('hub_count')}   J = {m.get('cost_J')}   "
                     f"L = {m.get('total_length_m')} m   X = {m.get('crossings')}   "
                     f"O = {m.get('overlap_m')} m   B = {m.get('bends')}",
                     fontsize=10, color="#f8fafc" if valid else "#ef4444")
    legend = ax.legend(handles=[
        Line2D([], [], marker="*", ls="", ms=14, mfc="#38bdf8", mec="#f8fafc", label="hub"),
        Line2D([], [], marker="o", ls="", ms=8, mfc="#f8fafc", mec="#38bdf8", mew=2, label="node"),
        Line2D([], [], color="#38bdf8", lw=2, label="cable (numbers = visiting order)"),
        Line2D([], [], color="#f59e0b", lw=6, label="door"),
        Line2D([], [], color="#38bdf8", lw=6, label="window"),
        Line2D([], [], marker="x", ls="", ms=10, color="#ef4444", mew=3, label="violation"),
    ], loc="upper left", bbox_to_anchor=(1.0, 1.0), fontsize=8, frameon=True)
    legend.get_frame().set_facecolor("#111827")
    legend.get_frame().set_edgecolor("#334155")
    for text in legend.get_texts():
        text.set_color("#cbd5e1")
    ax.margins(y=0.015)
    ax.set_aspect("equal")
    ax.tick_params(labelsize=7, colors="#94a3b8")
    for spine in ax.spines.values():
        spine.set_color("#334155")
    return ax


def save(plan, out: dict, path, title: str | None = None):
    fig, ax = plt.subplots(figsize=(11, 9))
    draw(plan, out, ax, title)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path
