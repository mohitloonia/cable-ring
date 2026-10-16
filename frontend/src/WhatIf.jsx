import React from "react";

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
    w_cross: rules?.w_cross ?? 5,
    w_bend: rules?.w_bend ?? 0.5,
    hub_move: { hub_id: "", room_id: "", dx: 0, dy: 0 },
  };
}

export function scenarioRequest(planKey, scenario) {
  const { hub_move: move, ...rest } = scenario;
  return {
    plan_key: planKey,
    ...rest,
    hub_move: move.hub_id && move.room_id ? move : null,
  };
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

export function WhatIfPanel({ options, scenario, setScenario, onRun, onReset, busy, scenarioResult, displayMode }) {
  if (!options) return <div className="notice info-notice">Loading the what-if simulator ...</div>;

  const rules = options.official_rules;
  const set = (key, value) => setScenario((s) => ({ ...s, [key]: value }));
  const setMove = (key, value) => setScenario((s) => ({ ...s, hub_move: { ...s.hub_move, [key]: value } }));
  const toggleDoor = (index) => setScenario((s) => ({
    ...s,
    closed_doors: s.closed_doors.includes(index)
      ? s.closed_doors.filter((d) => d !== index)
      : [...s.closed_doors, index],
  }));
  const movingHub = options.hubs.find((h) => h.hub_id === scenario.hub_move.hub_id);
  const info = scenarioResult?.scenario;
  const weightsChanged = scenario.w_cross !== rules.w_cross || scenario.w_bend !== rules.w_bend;

  return (
    <section className="whatif" aria-label="What-if simulator">
      <p className="whatif-intro">
        Change the building or the rules and watch the design respond. Anything that differs from the
        official benchmark is labelled, so a scenario is never mistaken for a submitted result.
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

        <fieldset className="card">
          <legend>Score weights</legend>
          <p className="help-text">J = L + <strong>{scenario.w_cross}</strong>·X + O + <strong>{scenario.w_bend}</strong>·B</p>
          <label className="field-label" htmlFor="wi-cross">Crossing weight <OfficialHint value={rules.w_cross} /></label>
          <div className="range-line">
            <input id="wi-cross" type="range" min="0" max="20" step="0.5"
              value={scenario.w_cross} onChange={(e) => set("w_cross", Number(e.target.value))} />
            <output>{scenario.w_cross}</output>
          </div>
          <label className="field-label" htmlFor="wi-bend">Bend weight <OfficialHint value={rules.w_bend} /></label>
          <div className="range-line">
            <input id="wi-bend" type="range" min="0" max="3" step="0.1"
              value={scenario.w_bend} onChange={(e) => set("w_bend", Number(e.target.value))} />
            <output>{scenario.w_bend}</output>
          </div>
        </fieldset>

        <fieldset className="card">
          <legend>Move a hub</legend>
          <p className="help-text">Put a hub where you think it should go. Zones stay the same and that ring is re-routed
            through your hub. Rule breaks show up in red.</p>
          <label className="field-label" htmlFor="wi-hub">Hub</label>
          <select id="wi-hub" className="text-input" value={scenario.hub_move.hub_id}
            onChange={(e) => {
              const hub = options.hubs.find((h) => h.hub_id === e.target.value);
              setScenario((s) => ({ ...s, hub_move: { hub_id: e.target.value, room_id: hub?.room_id || "", dx: 0, dy: 0 } }));
            }}>
            <option value="">Optimizer decides (official)</option>
            {options.hubs.map((h) => <option key={h.hub_id} value={h.hub_id}>{h.hub_id} (now in {h.room_id})</option>)}
          </select>
          {movingHub && (
            <>
              <label className="field-label" htmlFor="wi-room">Move it into room</label>
              <select id="wi-room" className="text-input" value={scenario.hub_move.room_id}
                onChange={(e) => setMove("room_id", e.target.value)}>
                {options.rooms.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.id} {r.type} ({r.area_m2} m²){movingHub.assigned_rooms.includes(r.id) ? "" : " · outside its zone"}
                  </option>
                ))}
              </select>
              <label className="field-label" htmlFor="wi-dx">Nudge east / west</label>
              <div className="range-line">
                <input id="wi-dx" type="range" min="-3" max="3" step="0.1"
                  value={scenario.hub_move.dx} onChange={(e) => setMove("dx", Number(e.target.value))} />
                <output>{scenario.hub_move.dx.toFixed(1)} m</output>
              </div>
              <label className="field-label" htmlFor="wi-dy">Nudge north / south</label>
              <div className="range-line">
                <input id="wi-dy" type="range" min="-3" max="3" step="0.1"
                  value={scenario.hub_move.dy} onChange={(e) => setMove("dy", Number(e.target.value))} />
                <output>{scenario.hub_move.dy.toFixed(1)} m</output>
              </div>
            </>
          )}
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
                  const official = scenarioResult.official_metrics[row.key];
                  const value = scenarioResult.metrics[row.key];
                  const fmt = row.format || ((v) => v);
                  return (
                    <tr key={row.key}>
                      <td>{row.label}</td><td>{fmt(official)}</td><td>{fmt(value)}</td>
                      <td><Delta row={row} official={official} scenario={value} /></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {weightsChanged && (
            <p className="help-text">
              J is scored with the scenario's weights. The official design scored with the same weights:
              <strong> {scenarioResult.official_cost_J_with_scenario_weights}</strong>.
            </p>
          )}

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
    </section>
  );
}
