import React, { useEffect, useRef, useState } from "react";

const ZONE_COLORS = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#17becf", "#8c564b", "#e377c2"];
const PAD = 0.6;              // metres of margin around the plan
const MIN_DRAG = 0.05;        // ignore drags shorter than this (m)

async function post(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const json = await response.json();
  if (!response.ok) throw new Error(json.detail || "The request failed.");
  return json;
}

// SVG y grows downwards, plan y grows upwards: draw every point at (x, -y).
const toPath = (rings) => rings
  .map((ring) => "M" + ring.map(([x, y]) => `${x} ${-y}`).join(" L") + " Z")
  .join(" ");

function zoneOf(output) {
  const zone = {};
  output.hubs.forEach((hub, i) => hub.assigned_rooms.forEach((room) => { zone[room] = i; }));
  return zone;
}

function devicePositions(output) {
  return {
    nodes: Object.fromEntries(output.nodes.map((n) => [n.room_id, [n.location.x, n.location.y]])),
    hubs: Object.fromEntries(output.hubs.map((h) => [h.hub_id, [h.location.x, h.location.y]])),
  };
}

const METRICS = [
  ["cost_J", "Cost J"], ["total_length_m", "Length L (m)"], ["crossings", "Crossings X"],
  ["overlap_m", "Overlap O (m)"], ["bends", "Bends B"],
];

