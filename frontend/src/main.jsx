import React, { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...options.headers },
    ...options,
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.detail || "The request failed.");
  return body;
}

function Metric({ label, value, help, delta }) {
  return (
    <div className="metric" title={help || undefined}>
      <div className="metric-label">{label}</div>
      <div className="metric-value">{value}</div>
      {delta !== undefined && <div className="metric-delta">{delta}</div>}
    </div>
  );
}

function App() {
  const [datasetAvailable, setDatasetAvailable] = useState(false);
  const [source, setSource] = useState("dataset");
  const [planIndex, setPlanIndex] = useState("0");
  const [plan, setPlan] = useState(null);
  const [result, setResult] = useState(null);
  const sceneFrame = useRef(null);
  const [activeTab, setActiveTab] = useState("optimized");
  const [displayMode, setDisplayMode] = useState("2d");
  const [readySceneUrl, setReadySceneUrl] = useState("");
  const [wallHeight, setWallHeight] = useState(2.7);
  const [wallTransparency, setWallTransparency] = useState(50);
  const [sceneSettings, setSceneSettings] = useState({ wallHeight: 2.7, wallTransparency: 50 });
  const [operation, setOperation] = useState("");
  const [error, setError] = useState("");
  const [settings, setSettings] = useState({
    max_nodes: 20,
    node_mode: "optimize",
    zone_via_other_rooms: false,
    time_budget_s: 30,
    compare: true,
  });
  const sceneUrl = result && plan
    ? `/api/plans/${plan.plan_key}/3d?wall_height=${sceneSettings.wallHeight}&wall_transparency=${sceneSettings.wallTransparency}${activeTab === "baseline" ? "&baseline=true" : ""}`
    : "";

  useEffect(() => {
    api("/api/status")
      .then(async (status) => {
        setDatasetAvailable(status.dataset_available);
        if (status.dataset_available) {
          setPlan(await api("/api/plans/index/0", { method: "POST" }));
        }
      })
      .catch((reason) => setError(reason.message));
  }, []);

  useEffect(() => {
    const timer = setTimeout(() => setSceneSettings({ wallHeight, wallTransparency }), 300);
    return () => clearTimeout(timer);
  }, [wallHeight, wallTransparency]);

  useEffect(() => {
    if (!result?.plotly_asset_url) return undefined;
    const prefetch = document.createElement("link");
    prefetch.rel = "prefetch";
    prefetch.as = "script";
    prefetch.href = result.plotly_asset_url;
    document.head.append(prefetch);
    return () => prefetch.remove();
  }, [result?.plotly_asset_url]);

  useEffect(() => {
    if (!sceneUrl) {
      setReadySceneUrl("");
      return undefined;
    }
    setReadySceneUrl("");
    const timer = setInterval(() => {
      if (sceneFrame.current?.contentWindow?.__cableRing3DReady) {
        setReadySceneUrl(sceneUrl);
        clearInterval(timer);
      }
    }, 100);
    return () => clearInterval(timer);
  }, [sceneUrl]);

  async function loadIndex(index) {
    setPlanIndex(String(index));
    setPlan(null);
    setResult(null);
    setError("");
    setOperation("loading");
    try {
      setPlan(await api(`/api/plans/index/${index}`, { method: "POST" }));
    } catch (reason) {
      setError(reason.message);
    } finally {
      setOperation("");
    }
  }

  function selectDataset() {
    setSource("dataset");
    const index = Number(planIndex);
    if (datasetAvailable && Number.isInteger(index) && index >= 0 && index <= 16999) {
      loadIndex(index);
    } else {
      setPlan(null);
      setResult(null);
    }
  }

  function selectFileSource() {
    setSource("file");
    setPlan(null);
    setResult(null);
    setError("");
  }

  async function loadFile(file) {
    if (!file) return;
    setSource("file");
    setPlan(null);
    setResult(null);
    setError("");
    setOperation("loading");
    try {
      const payload = JSON.parse(await file.text());
      setPlan(await api("/api/plans/upload", {
        method: "POST",
        body: JSON.stringify(payload),
      }));
    } catch (reason) {
      setError(reason instanceof SyntaxError ? "Could not read this file: invalid JSON." : reason.message);
    } finally {
      setOperation("");
    }
  }

  async function generate() {
    if (!plan) return;
    setError("");
    setOperation("solving");
    try {
      setResult(await api("/api/solve", {
        method: "POST",
        body: JSON.stringify({ plan_key: plan.plan_key, ...settings }),
      }));
      setActiveTab("optimized");
      setDisplayMode("2d");
    } catch (reason) {
      setError(reason.message);
    } finally {
      setOperation("");
    }
  }

  const output = result?.output;
  const baseline = result?.baseline;
  const metrics = output?.reported_metrics;
  const solver = output?.solver || {};
  const shownImage = result
    ? activeTab === "baseline" && baseline ? result.baseline_image : result.optimized_image
    : plan?.image;

  function updateSetting(key, value) {
    setSettings((current) => ({ ...current, [key]: value }));
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <section>
          <h2>1. Choose a floor plan</h2>
          <div className="radio-group" role="radiogroup" aria-label="Where does the plan come from?">
            <label>
              <input type="radio" name="source" checked={source === "dataset"}
                onChange={selectDataset} />
              <span>Pick a floor plan from the dataset</span>
            </label>
            <label>
              <input type="radio" name="source" checked={source === "file"}
                onChange={selectFileSource} />
              <span>Upload a plan JSON file</span>
            </label>
          </div>
          {source === "dataset" ? (
            <>
              <label className="field-label" htmlFor="plan-index">Plan number (0 - 16999)</label>
              <input id="plan-index" className="text-input" type="number" min="0" max="16999" step="1"
                value={planIndex} disabled={!datasetAvailable || operation !== ""}
                onChange={(event) => setPlanIndex(event.target.value)}
                onBlur={() => {
                  const value = Number(planIndex);
                  if (Number.isInteger(value) && value >= 0 && value <= 16999) loadIndex(value);
                  else setError("Plan number must be between 0 and 16999.");
                }}
                onKeyDown={(event) => {
                  if (event.key === "Enter") event.currentTarget.blur();
                }}
                title="Each number is a different apartment. Try 0, 2, 42, 593." />
              {!datasetAvailable && <p className="muted">data/ResPlan.pkl not found</p>}
              <p className="help-text">Each number is a different apartment. Try 0, 2, 42, 593.</p>
            </>
          ) : (
            <>
              <label className="field-label" htmlFor="plan-file">Plan JSON file</label>
              <input id="plan-file" className="file-input" type="file" accept=".json,application/json"
                onChange={(event) => loadFile(event.target.files?.[0])} />
              <p className="help-text">Ready-made files are in the project's inputs/ folder.</p>
            </>
          )}
        </section>

        <section>
          <h2>2. Solve it</h2>
          <button className="primary-button full-width" type="button" onClick={generate}
            disabled={!plan || operation !== ""}>
            {operation === "solving" ? "Optimising ..." : "Generate"}
          </button>
          <details className="settings-details">
            <summary>Settings (optional)</summary>
            <div className="settings-fields">
              <label className="field-label" htmlFor="max-nodes">Max nodes per hub</label>
              <input id="max-nodes" className="text-input" type="number" min="1" max="50"
                value={settings.max_nodes} onChange={(event) => updateSetting("max_nodes", Number(event.target.value))} />

              <label className="field-label" htmlFor="node-mode">Node points</label>
              <select id="node-mode" className="text-input" value={settings.node_mode}
                onChange={(event) => updateSetting("node_mode", event.target.value)}>
                <option value="optimize">optimize</option>
                <option value="fixed">fixed</option>
              </select>
              <p className="help-text">optimize = we choose node points; fixed = use node points from the input JSON</p>

              <label className="check-row">
                <input type="checkbox" checked={settings.zone_via_other_rooms}
                  onChange={(event) => updateSetting("zone_via_other_rooms", event.target.checked)} />
                <span>Zones may connect through bathrooms etc.</span>
              </label>

              <label className="field-label" htmlFor="time-budget">Time budget per plan (s)</label>
              <div className="range-line">
                <input id="time-budget" type="range" min="5" max="120" step="1"
                  value={settings.time_budget_s}
                  onChange={(event) => updateSetting("time_budget_s", Number(event.target.value))} />
                <output>{settings.time_budget_s}</output>
              </div>

              <label className="check-row">
                <input type="checkbox" checked={settings.compare}
                  onChange={(event) => updateSetting("compare", event.target.checked)} />
                <span>Also run the simple baseline (for comparison)</span>
              </label>
            </div>
          </details>
        </section>
        {error && <div className="notice error-notice" role="alert">{error}</div>}
        {operation === "loading" && <p className="muted">Loading plan ...</p>}
      </aside>

      <main className="main-content">
        <header className="page-heading">
          <h1>Cable Ring Optimizer</h1>
          <p>Fewest hubs first, then lowest J = L + 5X + O + 0.5B</p>
        </header>

        {!plan ? (
          <div className="notice info-notice">
            Upload a plan JSON file in the sidebar (e.g. inputs/PLAN_00042.json), or switch to picking a plan from the dataset.
          </div>
        ) : (
          <>
            <p className="plan-summary"><strong>{plan.plan_id}</strong>: {plan.room_count} rooms, {plan.eligible_count} that need a node
              (living, bedroom, kitchen, storage), {plan.eligible_area_m2.toFixed(1)} m² of eligible area.</p>

            {!result && <div className="notice info-notice">This is the empty floor plan. Press <strong>Generate</strong> in the sidebar to place hubs, nodes and cable.</div>}

            {metrics && (
              <>
                <div className="metrics-grid">
                  <Metric label="Valid" value={metrics.valid ? "yes" : "NO"} />
                  <Metric label="Hubs" value={metrics.hub_count} help={`lower bound ${solver.hub_lower_bound}`} />
                  <Metric label="Cost J" value={metrics.cost_J}
                    delta={baseline ? Number(metrics.cost_J - baseline.reported_metrics.cost_J).toFixed(2) : undefined} />
                  <Metric label="Length L (m)" value={metrics.total_length_m} />
                  <Metric label="Crossings X" value={metrics.crossings} />
                  <Metric label="Overlap O (m)" value={metrics.overlap_m} />
                  <Metric label="Bends B" value={metrics.bends} />
                </div>
                <div className="tabs" role="tablist" aria-label="Solution view">
                  <button role="tab" aria-selected={activeTab === "optimized"} className={activeTab === "optimized" ? "active" : ""}
                    onClick={() => setActiveTab("optimized")}>Optimised</button>
                  {baseline && <button role="tab" aria-selected={activeTab === "baseline"} className={activeTab === "baseline" ? "active" : ""}
                    onClick={() => setActiveTab("baseline")}>Baseline</button>}
                </div>
                <div className="view-switch" role="tablist" aria-label="Visualization">
                  <button role="tab" aria-selected={displayMode === "2d"} className={displayMode === "2d" ? "active" : ""}
                    onClick={() => setDisplayMode("2d")}>2D</button>
                  <button role="tab" aria-selected={displayMode === "3d"} className={displayMode === "3d" ? "active" : ""}
                    onClick={() => setDisplayMode("3d")}>3D</button>
                </div>
                {displayMode === "3d" && (
                  <div className="scene-controls">
                    <label className="scene-control" htmlFor="wall-height">
                      <span>Wall height</span>
                      <input id="wall-height" type="range" min="1.8" max="4" step="0.1"
                        value={wallHeight} onChange={(event) => setWallHeight(Number(event.target.value))} />
                      <output>{wallHeight.toFixed(1)} m</output>
                    </label>
                    <label className="scene-control" htmlFor="wall-transparency">
                      <span>Wall transparency</span>
                      <input id="wall-transparency" type="range" min="0" max="100" step="5"
                        value={wallTransparency}
                        onChange={(event) => setWallTransparency(Number(event.target.value))} />
                      <output>{wallTransparency}%</output>
                    </label>
                  </div>
                )}
              </>
            )}

            {result && sceneUrl && (
              <div className={`scene-stage ${displayMode === "3d" ? "active" : "preload"}`}>
                {displayMode === "3d" && readySceneUrl !== sceneUrl && (
                  <div className="scene-loading" role="status">Preparing 3D view ...</div>
                )}
                <iframe ref={sceneFrame} key={sceneUrl} className="plot-3d" title="Interactive 3D floor plan"
                  src={sceneUrl}
                  aria-hidden={displayMode !== "3d"} tabIndex={displayMode === "3d" ? 0 : -1} />
              </div>
            )}
            {displayMode === "2d" && shownImage &&
              <div className="plot-frame"><img src={shownImage} alt={`${plan.plan_id} floor plan`} /></div>}

            {output && (
              <>
                {(output.validation?.violations || []).map((violation, index) => (
                  <div className="notice error-notice validation-notice" key={`${violation.rule}-${index}`}>
                    {violation.rule}: {violation.message}
                  </div>
                ))}
                {(output.validation?.warnings || []).map((warning, index) => (
                  <div className="notice warning-notice validation-notice" key={index}>{warning}</div>
                ))}

                <details className="output-details">
                  <summary>Hubs</summary>
                  <div className="table-wrap">
                    <table>
                      <thead><tr><th>hub</th><th>room</th><th>rooms</th><th>area m²</th><th>sequence</th><th>ring m</th></tr></thead>
                      <tbody>{output.hubs.map((hub) => (
                        <tr key={hub.hub_id}>
                          <td>{hub.hub_id}</td><td>{hub.room_id}</td><td>{hub.assigned_rooms.join(", ")}</td>
                          <td>{hub.assigned_area_m2}</td><td>{hub.closed_sequence.join(" → ")}</td><td>{hub.ring_length_m}</td>
                        </tr>
                      ))}</tbody>
                    </table>
                  </div>
                </details>
                <details className="output-details">
                  <summary>Solver info</summary>
                  <pre>{JSON.stringify(solver, null, 2)}</pre>
                </details>

                <section className="download-section">
                  <h2>Output file</h2>
                  <p>This JSON is the submission file for this plan: hubs, nodes, visiting order and every cable polyline.</p>
                  <a className="primary-button download-button" href={`data:application/json;charset=utf-8,${encodeURIComponent(result.output_json)}`}
                    download={`${plan.plan_id}_output.json`}>Download output JSON</a>
                </section>
              </>
            )}
          </>
        )}
      </main>
    </div>
  );
}

createRoot(document.getElementById("root")).render(<App />);