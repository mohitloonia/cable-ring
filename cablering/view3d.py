"""Interactive 3D model of a solved plan (Plotly).

What is drawn
  floor     rooms coloured by type (excluded rooms grey), balconies green
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

FLOOR_EXCLUDED = "#40536f"
FLOOR_OUTSIDE = "#0d1827"
WALL_COLOR = "#415675"
WALL_EDGE = "#69d9f5"
GLASS = "#00d9ff"
DOOR_FRAME = "#ffbd2e"
ENTRANCE = "#ff4d6d"
LABEL = "#f4fbff"
FLOOR_ROOM_COLORS = {
    "living": "#087fba",
    "bedroom": "#536dfe",
    "kitchen": "#00a889",
    "storage": "#9257d8",
}
PAGE_DARK = "#0b1120"
RING_COLORS = ["#00e5ff", "#00ffc6", "#b66bff", "#ffb000", "#ff4fa3", "#55ff4f"]


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
                         lighting=dict(ambient=0.48, diffuse=0.76, specular=0.18, roughness=0.68),
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


def _scene_bounds(traces):
    coordinates = [[], [], []]
    for trace in traces:
        for axis, values in enumerate((trace.x, trace.y, trace.z)):
            if values is None:
                continue
            for value in np.asarray(values, dtype=object).ravel():
                if value is not None and np.isfinite(value):
                    coordinates[axis].append(float(value))
    if not all(coordinates):
        raise ValueError("The 3D scene has no geometry to frame.")
    return tuple((min(axis), max(axis)) for axis in coordinates)


def _fit_camera(bounds, aspectratio, viewport_aspect=1.3, padding=0.04):
    spans = [max(high - low, 1e-6) for low, high in bounds]
    scale = [ratio / span for ratio, span in zip(aspectratio, spans)]
    half_extents = [span * factor / 2 for span, factor in zip(spans, scale)]

    direction = np.asarray([0.56, -0.79, 0.54], dtype=float)
    direction /= np.linalg.norm(direction)
    up_axis = np.asarray([0.0, 0.0, 1.0])
    right = np.cross(direction, up_axis)
    right /= np.linalg.norm(right)
    up = np.cross(right, direction)
    corners = [
        np.asarray([x, y, z], dtype=float)
        for x in (-half_extents[0], half_extents[0])
        for y in (-half_extents[1], half_extents[1])
        for z in (-half_extents[2], half_extents[2])
    ]
    half_width = max(abs(float(np.dot(corner, right))) for corner in corners)
    half_height = max(abs(float(np.dot(corner, up))) for corner in corners)
    half_depth = max(float(np.dot(corner, direction)) for corner in corners)
    tangent = math.tan(math.radians(45 / 2))
    usable = 1 - 2 * padding
    distance = half_depth + max(
        half_height / (tangent * usable),
        half_width / (tangent * viewport_aspect * usable),
    )
    return direction * distance * 0.48


# --------------------------------------------------------------------------- #
# Scene
# --------------------------------------------------------------------------- #
def build_figure(plan, out: dict, wall_h: float = 1.5, wall_opacity: float = 0.1,
                 show_labels: bool = True, animate: bool = True, height: int | None = 720) -> go.Figure:
    door_h = min(2.1, wall_h - 0.1)
    sill_h, glass_top = min(0.9, wall_h * 0.33), min(2.1, wall_h - 0.1)
    cable_h = wall_h - 0.12
    hub_h = min(1.5, cable_h - 0.3)

    hubs = out.get("hubs", [])
    zone_of = {r: i for i, h in enumerate(hubs) for r in h["assigned_rooms"]}
    traces: list = []

    # ---- floor ------------------------------------------------------------
    # Room-type colors keep the floor legible while the route colors identify hub zones.
    floors: dict[str, MeshBuilder] = {}
    for r in plan.rooms:
        color = FLOOR_ROOM_COLORS.get(r.type, FLOOR_EXCLUDED) if r.id in zone_of else FLOOR_EXCLUDED
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
    traces.append(go.Scatter3d(x=x, y=y, z=z, mode="lines", line=dict(color=WALL_EDGE, width=1.25),
                               hoverinfo="skip", showlegend=False))

    # ---- rings ------------------------------------------------------------
    ring_paths = []
    for zi, h in enumerate(hubs):
        color = RING_COLORS[zi % len(RING_COLORS)]
        xs, ys, zs, ring = [], [], [], []
        for s in h["route_segments"]:
            pl = s["polyline"]
            xs += [p[0] for p in pl] + [None]
            ys += [p[1] for p in pl] + [None]
            zs += [cable_h] * len(pl) + [None]
            ring += pl if not ring else pl[1:]
        ring_paths.append((color, ring))
        traces.append(go.Scatter3d(
            x=xs, y=ys, z=zs, mode="lines", line=dict(color=color, width=15),
            opacity=0.12, hoverinfo="skip", showlegend=False))
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
                mode="text", text=[m[2] for m in mids], textfont=dict(color="#f8fafc", size=13),
                hoverinfo="skip", showlegend=False))
        # hub: wall panel + riser to the ceiling cable
        hx, hy = h["location"]["x"], h["location"]["y"]
        traces.append(go.Scatter3d(x=[hx, hx], y=[hy, hy], z=[hub_h, cable_h], mode="lines",
                                   line=dict(color=color, width=5, dash="dot"),
                                   hoverinfo="skip", showlegend=False))
        traces.append(go.Scatter3d(
            x=[hx], y=[hy], z=[hub_h], mode="markers+text" if show_labels else "markers",
            marker=dict(symbol="diamond", size=12, color=color, line=dict(color="#f8fafc", width=1.8)),
            text=[f"<b>{h['hub_id']}</b>"], textposition="top center",
            textfont=dict(size=14, color="#f8fafc"),
            name=f"{h['hub_id']} hub", hovertext=(f"{h['hub_id']} · room {h['room_id']} · "
                                                  f"{h['assigned_area_m2']:.1f} m² · "
                                                  f"{len(h['assigned_nodes'])} nodes"),
            hoverinfo="text", showlegend=False))

    # ---- nodes (ceiling devices) -----------------------------------------
    nodes = out.get("nodes", [])
    if nodes:
        cols = [RING_COLORS[zone_of.get(n["room_id"], 0) % len(RING_COLORS)] for n in nodes]
        traces.append(go.Scatter3d(
            x=[n["location"]["x"] for n in nodes], y=[n["location"]["y"] for n in nodes],
            z=[cable_h] * len(nodes), mode="markers+text" if show_labels else "markers",
            marker=dict(size=7, color="#f8fafc", line=dict(color=cols, width=3)),
            text=[n["node_id"] for n in nodes], textposition="bottom center",
            textfont=dict(size=11, color="#dbe6f4"), name="nodes",
            hovertext=[f"{n['node_id']} · room {n['room_id']}" for n in nodes], hoverinfo="text",
            showlegend=False))

    # ---- room labels on the floor ----------------------------------------
    if show_labels:
        pts = [(r.poly.representative_point(), r) for r in plan.rooms]
        traces.append(go.Scatter3d(
            x=[p.x for p, _ in pts], y=[p.y for p, _ in pts], z=[0.05] * len(pts), mode="markers",
            marker=dict(size=7, color="#cbd5e1", opacity=0.12),
            hovertext=[f"{r.id} {r.type} · {r.area:.1f} m²" for _, r in pts],
            hoverinfo="text", showlegend=False))

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
    bounds = _scene_bounds(traces)
    minx, maxx = bounds[0]
    miny, maxy = bounds[1]
    minz, maxz = bounds[2]
    spans = [max(high - low, 1e-6) for low, high in bounds]
    span = max(spans)
    aspectratio = (spans[0] / span, spans[1] / span, max(spans[2] * 1.4 / span, 0.12))
    camera_eye = _fit_camera(bounds, aspectratio)
    fig.update_layout(
        height=height,
        margin=dict(l=0, r=0, t=10, b=0),
        paper_bgcolor="rgba(0,0,0,0)",
        autosize=height is None,
        legend=dict(x=0.015, y=0.98, bgcolor="rgba(11,17,32,0.82)", bordercolor="rgba(148,163,184,0.24)",
                borderwidth=1, font=dict(color="#dbe6f4")),
        scene=dict(
            xaxis=dict(visible=False, range=[minx, maxx]),
            yaxis=dict(visible=False, range=[miny, maxy]),
            zaxis=dict(visible=False, range=[minz, maxz]),
            aspectmode="manual",
            aspectratio=dict(x=aspectratio[0], y=aspectratio[1], z=aspectratio[2]),
            camera=dict(
                center=dict(x=0, y=0, z=0),
                eye=dict(x=float(camera_eye[0]), y=float(camera_eye[1]), z=float(camera_eye[2])),
                up=dict(x=0, y=0, z=1),
                projection=dict(type="perspective"),
            ),
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


# Slow camera orbit round the plan (one turn a minute). It pauses while the viewer drags or
# zooms, resumes from their view after a short idle, and stops while the view is off-screen.
# The WebGL camera is moved directly (~0.05 ms a step); Plotly.relayout would re-run the
# plot pipeline (~50 ms a step) and make the page sluggish. The layout copy of the camera
# is kept in step so a redraw (e.g. the cable animation) does not jump back.
# Plotly replaces {plot_id} with the plot's element id.
ORBIT_SCRIPT = """
(function () {
  var gd = document.getElementById('{plot_id}');
  var initialCamera = JSON.parse(JSON.stringify((gd.layout.scene || {}).camera || {}));
  var initialDirection = [initialCamera.eye.x, initialCamera.eye.y, initialCamera.eye.z];
  var initialDistance = Math.hypot.apply(null, initialDirection);
  initialDirection = initialDirection.map(function (value) { return value / initialDistance; });
  var SPEED = 2 * Math.PI / 60, IDLE_MS = 2500, STEP_MS = 33;
  var on = true, visible = true, down = false, lastUser = 0, last = null;
  var lastAspect = null, initialFitAspect = 1.3;
  function user() { lastUser = Date.now(); }
  function cameraDistance(direction, aspect) {
    var ratio = gd.layout.scene.aspectratio;
    var half = [ratio.x / 2, ratio.y / 2, ratio.z / 2];
    var right = [-direction[1], direction[0], 0];
    var rightLength = Math.hypot(right[0], right[1]);
    right = rightLength < 1e-6 ? [1, 0, 0] :
      right.map(function (value) { return value / rightLength; });
    var up = [
      right[1] * direction[2],
      -right[0] * direction[2],
      right[0] * direction[1] - right[1] * direction[0]
    ];
    var width = 0, height = 0, depth = 0;
    for (var sx of [-1, 1]) for (var sy of [-1, 1]) for (var sz of [-1, 1]) {
      var corner = [sx * half[0], sy * half[1], sz * half[2]];
      var projectedWidth = Math.abs(corner[0] * right[0] + corner[1] * right[1] + corner[2] * right[2]);
      var projectedHeight = Math.abs(corner[0] * up[0] + corner[1] * up[1] + corner[2] * up[2]);
      width = Math.max(width, projectedWidth);
      height = Math.max(height, projectedHeight);
      depth = Math.max(depth, corner[0] * direction[0] + corner[1] * direction[1] + corner[2] * direction[2]);
    }
    var tanHalfFov = Math.tan(Math.PI / 8), usable = 0.92;
    return (depth + Math.max(height / (tanHalfFov * usable), width / (tanHalfFov * aspect * usable))) * 0.48;
  }
  function resize() {
    if (!window.Plotly || !gd || !gd._fullLayout) return;
    window.Plotly.Plots.resize(gd);
    requestAnimationFrame(function () {
      if (!gd._fullLayout || !gd._fullLayout.scene) return;
      var rect = gd.getBoundingClientRect();
      var aspect = Math.max(rect.width / Math.max(rect.height, 1), 0.1);
      var full = gd._fullLayout.scene, scene = full._scene, camera = scene.getCamera();
      var direction = [camera.eye.x, camera.eye.y, camera.eye.z];
      var distance = Math.hypot.apply(null, direction);
      direction = direction.map(function (value) { return value / distance; });
      var previousFit = cameraDistance(direction, lastAspect || initialFitAspect);
      var zoom = distance / previousFit;
      var fittedDistance = cameraDistance(direction, aspect);
      camera.eye = {
        x: direction[0] * fittedDistance * zoom,
        y: direction[1] * fittedDistance * zoom,
        z: direction[2] * fittedDistance * zoom
      };
      scene.setViewport({camera: camera, aspectratio: full.aspectratio});
      full.camera.eye = camera.eye;
      gd.layout.scene.camera = JSON.parse(JSON.stringify(camera));
      var initialDistance = cameraDistance(initialDirection, aspect);
      initialCamera.eye = {
        x: initialDirection[0] * initialDistance,
        y: initialDirection[1] * initialDistance,
        z: initialDirection[2] * initialDistance
      };
      lastAspect = aspect;
    });
  }
  window.addEventListener('resize', resize, {passive: true});
  if (window.ResizeObserver) new ResizeObserver(resize).observe(gd);
  requestAnimationFrame(resize);
  gd.addEventListener('pointerdown', function () { down = true; user(); }, true);
  window.addEventListener('pointerup', function () { down = false; user(); }, true);
  gd.addEventListener('wheel', user, {capture: true, passive: true});
  gd.addEventListener('touchstart', user, {capture: true, passive: true});
  if (window.IntersectionObserver) {
    new IntersectionObserver(function (e) { visible = e[0].isIntersecting; }).observe(gd);
  }
  var btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'orbit-toggle';
  function label() { btn.textContent = on ? '\\u275a\\u275a Stop rotating' : '\\u27f3 Rotate'; }
  btn.onclick = function () { on = !on; last = null; label(); };
  label();
  document.body.appendChild(btn);
    var reset = document.createElement('button');
    reset.type = 'button';
    reset.className = 'camera-reset';
    reset.textContent = 'Reset camera';
    reset.setAttribute('aria-label', 'Reset camera');
    reset.onclick = function () {
        user(); last = null;
        if (window.Plotly) window.Plotly.relayout(gd, {'scene.camera': initialCamera});
    };
    document.body.appendChild(reset);
  function step(t) {
    requestAnimationFrame(step);
    var idle = Date.now() - lastUser > IDLE_MS;
    var full = gd._fullLayout && gd._fullLayout.scene, scene = full && full._scene;
    if (!on || !visible || down || !idle || !scene || document.hidden) { last = null; return; }
    if (last === null) { last = t; return; }
    if (t - last < STEP_MS) return;
    var dt = Math.min(t - last, 100) / 1000;
    last = t;
    var cam = scene.getCamera(), e = cam.eye;
    var r = Math.hypot(e.x, e.y), a = Math.atan2(e.y, e.x) + SPEED * dt;
    cam.eye = {x: r * Math.cos(a), y: r * Math.sin(a), z: e.z};
    scene.setViewport({camera: cam, aspectratio: full.aspectratio});
    full.camera.eye = cam.eye;
    if (gd.layout.scene && gd.layout.scene.camera) gd.layout.scene.camera.eye = cam.eye;
  }
  requestAnimationFrame(step);
})();
"""
ORBIT_STYLE = (".orbit-toggle{position:fixed;right:12px;top:40px;z-index:10;padding:6px 12px;"
               "font:12px Inter, sans-serif;color:#e2eaf5;background:rgba(17,24,39,.88);"
               "border:1px solid rgba(148,163,184,.3);border-radius:6px;cursor:pointer;backdrop-filter:blur(8px)}"
               ".orbit-toggle:hover,.camera-reset:hover{border-color:#18c6bc;color:#2dd4bf}"
               ".camera-reset{position:fixed;right:12px;top:80px;z-index:10;padding:6px 12px;"
               "font:12px Inter, sans-serif;color:#e2eaf5;background:rgba(17,24,39,.88);"
               "border:1px solid rgba(148,163,184,.3);border-radius:6px;cursor:pointer;backdrop-filter:blur(8px)}")


def standalone_html(fig: go.Figure, include_plotlyjs: bool | str = True,
                   notify_parent: bool = False) -> str:
    """The figure as one HTML page on a dark background (plotly.js embedded: works offline).

    The cable animation only runs from its button: Plotly would otherwise auto-play every
    frame on load, redrawing the scene for a minute or more and making rotation stall.
    """
    post_script = ORBIT_SCRIPT
    if notify_parent:
        post_script += ('window.__cableRing3DReady = true; '
                        'window.parent.postMessage({type: "cable-ring-3d-ready"}, "*");')
    html = fig.to_html(include_plotlyjs=include_plotlyjs, full_html=True,
                       config={"displaylogo": False, "responsive": True, "scrollZoom": True},
                       default_width="100vw", default_height="100vh", post_script=post_script, auto_play=False)
    style = (f"<style>html,body{{margin:0;width:100%;height:100%;background:{PAGE_DARK};overflow:hidden}}"
             ".plotly-graph-div{width:100%!important;height:100%!important}" + ORBIT_STYLE + "</style>")
    return html.replace("<head>", "<head>" + style, 1)


def save_html(plan, out: dict, path, **kw):
    """Standalone HTML file (works offline in any browser, no Python needed)."""
    fig = build_figure(plan, out, height=None, **kw)
    Path(path).write_text(standalone_html(fig), encoding="utf-8")
    return path
