import React, { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

const INITIAL_SUGGESTIONS = [
  "Explain my current design",
  "Why did you use this many panels?",
  "Reduce cable length",
  "Find a design with fewer crossings",
  "Explain the optimization score",
  "Check whether a smaller panel count is feasible",
];

function followUpSuggestions(message, response, answer) {
  const text = message.toLowerCase();
  const reply = response.toLowerCase();
  const metrics = answer.output?.reported_metrics;
  if (!answer.output) return ["What can you do?", "Check whether this solution is valid"];
  const requestedOptimization = /\b(optimi[sz]|reduce|shorten|shorter|make it better|improv|fewer|less|decrease|lower|minimi[sz]|try one|use one)\b/.test(text);
  if (requestedOptimization && /couldn't|could not|failed validation|not met|didn't find|kept the current design/.test(reply)) {
    return [
      "Explain what prevented the change",
      "Reduce cable length",
      "Find a design with fewer crossings",
    ];
  }
  if (/\b(panel|hub)\b/.test(text)) {
    return metrics?.hub_count > 1
      ? ["Can we try one panel?", "Explain the panel capacity limits", "Compare with the previous design"]
      : ["Explain the panel capacity limits", "Explain my current design", "Compare with the previous design"];
  }
  if (answer.changed) {
    return ["Try to reduce it further", "Compare with the previous design", "Explain what changed"];
  }
  if (/\b(score|cost|objective)\b/.test(text)) {
    return ["Reduce cable length", "Can we reduce the number of bends?", "Explain the score components"];
  }
  if (/\b(valid|validation|status)\b/.test(text)) {
    return ["Explain my current design", "Explain the optimization score", "Find a design with fewer crossings"];
  }
  return ["Reduce cable length", "Find a design with fewer crossings", "Explain the optimization score"];
}

function progressMessage(message) {
  if (/\b(optimi[sz]|minimi[sz]|improv|better|reduce|shorten|shorter|decrease|lower|fewer|less|try (?:with )?(?:one|two|three|\d+) panel|use (?:one|two|three|\d+) panel)\b/i.test(message)) {
    return "I’m searching for a valid improvement with the existing optimizer. This may take a moment.";
  }
  return "Checking the current design…";
}

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
  const [assistantAiAvailable, setAssistantAiAvailable] = useState(false);
  const [source, setSource] = useState("dataset");
  const [planIndex, setPlanIndex] = useState("0");
  const [plan, setPlan] = useState(null);
  const [result, setResult] = useState(null);
  const [activeTab, setActiveTab] = useState("optimized");
  const [operation, setOperation] = useState("");
  const [error, setError] = useState("");
  const [assistantInput, setAssistantInput] = useState("");
  const [chatMessages, setChatMessages] = useState([]);
  const [suggestions, setSuggestions] = useState(INITIAL_SUGGESTIONS);
  const [assistantProgress, setAssistantProgress] = useState("");
  const assistantRequestActive = useRef(false);
  const [settings, setSettings] = useState({
    max_nodes: 20,
    node_mode: "optimize",
    zone_via_other_rooms: false,
    time_budget_s: 30,
    compare: true,
  });

  useEffect(() => {
    api("/api/status")
      .then(async (status) => {
        setDatasetAvailable(status.dataset_available);
        setAssistantAiAvailable(status.assistant_ai_available);
        if (status.dataset_available) {
          setPlan(await api("/api/plans/index/0", { method: "POST" }));
        }
      })
      .catch((reason) => setError(reason.message));
  }, []);

  async function loadIndex(index) {
    setPlanIndex(String(index));
    setPlan(null);
    setResult(null);
    setChatMessages([]);
    setAssistantInput("");
    setSuggestions(INITIAL_SUGGESTIONS);
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
      setChatMessages([]);
      setAssistantInput("");
      setSuggestions(INITIAL_SUGGESTIONS);
    }
  }

  function selectFileSource() {
    setSource("file");
    setPlan(null);
    setResult(null);
    setChatMessages([]);
    setAssistantInput("");
    setSuggestions(INITIAL_SUGGESTIONS);
    setError("");
  }

  async function loadFile(file) {
    if (!file) return;
    setSource("file");
    setPlan(null);
    setResult(null);
    setChatMessages([]);
    setAssistantInput("");
    setSuggestions(INITIAL_SUGGESTIONS);
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
      const solved = await api("/api/solve", {
        method: "POST",
        body: JSON.stringify({ plan_key: plan.plan_key, ...settings }),
      });
      setResult(solved);
      setActiveTab("optimized");
    } catch (reason) {
      setError(reason.message);
    } finally {
      setOperation("");
    }
  }

  async function sendAssistant(message = assistantInput) {
    const text = message.trim();
    if (!plan || !text || operation !== "" || assistantRequestActive.current) return;
    assistantRequestActive.current = true;
    setChatMessages((current) => [...current, { role: "user", content: text }]);
    setSuggestions([]);
    setAssistantInput("");
    setError("");
    setOperation("assistant");
    setAssistantProgress(progressMessage(text));
    try {
      const answer = await api("/api/assistant", {
        method: "POST",
        body: JSON.stringify({
          plan_key: plan.plan_key,
          message: text,
          conversation: chatMessages.slice(-28).map((turn) => ({
            ...turn,
            content: turn.content.slice(-4000),
          })),
        }),
      });
      setChatMessages((current) => [...current, { role: "assistant", content: answer.response }]);
      setSuggestions(followUpSuggestions(text, answer.response, answer));
      if (answer.output) {
        setResult((current) => ({
          ...current,
          output: answer.output,
          optimized_image: answer.optimized_image,
          alternative_image: answer.alternative_image || null,
          output_json: answer.output_json,
        }));
        setActiveTab("optimized");
      }
    } catch (reason) {
      setChatMessages((current) => [...current, {
        role: "assistant",
        content: `The request failed: ${reason.message}`,
      }]);
      setSuggestions(["Check whether this solution is valid", "Explain my current design"]);
    } finally {
      assistantRequestActive.current = false;
      setOperation("");
      setAssistantProgress("");
    }
  }

  const output = result?.output;
  const baseline = result?.baseline;
  const metrics = output?.reported_metrics;
  const solver = output?.solver || {};
  const shownImage = result
    ? activeTab === "baseline" && baseline ? result.baseline_image
      : activeTab === "assistantAlternative" && result.alternative_image
        ? result.alternative_image : result.optimized_image
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
              <input id="max-nodes" className="text-input" type="number" min="1" max="20"
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

            <section className="assistant-panel" aria-labelledby="assistant-heading">
              <h2 id="assistant-heading">Cable Ring Assistant</h2>
              <div className="chat-history" aria-live="polite" aria-relevant="additions text">
                {chatMessages.length === 0 && (
                  <>
                    <p className="assistant-intro">Hi! I’m your Cable Ring Assistant. I can explain your current layout,
                      ask the existing optimizer to improve it, compare designs, and help you understand constraints.
                      What would you like to explore?</p>
                    {!assistantAiAvailable && (
                      <p className="assistant-ai-notice">
                        Open-ended AI answers are not configured on this server. Supported design commands still work.
                      </p>
                    )}
                  </>
                )}
                {chatMessages.map((message, index) => (
                  <div className={`chat-message ${message.role}`} key={`${index}-${message.role}`}>
                    <p>{message.content}</p>
                  </div>
                ))}
                {operation === "assistant" && <p className="assistant-loading" role="status">{assistantProgress}</p>}
              </div>
              {suggestions.length > 0 && (
                <div className="assistant-suggestions" aria-label="Suggested questions">
                  {suggestions.map((suggestion) => (
                    <button key={suggestion} type="button" className="suggestion-button"
                      disabled={operation !== ""} onClick={() => sendAssistant(suggestion)}>
                      {suggestion}
                    </button>
                  ))}
                </div>
              )}
              <form className="assistant-form" onSubmit={(event) => {
                event.preventDefault();
                sendAssistant();
              }}>
                <textarea className="text-input" aria-label="Message the Cable Ring Assistant"
                  placeholder="Message the assistant…" value={assistantInput}
                  rows={2} disabled={operation !== ""}
                  onChange={(event) => setAssistantInput(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && !event.shiftKey) {
                      event.preventDefault();
                      event.currentTarget.form.requestSubmit();
                    }
                  }} />
                <button className="primary-button" type="submit"
                  disabled={!assistantInput.trim() || operation !== ""}>Send</button>
              </form>
            </section>

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
                  {result.alternative_image && <button role="tab"
                    aria-selected={activeTab === "assistantAlternative"}
                    className={activeTab === "assistantAlternative" ? "active" : ""}
                    onClick={() => setActiveTab("assistantAlternative")}>Compared design</button>}
                </div>
              </>
            )}

            {shownImage && <div className="plot-frame"><img src={shownImage} alt={`${plan.plan_id} floor plan`} /></div>}

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