export function DragEditor({ planKey, official, formula, onScenario, onReset }) {
  const svgRef = useRef(null);
  const [geometry, setGeometry] = useState(null);
  const [current, setCurrent] = useState(official);
  const [history, setHistory] = useState([]);
  const [moves, setMoves] = useState([]);
  const [drag, setDragState] = useState(null);
  const dragRef = useRef(null);                 // live drag, readable before React re-renders
  const setDrag = (value) => {
    const next = typeof value === "function" ? value(dragRef.current) : value;
    dragRef.current = next;
    setDragState(next);
  };
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  useEffect(() => {
    fetch(`/api/plans/${planKey}/geometry`).then((r) => r.json()).then(setGeometry)
      .catch(() => setMessage("Could not load the floor plan."));
  }, [planKey]);

  useEffect(() => { setCurrent(official); setHistory([]); setMoves([]); setMessage(""); }, [official]);

  if (!geometry) return <div className="notice info-notice">Loading the floor plan ...</div>;

  const [minx, miny, maxx, maxy] = geometry.bounds;
  const viewBox = `${minx - PAD} ${-(maxy + PAD)} ${maxx - minx + 2 * PAD} ${maxy - miny + 2 * PAD}`;
  const zone = zoneOf(current);
  const color = (room) => (zone[room] === undefined ? "#9ca3af" : ZONE_COLORS[zone[room] % ZONE_COLORS.length]);
  const hubColor = (i) => ZONE_COLORS[i % ZONE_COLORS.length];

  function svgPoint(event) {
    const svg = svgRef.current;
    const point = svg.createSVGPoint();
    point.x = event.clientX;
    point.y = event.clientY;
    const p = point.matrixTransform(svg.getScreenCTM().inverse());
    return [p.x, -p.y];
  }

  function startDrag(event, kind, id, label, x, y) {
    if (busy) return;
    event.preventDefault();
    try { event.currentTarget.setPointerCapture(event.pointerId); } catch { /* capture is optional */ }
    setMessage("");
    setDrag({ kind, id, label, ox: x, oy: y, x, y });
  }

  function moveDrag(event) {
    if (!dragRef.current) return;
    const [x, y] = svgPoint(event);
    setDrag((d) => ({ ...d, x, y }));
  }

  async function endDrag(event) {
    if (!dragRef.current) return;
    const [x, y] = svgPoint(event);                // the drop point is where the pointer is released
    const d = { ...dragRef.current, x, y };
    setDrag(null);
    if (Math.hypot(d.x - d.ox, d.y - d.oy) < MIN_DRAG) return;
    const positions = devicePositions(current);
    positions[d.kind === "node" ? "nodes" : "hubs"][d.id] = [d.x, d.y];
    setBusy(true);
    try {
      const response = await post("/api/drag", { plan_key: planKey, ...positions });
      if (!response.output.hubs.length) {        // e.g. dropped inside a wall: keep the last design
        const why = response.output.validation?.violations?.[0]?.message || "That spot cannot be cabled.";
        setMessage(`${d.label} cannot go there: ${why}`);
        return;
      }
      setHistory((h) => [...h, current]);
      setMoves((m) => [...m, { label: d.label, J: response.metrics.cost_J }]);
      setCurrent(response.output);
      onScenario?.();
    } catch (error) {
      setMessage(error.message);
    } finally {
      setBusy(false);
    }
  }

  function undo() {
    if (!history.length) return;
    setCurrent(history[history.length - 1]);
    setHistory((h) => h.slice(0, -1));
    setMoves((m) => m.slice(0, -1));
    setMessage("");
    if (history.length === 1) onReset?.(); else onScenario?.();
  }

  function reset() {
    setCurrent(official);
    setHistory([]);
    setMoves([]);
    setMessage("");
    onReset?.();
  }

  const optimal = official.reported_metrics;
  const now = current.reported_metrics;
  const delta = Math.round((now.cost_J - optimal.cost_J) * 1000) / 1000;
  const changed = history.length > 0;
  const violations = current.validation?.violations || [];
  const warnings = current.validation?.warnings || [];

  return (
    <div className="drag-layout">
      <div className={`drag-canvas ${busy ? "busy" : ""}`}>
        <svg ref={svgRef} viewBox={viewBox} role="img" aria-label="Floor plan: drag nodes and hubs"
          onPointerMove={moveDrag} onPointerUp={endDrag} onPointerCancel={() => setDrag(null)}>
          <path d={toPath(geometry.outside)} fill="#102331" stroke="#334155" strokeWidth="0.03" />
          {geometry.rooms.map((room) => (
            <path key={room.id} d={toPath(room.rings)} fillRule="evenodd"
                fill={room.eligible ? color(room.id) : "#172235"} fillOpacity={room.eligible ? 0.19 : 1}
                stroke={room.eligible ? color(room.id) : "#334155"} strokeWidth="0.03" />
          ))}
              <path d={toPath(geometry.walls)} fillRule="evenodd" fill="#334155" />
              <path d={toPath(geometry.windows)} fill="#38bdf8" />
              <path d={toPath(geometry.doors)} fill="#f59e0b" />
              <path d={toPath(geometry.front_door)} fill="#ef4444" />
          {geometry.rooms.map((room) => (
            <text key={`t-${room.id}`} x={room.label_at[0]} y={-room.label_at[1]} className="room-label">
              {room.id} {room.type}
            </text>
          ))}

          {current.hubs.map((hub, i) => hub.route_segments.map((seg, k) => (
            <polyline key={`${hub.hub_id}-${k}`} points={seg.polyline.map(([x, y]) => `${x},${-y}`).join(" ")}
              fill="none" stroke={hubColor(i)} strokeWidth="0.09" strokeLinejoin="round" strokeLinecap="round"
              opacity={drag ? 0.35 : 0.95} />
          )))}

          {current.nodes.map((node) => {
            const dragging = drag?.kind === "node" && drag.id === node.room_id;
            const { x, y } = node.location;
            return (
              <g key={node.node_id} className="device" opacity={dragging ? 0.35 : 1}
                onPointerDown={(e) => startDrag(e, "node", node.room_id, node.node_id, x, y)}>
                <title>{`${node.node_id} in ${node.room_id}: drag to move`}</title>
                <circle cx={x} cy={-y} r="0.22" fill="white" stroke={color(node.room_id)} strokeWidth="0.08" />
                <text x={x} y={-y + 0.55} className="device-label">{node.node_id}</text>
              </g>
            );
          })}
          {current.hubs.map((hub, i) => {
            const dragging = drag?.kind === "hub" && drag.id === hub.hub_id;
            const { x, y } = hub.location;
            return (
              <g key={hub.hub_id} className="device" opacity={dragging ? 0.35 : 1}
                onPointerDown={(e) => startDrag(e, "hub", hub.hub_id, hub.hub_id, x, y)}>
                <title>{`${hub.hub_id} in ${hub.room_id}: drag to move`}</title>
                <rect x={x - 0.22} y={-y - 0.22} width="0.44" height="0.44" transform={`rotate(45 ${x} ${-y})`}
                  fill={hubColor(i)} stroke="white" strokeWidth="0.06" />
                <text x={x} y={-y - 0.42} className="device-label hub-label">{hub.hub_id}</text>
              </g>
            );
          })}

          {drag && (
            <g pointerEvents="none">
              <line x1={drag.ox} y1={-drag.oy} x2={drag.x} y2={-drag.y} stroke="#f8fafc" strokeWidth="0.04" strokeDasharray="0.12 0.1" />
              {drag.kind === "node"
                ? <circle cx={drag.x} cy={-drag.y} r="0.24" fill="#f8fafc88" stroke="#f8fafc" strokeWidth="0.06" strokeDasharray="0.1 0.06" />
                : <rect x={drag.x - 0.24} y={-drag.y - 0.24} width="0.48" height="0.48" transform={`rotate(45 ${drag.x} ${-drag.y})`}
                    fill="#f8fafc88" stroke="#f8fafc" strokeWidth="0.06" strokeDasharray="0.1 0.06" />}
              <text x={drag.x} y={-drag.y - 0.45} className="device-label">{drag.label}</text>
            </g>
          )}
        </svg>
        {busy && <div className="drag-busy" role="status">Re-routing the cable ...</div>}
      </div>

      <aside className="drag-side">
        <p className="help-text">
          Drag any <strong>node</strong> (circle) or <strong>hub</strong> (diamond) to a new spot. The visiting order
          and every cable route are re-optimised through the doors, while the other devices stay put.
          Score stays official: <strong>{formula}</strong>.
        </p>

        {!changed ? (
          <div className="notice info-notice">This is the optimizer's design. Move a device to challenge it.</div>
        ) : delta > 0 ? (
          <div className="notice error-notice verdict">
            <strong>+{delta} J</strong> worse than the optimizer. Its placement was better.
          </div>
        ) : delta === 0 ? (
          <div className="notice info-notice verdict"><strong>Same cost</strong> as the optimizer.</div>
        ) : (
          <div className="notice success-notice verdict"><strong>{delta} J</strong> lower than the optimizer.</div>
        )}
        {message && <div className="notice warning-notice">{message}</div>}

        <table className="compare-table">
          <thead><tr><th>Metric</th><th>Optimal</th><th>Yours</th></tr></thead>
          <tbody>
            <tr><td>Valid</td><td>{optimal.valid ? "yes" : "NO"}</td><td>{now.valid ? "yes" : "NO"}</td></tr>
            {METRICS.map(([key, label]) => {
              const diff = (now[key] ?? 0) - (optimal[key] ?? 0);
              return (
                <tr key={key}>
                  <td>{label}</td><td>{optimal[key]}</td>
                  <td className={diff > 1e-9 ? "delta-bad" : diff < -1e-9 ? "delta-good" : ""}>{now[key]}</td>
                </tr>
              );
            })}
          </tbody>
        </table>

        {violations.map((v, i) => <div className="notice error-notice validation-notice" key={i}>{v.rule}: {v.message}</div>)}
        {warnings.map((w, i) => <div className="notice warning-notice validation-notice" key={`w${i}`}>{w}</div>)}

        {moves.length > 0 && (
          <ol className="move-list">
            {moves.map((m, i) => <li key={i}>Moved {m.label} → J {m.J}</li>)}
          </ol>
        )}
        <div className="whatif-actions">
          <button className="secondary-button" type="button" onClick={undo} disabled={!changed || busy}>Undo</button>
          <button className="secondary-button" type="button" onClick={reset} disabled={!changed || busy}>Reset to optimal</button>
        </div>
      </aside>
    </div>
  );
}
