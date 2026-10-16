import React, { useState } from "react";
import { DragEditor } from "./DragEditor.jsx";

const METRIC_ROWS = [
  { key: "valid", label: "Valid", format: (v) => (v ? "yes" : "NO") },
  { key: "hub_count", label: "Hubs", lowerIsBetter: true },
  { key: "cost_J", label: "Cost J", lowerIsBetter: true },
  { key: "total_length_m", label: "Length L (m)", lowerIsBetter: true },
  { key: "crossings", label: "Crossings X", lowerIsBetter: true },
  { key: "overlap_m", label: "Overlap O (m)", lowerIsBetter: true },
  { key: "bends", label: "Bends B", lowerIsBetter: true },
];

export function defaultScenario(rules) {
  return {
    closed_doors: [],
    max_area_m2: rules?.max_area_m2 ?? 100,
    max_nodes: rules?.max_nodes ?? 20,
    clearance_m: rules?.clearance_m ?? 0.5,
    zone_via_other_rooms: rules?.zone_via_other_rooms ?? false,
  };
}

export function scenarioRequest(planKey, scenario) {
  return { plan_key: planKey, ...scenario };
}

function Delta({ row, official, scenario }) {
  if (row.key === "valid" || typeof official !== "number" || typeof scenario !== "number") return null;
  const diff = Math.round((scenario - official) * 1000) / 1000;
  if (diff === 0) return <span className="delta-same">no change</span>;
  const better = row.lowerIsBetter ? diff < 0 : diff > 0;
  return <span className={better ? "delta-good" : "delta-bad"}>{diff > 0 ? "+" : ""}{diff}</span>;
}

function OfficialHint({ value, unit = "" }) {
  return <span className="official-hint">official {value}{unit}</span>;
}

