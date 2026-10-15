"""Interactive 3D model of a solved plan (Plotly).

What is drawn
  floor     rooms coloured by hub zone (excluded rooms grey), balconies green
  walls     raised to `wall_h`; doors are openings with a lintel above,
            windows have a sill, translucent glass and a lintel
  cable     each ring at ceiling height (`cable_h`), one colour per hub, with
            visiting-order numbers; nodes are ceiling devices on the cable
  hubs      wall-panel markers at `hub_h` with a vertical riser up to the cable
  animation a pulse travels round every ring (Play button)

The riser and the heights are presentation only. Scored geometry (x, y of every
polyline) is exactly the 2D output.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import plotly.graph_objects as go
import shapely

from .geometry import parts
from .visualization import ZONE_COLORS

FLOOR_EXCLUDED = "#e4e4e4"
FLOOR_OUTSIDE = "#dcebd2"
WALL_COLOR = "#d9d6cf"
WALL_EDGE = "#8a8a8a"
GLASS = "#9fd3ff"
DOOR_FRAME = "#f2b25c"
ENTRANCE = "#e05a64"
LABEL = "#1f2937"          # hub / node labels (sit over the light floors)
FLOOR_TINT = 0.76         # floors are zone colours mixed 76% with white, like the 2D view
PAGE_DARK = "#0e1117"      # Streamlit's dark background, used for exported HTML


def tint(hex_color: str, amount: float = FLOOR_TINT) -> str:
    """Mix a colour with white: amount 0 = unchanged, 1 = white."""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    mix = lambda c: round(c + (255 - c) * amount)
    return f"#{mix(r):02x}{mix(g):02x}{mix(b):02x}"


# --------------------------------------------------------------------------- #
# Mesh helpers
# --------------------------------------------------------------------------- #
class MeshBuilder:
    """Collects triangles for one Mesh3d trace (one colour)."""

    def __init__(self):
        self.x, self.y, self.z = [], [], []
        self.i, self.j, self.k = [], [], []

    def _tri(self, a, b, c):
        n = len(self.x)
        for p in (a, b, c):
            self.x.append(p[0]); self.y.append(p[1]); self.z.append(p[2])
        self.i.append(n); self.j.append(n + 1); self.k.append(n + 2)

    def flat(self, geom, z: float):
        """Horizontal face of a polygon (with holes) at height z."""
        for tri in _triangles(geom):
            c = list(tri.exterior.coords)[:3]
            self._tri(*[(x, y, z) for x, y in c])

    def prism(self, geom, z0: float, z1: float):
        """Extrude a polygon (with holes) from z0 to z1."""
        if z1 <= z0:
            return
        self.flat(geom, z0)
        self.flat(geom, z1)
        for p in parts(geom):
            for ring in (p.exterior, *p.interiors):
                c = list(ring.coords)
                for (ax, ay), (bx, by) in zip(c, c[1:]):
                    self._tri((ax, ay, z0), (bx, by, z0), (bx, by, z1))
                    self._tri((ax, ay, z0), (bx, by, z1), (ax, ay, z1))

    def trace(self, color: str, name: str, opacity: float = 1.0, **kw) -> go.Mesh3d | None:
        if not self.x:
            return None
        return go.Mesh3d(x=self.x, y=self.y, z=self.z, i=self.i, j=self.j, k=self.k,
                         color=color, opacity=opacity, name=name, flatshading=True,
                         lighting=dict(ambient=0.65, diffuse=0.6, specular=0.05, roughness=0.9),
                         lightposition=dict(x=100, y=200, z=400), hoverinfo="skip",
                         showlegend=False, **kw)


def _triangles(geom):
    out = []
    for p in parts(geom):
        try:
            tris = shapely.constrained_delaunay_triangles(p)
            out.extend(parts(tris))
        except Exception:               # older GEOS: unconstrained + keep inside ones
            from shapely.ops import triangulate
            out.extend(t for t in triangulate(p) if p.covers(t.representative_point()))
    return out


def _outline(geom, z: float):
    """x, y, z arrays (None-separated) of all polygon rings at height z."""
    xs, ys, zs = [], [], []
    for p in parts(geom):
        for ring in (p.exterior, *p.interiors):
            c = np.asarray(ring.coords)
            xs += list(c[:, 0]) + [None]
            ys += list(c[:, 1]) + [None]
            zs += [z] * len(c) + [None]
    return xs, ys, zs


def _resample(points, step: float):
    """Points every `step` metres along a polyline (for the animation)."""
    pts = [tuple(p) for p in points]
    out = [pts[0]]
    carry = 0.0
    for a, b in zip(pts, pts[1:]):
        seg = math.dist(a, b)
        d = step - carry
        while d <= seg:
            t = d / seg
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
            d += step
        carry = (carry + seg) % step
    out.append(pts[-1])
    return out


# --------------------------------------------------------------------------- #
# Scene
# --------------------------------------------------------------------------- #
def build_figure(plan, out: dict, wall_h: float = 2.7, wall_opacity: float = 0.5,
                 show_labels: bool = True, animate: bool = True, height: int | None = 720) -> go.Figure:
    door_h = min(2.1, wall_h - 0.1)
    sill_h, glass_top = min(0.9, wall_h * 0.33), min(2.1, wall_h - 0.1)
    cable_h = wall_h - 0.12
    hub_h = min(1.5, cable_h - 0.3)

    hubs = out.get("hubs", [])
    zone_of = {r: i for i, h in enumerate(hubs) for r in h["assigned_rooms"]}
    traces: list = []

    # ---- floor ------------------------------------------------------------
    # light tints (like the 2D view) so the strong colours belong to cable, hubs and nodes
    floors: dict[str, MeshBuilder] = {}
    for r in plan.rooms:
        color = tint(ZONE_COLORS[zone_of[r.id] % len(ZONE_COLORS)]) if r.id in zone_of else FLOOR_EXCLUDED
        floors.setdefault(color, MeshBuilder()).flat(r.poly, 0.0)
    outside = MeshBuilder()
    outside.flat(plan.outside, -0.02)
    traces.append(outside.trace(FLOOR_OUTSIDE, "outside"))
    for color, mb in floors.items():
        traces.append(mb.trace(color, "floor"))

    # ---- walls, doors, windows -------------------------------------------
    walls = MeshBuilder()
    walls.prism(plan.walls, 0.0, wall_h)
    for d in plan.doors:                       # lintel above each door opening
        walls.prism(d, door_h, wall_h)
    walls.prism(plan.front_door, door_h, wall_h)
    walls.prism(plan.windows, 0.0, sill_h)     # sill + lintel around the glass
    walls.prism(plan.windows, glass_top, wall_h)
    traces.append(walls.trace(WALL_COLOR, "walls", opacity=wall_opacity))
    glass = MeshBuilder()
    glass.prism(plan.windows, sill_h, glass_top)
    traces.append(glass.trace(GLASS, "windows", opacity=0.35))
    entrance = MeshBuilder()
    entrance.flat(plan.front_door, 0.01)
    traces.append(entrance.trace(ENTRANCE, "entrance"))
    door_floor = MeshBuilder()
    for d in plan.doors:
        door_floor.flat(d, 0.01)
    traces.append(door_floor.trace(DOOR_FRAME, "doors"))

    x, y, z = _outline(plan.walls, wall_h)     # crisp top edges of the walls
    traces.append(go.Scatter3d(x=x, y=y, z=z, mode="lines", line=dict(color=WALL_EDGE, width=2),
                               hoverinfo="skip", showlegend=False))

    # ---- rings ------------------------------------------------------------
    ring_paths = []
    for zi, h in enumerate(hubs):
        color = ZONE_COLORS[zi % len(ZONE_COLORS)]
        xs, ys, zs, ring = [], [], [], []
        for s in h["route_segments"]:
            pl = s["polyline"]
            xs += [p[0] for p in pl] + [None]
            ys += [p[1] for p in pl] + [None]
            zs += [cable_h] * len(pl) + [None]
            ring += pl if not ring else pl[1:]
        ring_paths.append((color, ring))
        traces.append(go.Scatter3d(
            x=xs, y=ys, z=zs, mode="lines", line=dict(color=color, width=8),
            name=f"{h['hub_id']} ring · {h['ring_length_m']:.1f} m", hoverinfo="name"))
        # visiting order numbers
        mids = []
        for k, s in enumerate(h["route_segments"], start=1):
            line = shapely.LineString(s["polyline"]) if len(s["polyline"]) > 1 else None
            if line is not None and line.length > 0:
                m = line.interpolate(0.5, normalized=True)
                mids.append((m.x, m.y, str(k)))
        if mids:
            traces.append(go.Scatter3d(
                x=[m[0] for m in mids], y=[m[1] for m in mids], z=[cable_h + 0.18] * len(mids),
                mode="text", text=[m[2] for m in mids], textfont=dict(color=color, size=13),
                hoverinfo="skip", showlegend=False))
        # hub: wall panel + riser to the ceiling cable
        hx, hy = h["location"]["x"], h["location"]["y"]
        traces.append(go.Scatter3d(x=[hx, hx], y=[hy, hy], z=[hub_h, cable_h], mode="lines",
                                   line=dict(color=color, width=5, dash="dot"),
                                   hoverinfo="skip", showlegend=False))
        traces.append(go.Scatter3d(
            x=[hx], y=[hy], z=[hub_h], mode="markers+text" if show_labels else "markers",
            marker=dict(symbol="diamond", size=12, color=color, line=dict(color="white", width=2)),
            text=[f"<b>{h['hub_id']}</b>"], textposition="top center",
            textfont=dict(size=15, color=LABEL),
            name=f"{h['hub_id']} hub", hovertext=(f"{h['hub_id']} · room {h['room_id']} · "
                                                  f"{h['assigned_area_m2']:.1f} m² · "
                                                  f"{len(h['assigned_nodes'])} nodes"),
            hoverinfo="text", showlegend=False))

    # ---- nodes (ceiling devices) -----------------------------------------
    nodes = out.get("nodes", [])
    if nodes:
        cols = [ZONE_COLORS[zone_of.get(n["room_id"], 0) % len(ZONE_COLORS)] for n in nodes]
        traces.append(go.Scatter3d(
            x=[n["location"]["x"] for n in nodes], y=[n["location"]["y"] for n in nodes],
            z=[cable_h] * len(nodes), mode="markers+text" if show_labels else "markers",
            marker=dict(size=7, color="white", line=dict(color=cols, width=4)),
            text=[n["node_id"] for n in nodes], textposition="bottom center",
            textfont=dict(size=13, color=LABEL), name="nodes",
            hovertext=[f"{n['node_id']} · room {n['room_id']}" for n in nodes], hoverinfo="text",
            showlegend=False))

    # ---- room labels on the floor ----------------------------------------
    if show_labels:
        pts = [(r.poly.representative_point(), r) for r in plan.rooms]
        traces.append(go.Scatter3d(
            x=[p.x for p, _ in pts], y=[p.y for p, _ in pts], z=[0.05] * len(pts), mode="text",
            text=[f"{r.id} {r.type}<br>{r.area:.1f} m²" for _, r in pts],
            textfont=dict(size=10, color="#6b7280"), hoverinfo="skip", showlegend=False))

    traces = [t for t in traces if t is not None]

    # ---- animation: a pulse running round each ring ----------------------
    frames = []
    if animate and ring_paths:
        samples = [(c, _resample(ring, 0.15)) for c, ring in ring_paths if len(ring) > 1]
        n_frames = max(len(s) for _, s in samples) if samples else 0
        pulse_start = len(traces)
        for c, s in samples:
            traces.append(go.Scatter3d(x=[s[0][0]], y=[s[0][1]], z=[cable_h], mode="markers",
                                       marker=dict(size=9, color=c, line=dict(color="white", width=2)),
                                       hoverinfo="skip", showlegend=False))
        idx = list(range(pulse_start, pulse_start + len(samples)))
        for f in range(n_frames):
            data = []
            for c, s in samples:
                p = s[int(f * len(s) / n_frames) % len(s)]
                data.append(go.Scatter3d(x=[p[0]], y=[p[1]], z=[cable_h]))
            frames.append(go.Frame(data=data, traces=idx, name=str(f)))

    fig = go.Figure(data=traces, frames=frames)
    minx, miny, maxx, maxy = plan.inner.union(plan.outside).bounds
    span = max(maxx - minx, maxy - miny, 1.0)
    fig.update_layout(
        height=height,
        margin=dict(l=0, r=0, t=10, b=0),
        paper_bgcolor="rgba(0,0,0,0)",
        legend=dict(x=0.01, y=0.98, bgcolor="rgba(28,32,42,0.85)", bordercolor="rgba(255,255,255,0.15)",
                    borderwidth=1, font=dict(color="#e5e7eb")),
        scene=dict(
            xaxis=dict(visible=False), yaxis=dict(visible=False), zaxis=dict(visible=False),
            aspectmode="manual",
            aspectratio=dict(x=(maxx - minx) / span, y=(maxy - miny) / span, z=wall_h * 1.6 / span),
            camera=dict(eye=dict(x=0.55, y=-0.85, z=0.75), up=dict(x=0, y=0, z=1)),
            bgcolor="rgba(0,0,0,0)",                # transparent: blends with the page
        ),
    )
    if frames:
        fig.update_layout(updatemenus=[dict(
            type="buttons", showactive=False, x=0.01, y=0.02, xanchor="left", yanchor="bottom",
            bgcolor="rgba(28,32,42,0.9)", bordercolor="rgba(255,255,255,0.2)",
            font=dict(color="#e5e7eb"),
            buttons=[
                dict(label="▶ Trace the cable", method="animate",
                     args=[None, dict(frame=dict(duration=40, redraw=True), fromcurrent=True,
                                      transition=dict(duration=0), mode="immediate")]),
                dict(label="❚❚ Pause", method="animate",
                     args=[[None], dict(frame=dict(duration=0, redraw=False), mode="immediate")]),
            ])])
    return fig


def standalone_html(fig: go.Figure) -> str:
    """The figure as one HTML page on a dark background (plotly.js embedded: works offline)."""
    html = fig.to_html(include_plotlyjs=True, full_html=True, config={"displaylogo": False},
                       default_height="100vh")
    style = f"<style>html,body{{margin:0;background:{PAGE_DARK};}}</style>"
    return html.replace("<head>", "<head>" + style, 1)


def save_html(plan, out: dict, path, **kw):
    """Standalone HTML file (works offline in any browser, no Python needed)."""
    fig = build_figure(plan, out, height=None, **kw)
    Path(path).write_text(standalone_html(fig), encoding="utf-8")
    return path