export function WhatIfPanel({ options, planKey, official, scenario, setScenario, onRun, onReset, busy,
  scenarioResult, displayMode, onDragScenario, onDragReset }) {
  const [mode, setMode] = useState("drag");
  if (!options) return <div className="notice info-notice">Loading the what-if simulator ...</div>;

  const rules = options.official_rules;
  const formula = `J = L + ${rules.w_cross}·X + O + ${rules.w_bend}·B`;
  const set = (key, value) => setScenario((s) => ({ ...s, [key]: value }));
  const toggleDoor = (index) => setScenario((s) => ({
    ...s,
    closed_doors: s.closed_doors.includes(index)
      ? s.closed_doors.filter((d) => d !== index)
      : [...s.closed_doors, index],
  }));
  const info = scenarioResult?.scenario;

  return (
    <section className="whatif" aria-label="What-if simulator">
      <div className="view-switch whatif-mode" role="tablist" aria-label="What-if mode">
        <button role="tab" aria-selected={mode === "drag"} className={mode === "drag" ? "active" : ""}
          onClick={() => setMode("drag")}>Drag devices</button>
        <button role="tab" aria-selected={mode === "rules"} className={mode === "rules" ? "active" : ""}
          onClick={() => setMode("rules")}>Building &amp; rules</button>
      </div>

      {mode === "drag" ? (
        <>
          {/* kept mounted in 3D mode so the moves and undo history survive the switch */}
          <div hidden={displayMode !== "2d"}>
            <DragEditor planKey={planKey} official={official} formula={formula}
              onScenario={onDragScenario} onReset={onDragReset} />
          </div>
          {displayMode !== "2d" && (
            <p className="help-text">The 3D view below shows your latest drag. Switch to 2D to keep moving devices.</p>
          )}
        </>
      ) : (
        <>
          <p className="whatif-intro">
            Change the building or the rules and watch the design respond. Anything that differs from the
            official benchmark is labelled, so a scenario is never mistaken for a submitted result.
            The score always stays official: <strong>{formula}</strong>.
          </p>

          <div className="whatif-grid">
            <fieldset className="card">
              <legend>Building</legend>
              <p className="help-text">Close a door: it becomes wall, so cable cannot pass and the two rooms lose that link.
                Door tags (D1, D2, ...) are shown on the official plan below.</p>
              <div className="door-list">
                {options.doors.length === 0 && <p className="muted">No interior doors in this plan.</p>}
                {options.doors.map((door) => (
                  <label className="check-row" key={door.index}>
                    <input type="checkbox" checked={scenario.closed_doors.includes(door.index)}
                      onChange={() => toggleDoor(door.index)} />
                    <span><strong>Close {door.label}</strong> · {door.description}</span>
                  </label>
                ))}
              </div>
            </fieldset>

            <fieldset className="card">
              <legend>Rules</legend>
              <label className="field-label" htmlFor="wi-area">Max area per hub (m²) <OfficialHint value={rules.max_area_m2} /></label>
              <input id="wi-area" className="text-input" type="number" min="10" max="500" step="5"
                value={scenario.max_area_m2} onChange={(e) => set("max_area_m2", Number(e.target.value))} />
              <label className="field-label" htmlFor="wi-nodes">Max nodes per hub <OfficialHint value={rules.max_nodes} /></label>
              <input id="wi-nodes" className="text-input" type="number" min="1" max="50" step="1"
                value={scenario.max_nodes} onChange={(e) => set("max_nodes", Number(e.target.value))} />
              <label className="field-label" htmlFor="wi-clear">Hub / node clearance from walls <OfficialHint value={rules.clearance_m} unit=" m" /></label>
              <div className="range-line">
                <input id="wi-clear" type="range" min="0.2" max="1.2" step="0.05"
                  value={scenario.clearance_m} onChange={(e) => set("clearance_m", Number(e.target.value))} />
                <output>{scenario.clearance_m.toFixed(2)} m</output>
              </div>
              <label className="check-row">
                <input type="checkbox" checked={scenario.zone_via_other_rooms}
                  onChange={(e) => set("zone_via_other_rooms", e.target.checked)} />
                <span>Zones may connect through bathrooms etc.</span>
              </label>
            </fieldset>
          </div>

          <div className="whatif-actions">
            <button className="primary-button" type="button" onClick={onRun} disabled={busy}>
              {busy ? "Re-solving ..." : "Run scenario"}
            </button>
            <button className="secondary-button" type="button" onClick={onReset} disabled={busy}>
              Reset to official
            </button>
          </div>

          {scenarioResult && (
            <div className="whatif-results">
              {info.official ? (
                <div className="notice success-notice" role="status">
                  <strong>Official rules.</strong> This scenario matches the benchmark, so it is the official result.
                </div>
              ) : (
                <div className="notice warning-notice scenario-banner" role="status">
                  <strong>What-if scenario: differs from the official benchmark rules</strong>
                  <ul>{info.deviations.map((d) => <li key={d}>{d}</li>)}</ul>
                </div>
              )}
              {info.note && <div className="notice info-notice">{info.note}</div>}

              <div className="table-wrap">
                <table className="compare-table">
                  <thead><tr><th>Metric</th><th>Official</th><th>Scenario</th><th>Change</th></tr></thead>
                  <tbody>
                    {METRIC_ROWS.map((row) => {
                      const off = scenarioResult.official_metrics[row.key];
                      const value = scenarioResult.metrics[row.key];
                      const fmt = row.format || ((v) => v);
                      return (
                        <tr key={row.key}>
                          <td>{row.label}</td><td>{fmt(off)}</td><td>{fmt(value)}</td>
                          <td><Delta row={row} official={off} scenario={value} /></td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>

              {(scenarioResult.output.validation?.violations || []).map((v, i) => (
                <div className="notice error-notice validation-notice" key={`${v.rule}-${i}`}>{v.rule}: {v.message}</div>
              ))}
            </div>
          )}

          {displayMode === "2d" && (
            <div className="image-pair">
              <figure>
                <figcaption>Official solution (door tags D1, D2, ...)</figcaption>
                <img src={options.door_map} alt="Official solution with door labels" />
              </figure>
              {scenarioResult && (
                <figure>
                  <figcaption>{info.official ? "Scenario (official rules)" : "What-if scenario (closed doors in red)"}</figcaption>
                  <img src={scenarioResult.image} alt="What-if scenario result" />
                </figure>
              )}
            </div>
          )}
        </>
      )}
    </section>
  );
}
