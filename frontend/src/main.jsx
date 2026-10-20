import React, { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Activity, Bot, Cable, CircleAlert, CircleCheck, Download, Gauge, Layers,
  FileUp, FolderOpen, PanelLeftClose, PanelLeftOpen, Route, Settings2, Sparkles, Timer, Upload,
  Minimize2, Presentation,
} from "lucide-react";
import "./styles.css";
import { WhatIfPanel, defaultScenario, scenarioRequest } from "./WhatIf.jsx";

const INITIAL_SUGGESTIONS = [
  "Explain my current design",
  "Why did you use this many panels?",
  "Reduce cable length",
  "Find a design with fewer crossings",
  "Explain the optimization score",
  "Check whether a smaller panel count is feasible",
];

const DEFAULT_CAMERA_ZOOM = 0.5;

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

function fitPlotlyScene(frameWindow, directionOverride, zoomFactor = DEFAULT_CAMERA_ZOOM) {
  const graph = frameWindow.document.querySelector(".plotly-graph-div");
  const plotly = frameWindow.Plotly;
  const scene = graph?._fullLayout?.scene;
  const camera = graph?.layout?.scene?.camera;
  const renderer = scene?._scene?.glplot;
  const { bounds, cameraParams } = renderer || {};
  if (!graph || !plotly || !camera?.eye || !bounds || !cameraParams) return false;

  const { model, view, projection } = cameraParams;
  const fitView = directionOverride && graph.__cableRingDefaultView
    ? graph.__cableRingDefaultView
    : view;
  if (!model || !fitView || !projection) return false;
  const direction = directionOverride
    ? [...directionOverride]
    : [camera.eye.x, camera.eye.y, camera.eye.z];
  const eyeLength = Math.hypot(...direction);
  if (!Number.isFinite(eyeLength) || eyeLength === 0) return false;
  for (let i = 0; i < direction.length; i += 1) direction[i] /= eyeLength;

  const right = [fitView[0], fitView[4], fitView[8]];
  const up = [fitView[1], fitView[5], fitView[9]];
  const forward = [fitView[2], fitView[6], fitView[10]];
  const padding = 0.9;
  let fittedDistance = 0;
  for (let mask = 0; mask < 8; mask += 1) {
    const x = bounds[(mask >> 0) & 1][0];
    const y = bounds[(mask >> 1) & 1][1];
    const z = bounds[(mask >> 2) & 1][2];
    const point = [
      model[0] * x + model[4] * y + model[8] * z + model[12],
      model[1] * x + model[5] * y + model[9] * z + model[13],
      model[2] * x + model[6] * y + model[10] * z + model[14],
    ];
    const dot = (axis) => axis[0] * point[0] + axis[1] * point[1] + axis[2] * point[2];
    const depth = dot(forward);
    const screenX = dot(right) + fitView[12];
    const screenY = dot(up) + fitView[13];
    fittedDistance = Math.max(
      fittedDistance,
      depth + Math.max(
        Math.abs(screenX * projection[0]),
        Math.abs(screenY * projection[5]),
      ) / padding,
    );
  }
  if (!Number.isFinite(fittedDistance) || fittedDistance <= 0) return false;
  const scaleFactor = Number.isFinite(zoomFactor) && zoomFactor > 0 ? zoomFactor : 1;
  const fittedScale = fittedDistance * scaleFactor;
  if (!Number.isFinite(fittedScale) || fittedScale <= 0) return false;
  if (!graph.__cableRingDefaultDirection) {
    graph.__cableRingDefaultDirection = [...direction];
    graph.__cableRingDefaultCamera = JSON.parse(JSON.stringify(camera));
    graph.__cableRingDefaultView = [...view];
  }
  const fittedCamera = {
    ...(directionOverride ? graph.__cableRingDefaultCamera : camera),
    eye: {
      x: direction[0] * fittedScale,
      y: direction[1] * fittedScale,
      z: direction[2] * fittedScale,
    },
  };
  graph.__cableRingFitCamera = fittedCamera;
  graph.__cableRingFitDistance = fittedDistance;
  graph.__cableRingZoomFactor = scaleFactor;
  plotly.relayout(graph, { "scene.camera": fittedCamera });
  return true;
}

function updatePlotlyScene(frameWindow, hubs) {
  const graph = frameWindow.document.querySelector(".plotly-graph-div");
  const plotly = frameWindow.Plotly;
  if (!graph || !plotly || !fitPlotlyScene(frameWindow)) return false;
  if (!graph.__cableRingZoomTracking) {
    graph.__cableRingZoomTracking = true;
    graph.on("plotly_relayout", () => {
      frameWindow.requestAnimationFrame(() => {
        const currentDistance = graph._fullLayout?.scene?._scene?.glplot?.camera?.distance;
        const fittedDistance = graph.__cableRingFitDistance;
        if (currentDistance && fittedDistance) {
          graph.__cableRingZoomFactor = currentDistance / fittedDistance;
        }
      });
    });
  }
  const resetButton = frameWindow.document.querySelector(".camera-reset");
  if (resetButton && !resetButton.__cableRingFitReset) {
    resetButton.__cableRingFitReset = true;
    resetButton.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopImmediatePropagation();
      fitPlotlyScene(frameWindow, graph.__cableRingDefaultDirection, DEFAULT_CAMERA_ZOOM);
    }, true);
  }

  const routeIndices = [];
  const routeColors = [];
  const outlineIndices = [];
  const floorIndices = [];
  const floorColors = [];
  const legendIndices = [];
  const legendNames = [];

  graph.data.forEach((trace, index) => {
    const cableRoute = trace.mode?.includes("lines") && (
      trace.name?.includes(" ring · ") || (trace.line?.width === 15 && trace.opacity === 0.12)
    );
    if (cableRoute) {
      routeIndices.push(index);
      routeColors.push("#22D3EE");
      const hub = hubs.find((item) => trace.name?.startsWith(`${item.hub_id} ring · `));
      if (hub && Number.isFinite(hub.ring_length_m)) {
        const length = hub.ring_length_m;
        const precision = length === 0 ? 2 : Math.max(2, Math.min(12, Math.ceil(-Math.log10(Math.abs(length)))));
        legendIndices.push(index);
        legendNames.push(`${hub.hub_id} ring · ${length.toFixed(precision)} m`);
      }
    } else if (trace.mode?.includes("lines") && trace.line?.color === "#69d9f5") {
      outlineIndices.push(index);
    }

    if (trace.type === "mesh3d" && trace.name === "floor") {
      const adjusted = {
        "#087fba": "#287b9e",
        "#00a889": "#238b7d",
      }[trace.color];
      if (adjusted) {
        floorIndices.push(index);
        floorColors.push(adjusted);
      }
    }
  });

  if (routeIndices.length) plotly.restyle(graph, { "line.color": routeColors }, routeIndices);
  if (outlineIndices.length) plotly.restyle(graph, { "line.color": "#526579" }, outlineIndices);
  if (floorIndices.length) plotly.restyle(graph, { color: floorColors }, floorIndices);
  if (legendIndices.length) plotly.restyle(graph, { name: legendNames }, legendIndices);
  return true;
}

function Metric({ label, value, help, delta, deltaTone, deltaLabel, Icon, featured = false, valueTone = "" }) {
  return (
    <div className={`metric${featured ? " metric-featured" : ""}`} title={help || undefined}>
      <div className="metric-heading">
        <span className="metric-label">{label}</span>
        {Icon && <Icon size={16} strokeWidth={1.8} aria-hidden="true" />}
      </div>
      <div className={`metric-value ${valueTone}`}>{value}</div>
      {delta !== undefined && <div className={`metric-delta ${deltaTone}`}>{delta} <span>{deltaLabel}</span></div>}
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
  const sceneFrame = useRef(null);
  const appearanceMenu = useRef(null);
  const [activeTab, setActiveTab] = useState("optimized");
  const [displayMode, setDisplayMode] = useState("3d");
  const [theme, setTheme] = useState(() => localStorage.getItem("cable-ring-theme") || "night");
  const [accent, setAccent] = useState(() => localStorage.getItem("cable-ring-accent") || "cyan");
  const [compact, setCompact] = useState(() => localStorage.getItem("cable-ring-compact") === "true");
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => window.matchMedia("(max-width: 720px)").matches);
  const [presentationMode, setPresentationMode] = useState(false);
  const presentationLayout = useRef(null);
  const sceneOutputRef = useRef(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [readySceneUrl, setReadySceneUrl] = useState("");
  const [wallHeight, setWallHeight] = useState(1.5);
  const [wallTransparency, setWallTransparency] = useState(90);
  const [sceneSettings, setSceneSettings] = useState({ wallHeight: 1.5, wallTransparency: 90 });
  const [operation, setOperation] = useState("");
  const [error, setError] = useState("");
  const startupStartedRef = useRef(false);
  const startupRequestRef = useRef(0);
  const startupInteractedRef = useRef(false);
  const [assistantInput, setAssistantInput] = useState("");
  const [chatMessages, setChatMessages] = useState([]);
  const [assistantOpen, setAssistantOpen] = useState(false);
  const [suggestions, setSuggestions] = useState(INITIAL_SUGGESTIONS);
  const [assistantProgress, setAssistantProgress] = useState("");
  const assistantRequestActive = useRef(false);
  const [settings, setSettings] = useState({
    max_nodes: 5,
    node_mode: "optimize",
    zone_via_other_rooms: false,
    time_budget_s: 30,
    compare: true,
  });
  const [whatIfOptions, setWhatIfOptions] = useState(null);
  const [scenario, setScenario] = useState(defaultScenario());
  const [scenarioResult, setScenarioResult] = useState(null);
  const [scenarioVersion, setScenarioVersion] = useState(0);
  const sceneVariant = activeTab === "baseline"
    ? "&baseline=true"
    : activeTab === "whatif" && scenarioVersion > 0 ? `&scenario=true&v=${scenarioVersion}` : "";
  const sceneUrl = result && plan
    ? `/api/plans/${plan.plan_key}/3d?wall_height=${sceneSettings.wallHeight}&wall_transparency=${sceneSettings.wallTransparency}${sceneVariant}`
    : "";

  function resetWhatIf() {
    setWhatIfOptions(null);
    setScenarioResult(null);
    setScenario(defaultScenario());
    setScenarioVersion(0);
  }

  function resetViewerSettings() {
    setWallHeight(1.5);
    setWallTransparency(90);
    setSceneSettings({ wallHeight: 1.5, wallTransparency: 90 });
  }

  function interruptStartup() {
    if (!startupStartedRef.current || startupInteractedRef.current) return;
    startupInteractedRef.current = true;
    startupRequestRef.current += 1;
    setOperation((current) => current === "startup" ? "" : current);
  }

  useEffect(() => {
    if (activeTab !== "whatif" || !result || !plan || whatIfOptions) return;
    api(`/api/plans/${plan.plan_key}/whatif`)
      .then((options) => {
        setWhatIfOptions(options);
        setScenario(defaultScenario(options.official_rules));
      })
      .catch((reason) => setError(reason.message));
  }, [activeTab, result, plan, whatIfOptions]);

  async function runScenario() {
    setError("");
    setOperation("scenario");
    try {
      setScenarioResult(await api("/api/whatif", {
        method: "POST",
        body: JSON.stringify(scenarioRequest(plan.plan_key, scenario)),
      }));
      setScenarioVersion((v) => v + 1);
    } catch (reason) {
      setError(reason.message);
    } finally {
      setOperation("");
    }
  }

  function resetScenario() {
    setScenario(defaultScenario(whatIfOptions?.official_rules));
    setScenarioResult(null);
    setScenarioVersion(0);
  }

  useEffect(() => {
    if (startupStartedRef.current) return;
    startupStartedRef.current = true;
    const requestId = ++startupRequestRef.current;
    const isCurrent = () => requestId === startupRequestRef.current && !startupInteractedRef.current;
    setOperation("startup");
    (async () => {
      try {
        const status = await api("/api/status");
        if (!isCurrent()) return;
        setDatasetAvailable(status.dataset_available);
        setAssistantAiAvailable(status.assistant_ai_available);
        if (!status.dataset_available) throw new Error("The dataset is unavailable. You can still upload a plan JSON file.");
        const initialPlan = await api("/api/plans/index/0", { method: "POST" });
        if (!isCurrent()) return;
        setPlan(initialPlan);
        const solved = await api("/api/solve", {
          method: "POST",
          body: JSON.stringify({ plan_key: initialPlan.plan_key, ...settings }),
        });
        if (!isCurrent()) return;
        setResult(solved);
        setActiveTab("optimized");
        setDisplayMode("3d");
        resetWhatIf();
      } catch (reason) {
        if (isCurrent()) setError(reason.message);
      } finally {
        if (isCurrent()) setOperation("");
      }
    })();
  }, []);

  useEffect(() => {
    localStorage.setItem("cable-ring-theme", theme);
    localStorage.setItem("cable-ring-accent", accent);
    localStorage.setItem("cable-ring-compact", String(compact));
    document.documentElement.style.colorScheme = theme === "night" ? "dark" : "light";
  }, [theme, accent, compact]);

  useEffect(() => {
    if (!menuOpen) return undefined;
    function closeOnOutside(event) {
      if (!appearanceMenu.current?.contains(event.target)) setMenuOpen(false);
    }
    function closeOnEscape(event) {
      if (event.key === "Escape") setMenuOpen(false);
    }
    document.addEventListener("pointerdown", closeOnOutside);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutside);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [menuOpen]);

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
    let resizeObserver;
    let resizeTimer;
    let initialFitTimer;
    const timer = setInterval(() => {
      const frameWindow = sceneFrame.current?.contentWindow;
      if (frameWindow?.__cableRing3DReady && updatePlotlyScene(frameWindow, sceneOutputRef.current?.hubs || [])) {
        setReadySceneUrl(sceneUrl);
        clearInterval(timer);
        initialFitTimer = setTimeout(() => {
          if (sceneFrame.current?.contentWindow === frameWindow) {
            fitPlotlyScene(frameWindow, undefined, DEFAULT_CAMERA_ZOOM);
          }
        }, 150);
        const frame = sceneFrame.current;
        if (frame) {
          let previousSize = [frame.clientWidth, frame.clientHeight];
          resizeObserver = new ResizeObserver(() => {
            const size = [frame.clientWidth, frame.clientHeight];
            if (size[0] === previousSize[0] && size[1] === previousSize[1]) return;
            previousSize = size;
            clearTimeout(resizeTimer);
            resizeTimer = setTimeout(() => {
              const graph = frameWindow.document.querySelector(".plotly-graph-div");
              const currentDistance = graph?._fullLayout?.scene?._scene?.glplot?.camera?.distance;
              const fittedDistance = graph?.__cableRingFitDistance;
              const zoomFactor = graph?.__cableRingZoomFactor
                || (currentDistance && fittedDistance ? currentDistance / fittedDistance : 1);
              fitPlotlyScene(frameWindow, undefined, zoomFactor);
            }, 100);
          });
          resizeObserver.observe(frame);
        }
      }
    }, 100);
    return () => {
      clearInterval(timer);
      clearTimeout(initialFitTimer);
      resizeObserver?.disconnect();
      clearTimeout(resizeTimer);
    };
  }, [sceneUrl]);

  async function loadIndex(index) {
    interruptStartup();
    setPlanIndex(String(index));
    setPlan(null);
    setResult(null);
    setChatMessages([]);
    setAssistantInput("");
    setSuggestions(INITIAL_SUGGESTIONS);
    resetViewerSettings();
    resetWhatIf();
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
    interruptStartup();
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
    interruptStartup();
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
    interruptStartup();
    setSource("file");
    setPlan(null);
    setResult(null);
    setChatMessages([]);
    setAssistantInput("");
    setSuggestions(INITIAL_SUGGESTIONS);
    resetViewerSettings();
    resetWhatIf();
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
    interruptStartup();
    const index = Number(planIndex);
    if (source === "dataset" && (!Number.isInteger(index) || index < 0 || index > 16999)) {
      setError("Plan number must be between 0 and 16999.");
      return;
    }
    if (source === "file" && !plan) return;
    setError("");
    setOperation("solving");
    try {
      let activePlan = plan;
      const selectedPlanId = `PLAN_${String(index).padStart(5, "0")}`;
      if (source === "dataset" && activePlan?.plan_id !== selectedPlanId) {
        activePlan = await api(`/api/plans/index/${index}`, { method: "POST" });
        setPlan(activePlan);
        resetViewerSettings();
        setResult(null);
        setChatMessages([]);
        setAssistantInput("");
        setSuggestions(INITIAL_SUGGESTIONS);
        resetWhatIf();
      }
      if (!activePlan) return;
      const solved = await api("/api/solve", {
        method: "POST",
        body: JSON.stringify({ plan_key: activePlan.plan_key, ...settings }),
      });
      setResult(solved);
      resetWhatIf();
      setActiveTab("optimized");
      setDisplayMode("3d");
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
          view: activeTab === "baseline" ? "greedy_baseline" : "optimized",
          conversation: chatMessages.slice(-28).map((turn) => ({
            ...turn,
            content: turn.content.slice(-4000),
          })),
        }),
      });
      setChatMessages((current) => [...current, { role: "assistant", content: answer.response }]);
      setSuggestions(followUpSuggestions(text, answer.response, answer));
      if (answer.output && answer.changed) {
        setResult((current) => ({
          ...current,
          output: answer.output,
          optimized_image: answer.optimized_image,
          alternative_image: answer.alternative_image || null,
          output_json: answer.output_json,
        }));
        setActiveTab("optimized");
      } else if (answer.alternative_image) {
        setResult((current) => current && ({ ...current, alternative_image: answer.alternative_image }));
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
  const displayedOutput = activeTab === "baseline" && baseline ? baseline : output;
  const metrics = displayedOutput?.reported_metrics;
  const solver = displayedOutput?.solver || {};
  const comparisonMetrics = activeTab === "baseline"
    ? output?.reported_metrics
    : baseline?.reported_metrics;
  const costDelta = comparisonMetrics
    ? Number(metrics.cost_J - comparisonMetrics.cost_J).toFixed(2)
    : undefined;
  const displayedOutputJson = activeTab === "baseline" && baseline
    ? JSON.stringify(baseline, null, 2)
    : result?.output_json;
  const sceneOutput = activeTab === "baseline" && baseline ? baseline
    : activeTab === "whatif" && scenarioVersion > 0 ? scenarioResult?.output || output
      : displayedOutput;
  sceneOutputRef.current = sceneOutput;
  const shownImage = activeTab === "whatif" ? null : result
    ? activeTab === "baseline" && baseline ? result.baseline_image
      : activeTab === "assistantAlternative" && result.alternative_image
        ? result.alternative_image : result.optimized_image
    : plan?.image;

  function updateSetting(key, value) {
    interruptStartup();
    setSettings((current) => ({ ...current, [key]: value }));
  }

  function enterPresentationMode() {
    presentationLayout.current = { sidebarCollapsed, displayMode };
    setSidebarCollapsed(true);
    setDisplayMode("3d");
    setPresentationMode(true);
  }

  function exitPresentationMode() {
    const previous = presentationLayout.current;
    setPresentationMode(false);
    if (previous) {
      setSidebarCollapsed(previous.sidebarCollapsed);
      setDisplayMode(previous.displayMode);
    }
    presentationLayout.current = null;
  }

  return (
    <div className={`app-shell theme-${theme} accent-${accent}${compact ? " compact" : ""}${sidebarCollapsed ? " sidebar-collapsed" : ""}${presentationMode ? " presentation-mode" : ""}`}>
      <aside className="sidebar">
        <div className="sidebar-collapsed-rail" aria-label="Collapsed controls">
          <button className="rail-button rail-expand" type="button" aria-label="Expand controls"
            title="Expand controls" onClick={() => setSidebarCollapsed(false)}><PanelLeftOpen size={18} /></button>
          <button className="rail-button" type="button" aria-label="Floor plan selection" title="Floor plan selection"
            onClick={() => setSidebarCollapsed(false)}><FolderOpen size={17} /></button>
          <button className="rail-button" type="button" aria-label="Upload floor plan JSON" title="Upload floor plan JSON"
            onClick={() => { setSource("file"); setSidebarCollapsed(false); }}><FileUp size={17} /></button>
          <button className="rail-button rail-generate" type="button" aria-label="Generate design" title="Generate design"
            disabled={!plan || operation !== ""} onClick={generate}><Sparkles size={17} /></button>
          <button className="rail-button" type="button" aria-label="Advanced optimization settings" title="Advanced optimization settings"
            onClick={() => setSidebarCollapsed(false)}><Settings2 size={17} /></button>
        </div>
        <div className="sidebar-expanded-content">
        <div className="sidebar-brand">
          <button className="sidebar-brand-collapse" type="button" aria-label="Collapse controls"
            title="Collapse controls" onClick={() => setSidebarCollapsed(true)}><PanelLeftClose size={17} /></button>
        </div>
        <section>
          <h2><span>01</span> Floor plan</h2>
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
                value={planIndex} disabled={!datasetAvailable || operation === "loading" || operation === "solving"}
                onChange={(event) => {
                  interruptStartup();
                  setPlanIndex(event.target.value);
                }}
                onBlur={(event) => {
                  const value = Number(planIndex);
                  if (Number.isInteger(value) && value >= 0 && value <= 16999) {
                    if (event.relatedTarget?.id !== "generate-button") loadIndex(value);
                  }
                  else setError("Plan number must be between 0 and 16999.");
                }}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    event.preventDefault();
                    generate();
                  }
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
          <h2><span>02</span> Optimization</h2>
          <button id="generate-button" className="primary-button full-width" type="button" onClick={generate}
            disabled={operation !== "" || (source === "file" && !plan) || (source === "dataset" && !datasetAvailable)}>
            {operation === "startup" ? <><Activity size={16} className="spin-icon" /> Optimizing Plan 0 ...</>
              : operation === "solving" ? <><Activity size={16} className="spin-icon" /> Optimizing ...</>
                : <><Sparkles size={16} /> Generate design</>}
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

              <label className="field-label" htmlFor="time-budget"><Timer size={14} /> Time budget per plan (s)</label>
              <div className="range-line">
                <input id="time-budget" type="range" min="5" max="120" step="1"
                  value={settings.time_budget_s}
                  onChange={(event) => updateSetting("time_budget_s", Number(event.target.value))} />
                <output>{settings.time_budget_s}</output>
              </div>

              <label className="check-row">
                <input type="checkbox" checked={settings.compare}
                  onChange={(event) => updateSetting("compare", event.target.checked)} />
                <span>Also run the greedy baseline (for comparison)</span>
              </label>
            </div>
          </details>
        </section>
        {error && <div className="notice error-notice" role="alert">{error}</div>}
        {operation === "loading" && <p className="muted">Loading plan ...</p>}
        </div>
      </aside>

      <main className="main-content">
        <header className="page-heading">
          <div className="topbar-leading">
            <button className="icon-button sidebar-toggle" type="button" aria-label={sidebarCollapsed ? "Expand controls" : "Collapse controls"}
              title={sidebarCollapsed ? "Expand controls" : "Collapse controls"}
              onClick={() => setSidebarCollapsed((collapsed) => !collapsed)}>
              {sidebarCollapsed ? <PanelLeftOpen size={18} /> : <PanelLeftClose size={18} />}
            </button>
            <div className="topbar-brand-mark"><Cable size={19} strokeWidth={2} aria-hidden="true" /></div>
            <div className="topbar-titles">
              <h1>Cable Ring Optimizer</h1>
              <p>Intelligent Cable Network Design</p>
            </div>
          </div>
          <div className="topbar-actions">
            <div className="topbar-plan"><span>ACTIVE PLAN</span><strong>{plan?.plan_id || "No plan loaded"}</strong></div>
            <span className={`status-pill ${metrics ? (metrics.valid ? "status-valid" : "status-invalid") : "status-idle"}`}>
              {metrics ? (metrics.valid ? <CircleCheck size={14} /> : <CircleAlert size={14} />) : <span className="status-dot" />}
              {metrics ? (metrics.valid ? "Valid solution" : "Needs review") : "Awaiting solve"}
            </span>
            {displayedOutputJson && (
              <a className="icon-button top-export" href={`data:application/json;charset=utf-8,${encodeURIComponent(displayedOutputJson)}`}
                download={`${plan.plan_id}_output.json`} aria-label="Export JSON" title="Export JSON">
                <Download size={17} />
              </a>
            )}
            {plan && <button className="copilot-button" type="button" onClick={() => setAssistantOpen(true)}><Bot size={16} /> AI Copilot</button>}
            {presentationMode && <button className="presentation-exit" type="button" onClick={exitPresentationMode}>
              <Minimize2 size={15} /> Exit presentation
            </button>}
          <div className="appearance-menu" ref={appearanceMenu}>
            <button className="menu-trigger" type="button" aria-label="Appearance settings"
              aria-expanded={menuOpen} title="Appearance settings" onClick={() => setMenuOpen((open) => !open)}>
              <span aria-hidden="true">⋮</span>
            </button>
            {menuOpen && (
              <div className="appearance-popover" role="dialog" aria-label="Appearance settings">
                <div className="popover-title">Appearance</div>
                <div className="preference-label">Theme</div>
                <div className="segmented-control" role="group" aria-label="Theme">
                  <button type="button" aria-pressed={theme === "night"}
                    className={theme === "night" ? "selected" : ""} onClick={() => setTheme("night")}>Night</button>
                  <button type="button" aria-pressed={theme === "bright"}
                    className={theme === "bright" ? "selected" : ""} onClick={() => setTheme("bright")}>Bright</button>
                </div>
                <div className="preference-label accent-label">Accent</div>
                <div className="accent-options" role="group" aria-label="Accent color">
                  <button className="accent-choice coral" type="button" aria-label="Coral accent"
                    aria-pressed={accent === "coral"} title="Coral" onClick={() => setAccent("coral")} />
                  <button className="accent-choice cyan" type="button" aria-label="Cyan accent"
                    aria-pressed={accent === "cyan"} title="Cyan" onClick={() => setAccent("cyan")} />
                  <button className="accent-choice lime" type="button" aria-label="Lime accent"
                    aria-pressed={accent === "lime"} title="Lime" onClick={() => setAccent("lime")} />
                </div>
                <label className="compact-option">
                  <input type="checkbox" checked={compact} onChange={(event) => setCompact(event.target.checked)} />
                  <span>Compact layout</span>
                </label>
              </div>
            )}
          </div>
          </div>
        </header>

        {!plan ? (
          error ? (
            <div className="startup-error" role="alert">
              <CircleAlert size={18} />
              <span>{error}</span>
              <button className="secondary-button" type="button" onClick={generate}>Retry Plan 0</button>
            </div>
          ) : (
            <div className="notice info-notice">
              Upload a plan JSON file in the sidebar (e.g. inputs/PLAN_00042.json), or switch to picking a plan from the dataset.
            </div>
          )
        ) : (
          <>
            <section className="plan-summary">
              <div className="plan-summary-copy">
                <span className="eyebrow">FLOOR PLAN</span>
                <strong>{plan.plan_id}</strong>
                <span>{plan.room_count} rooms · {plan.eligible_count} network endpoints · {plan.eligible_area_m2.toFixed(1)} m² eligible</span>
              </div>
              <div className="hero-quick-metrics">
                <div><span>HUBS</span><strong>{metrics?.hub_count ?? "—"}</strong></div>
                <div><span>COST J</span><strong>{metrics?.cost_J ?? "—"}</strong></div>
                <div className="hero-objective"><Gauge size={14} /><span>Fewest hubs, then J = L + 5X + O + 0.5B</span></div>
              </div>
            </section>

            {operation === "startup" && (
              <div className="startup-loading" role="status">
                <Activity size={22} className="spin-icon" />
                <div><strong>Optimizing Plan 0</strong><span>Building the validated digital twin with the configured optimizer.</span></div>
              </div>
            )}
            {error && !result && (
              <div className="startup-error" role="alert">
                <CircleAlert size={18} />
                <span>{error}</span>
                <button className="secondary-button" type="button" onClick={generate}>Retry optimization</button>
              </div>
            )}

            {metrics && (
              <div className="hero-navigation">
                <div className="tabs" role="tablist" aria-label="Solution view">
                  <button role="tab" aria-selected={activeTab === "optimized"} className={activeTab === "optimized" ? "active" : ""}
                    onClick={() => setActiveTab("optimized")}>Optimised</button>
                  {baseline && <button role="tab" aria-selected={activeTab === "baseline"} className={activeTab === "baseline" ? "active" : ""}
                    onClick={() => setActiveTab("baseline")}
                    title="Greedy baseline: cost-guided region growing, multi-start nearest neighbour, bend-aware shortest paths">
                    Greedy baseline</button>}
                  {result.alternative_image && <button role="tab"
                    aria-selected={activeTab === "assistantAlternative"}
                    className={activeTab === "assistantAlternative" ? "active" : ""}
                    onClick={() => setActiveTab("assistantAlternative")}>Compared design</button>}
                  <button role="tab" aria-selected={activeTab === "whatif"} className={activeTab === "whatif" ? "active" : ""}
                    onClick={() => setActiveTab("whatif")}>What-if simulator</button>
                </div>
                <div className="view-switch" role="tablist" aria-label="Visualization">
                  <button role="tab" aria-selected={displayMode === "3d"} className={displayMode === "3d" ? "active" : ""}
                    onClick={() => setDisplayMode("3d")}>3D Twin</button>
                  <button role="tab" aria-selected={displayMode === "2d"} className={displayMode === "2d" ? "active" : ""}
                    onClick={() => setDisplayMode("2d")}>2D Plan</button>
                </div>
                {result && <button className="presentation-button" type="button" onClick={enterPresentationMode}>
                  <Presentation size={15} /> Present
                </button>}
              </div>
            )}

            {result && sceneUrl && (
              <div className={`scene-stage ${displayMode === "3d" ? "active" : "preload"}`}>
                {displayMode === "3d" && readySceneUrl !== sceneUrl && (
                  <div className="scene-loading" role="status">Preparing 3D view ...</div>
                )}
                <iframe ref={sceneFrame} key={sceneUrl} className="plot-3d" title="Interactive 3D floor plan"
                  src={sceneUrl}
                  aria-hidden={displayMode !== "3d"} tabIndex={displayMode === "3d" ? 0 : -1} />
                {displayMode === "3d" && (
                  <div className="scene-floating-controls" aria-label="3D building controls">
                    <div className="floating-control-heading"><Settings2 size={14} /> BUILDING</div>
                    <label className="floating-slider" htmlFor="wall-height">
                      <span>Wall height <output>{wallHeight.toFixed(1)} m</output></span>
                      <input id="wall-height" type="range" min="1.5" max="4" step="0.1"
                        value={wallHeight} onChange={(event) => setWallHeight(Number(event.target.value))} />
                    </label>
                    <label className="floating-slider" htmlFor="wall-transparency">
                      <span>Wall transparency <output>{wallTransparency}%</output></span>
                      <input id="wall-transparency" type="range" min="0" max="100" step="5"
                        value={wallTransparency} onChange={(event) => setWallTransparency(Number(event.target.value))} />
                    </label>
                  </div>
                )}
                {presentationMode && (
                  <div className="presentation-overlay" aria-label="Presentation metrics">
                    <strong>{plan.plan_id}</strong>
                    <span>{metrics?.hub_count ?? "—"} hubs</span>
                    <span>J {metrics?.cost_J ?? "—"}</span>
                    <span className={metrics?.valid ? "presentation-valid" : "presentation-invalid"}>
                      {metrics ? (metrics.valid ? "Valid" : "Needs review") : "Optimizing"}
                    </span>
                  </div>
                )}
              </div>
            )}
            {operation !== "startup" && ((displayMode === "2d") || !result) && shownImage &&
              <div className="plot-frame"><img src={shownImage} alt={`${plan.plan_id} floor plan`} /></div>}

            {metrics && (
              <section className="secondary-analytics" aria-label="Solution metrics and comparison">
                <div className="secondary-section-heading">
                  <div><span className="eyebrow">SOLUTION READOUT</span><h2>Performance metrics</h2></div>
                  <span className={`status-pill ${metrics.valid ? "status-valid" : "status-invalid"}`}>
                    {metrics.valid ? <CircleCheck size={14} /> : <CircleAlert size={14} />}
                    {metrics.valid ? "Validated" : "Invalid"}
                  </span>
                </div>
                <div className="metrics-grid">
                  <Metric label="Validity" value={metrics.valid ? "Valid" : "Invalid"}
                    help="Whether this design passes the independent hard-rule validator."
                    valueTone={metrics.valid ? "value-good" : "value-bad"} Icon={metrics.valid ? CircleCheck : CircleAlert} />
                  <Metric label="Hubs" value={metrics.hub_count} help={`Installed hubs. Feasible lower bound: ${solver.hub_lower_bound ?? "not reported"}.`}
                    Icon={Layers} featured />
                  <Metric label="Cost J" value={metrics.cost_J} delta={costDelta}
                    deltaTone={activeTab === "baseline" ? "delta-bad" : "delta-good"}
                    deltaLabel={activeTab === "baseline" ? "vs optimized" : "vs baseline"}
                    help="Official objective after minimizing hub count: J = L + 5X + O + 0.5B."
                    Icon={Gauge} featured />
                  <Metric label="Cable length" value={`${metrics.total_length_m} m`} help="Total cable length L in metres." Icon={Cable} />
                  <Metric label="Crossings" value={metrics.crossings} help="Number of cable crossings X." Icon={Activity} />
                  <Metric label="Overlap" value={`${metrics.overlap_m} m`} help="Cable overlap O in metres." Icon={Layers} />
                  <Metric label="Bends" value={metrics.bends} help="Number of cable bends B." Icon={Route} />
                </div>
                {baseline && output && (
                  <section className="baseline-strip" aria-label="Optimized design compared with greedy baseline">
                    <div className="baseline-priority">
                      <span className="eyebrow">DESIGN COMPARISON</span>
                      <strong>{output.reported_metrics.hub_count <= baseline.reported_metrics.hub_count
                        ? "Hub priority satisfied" : "Baseline uses fewer hubs"}</strong>
                    </div>
                    <div className="baseline-reading">
                      <span>Hubs</span>
                      <strong>{baseline.reported_metrics.hub_count} <i>→</i> {output.reported_metrics.hub_count}</strong>
                      <small>Greedy → optimized</small>
                    </div>
                    <div className="baseline-reading">
                      <span>Cost improvement</span>
                      <strong className={output.reported_metrics.cost_J <= baseline.reported_metrics.cost_J ? "delta-good" : "delta-bad"}>
                        {(baseline.reported_metrics.cost_J - output.reported_metrics.cost_J).toFixed(2)} J
                      </strong>
                      <small>Greedy baseline → optimized</small>
                    </div>
                    <div className="baseline-reading baseline-length">
                      <span>Cable saved</span>
                      <strong>{(baseline.reported_metrics.total_length_m - output.reported_metrics.total_length_m).toFixed(2)} m</strong>
                      <small>Greedy baseline → optimized</small>
                    </div>
                    {[
                      ["Crossings X", "crossings", ""],
                      ["Overlap O", "overlap_m", " m"],
                      ["Bends B", "bends", ""],
                    ].map(([label, key, unit]) => {
                      const difference = baseline.reported_metrics[key] - output.reported_metrics[key];
                      const tone = difference > 0 ? "delta-good" : difference < 0 ? "delta-bad" : "delta-same";
                      return (
                        <div className="baseline-reading baseline-extra" key={key}>
                          <span>{label}</span>
                          <strong className={tone}>{`${difference > 0 ? "+" : ""}${Number(difference.toFixed(2))}${unit}`}</strong>
                          <small>Greedy baseline → optimized</small>
                        </div>
                      );
                    })}
                  </section>
                )}
              </section>
            )}

            {activeTab === "whatif" && (
              <WhatIfPanel options={whatIfOptions} planKey={plan.plan_key} official={output}
                scenario={scenario} setScenario={setScenario}
                onRun={runScenario} onReset={resetScenario} busy={operation === "scenario"}
                scenarioResult={scenarioResult} displayMode={displayMode}
                onDragScenario={() => setScenarioVersion((v) => v + 1)}
                onDragReset={() => setScenarioVersion(0)} />
            )}
            {activeTab === "whatif" && displayMode === "3d" && (
              <p className="help-text">{scenarioVersion > 0
                ? "3D view of your what-if change (latest drag or scenario)."
                : "3D view of the official solution. Drag a device or run a scenario to see it here."}</p>
            )}

            {displayedOutput && (
              <>
                {(displayedOutput.validation?.violations || []).map((violation, index) => (
                  <div className="notice error-notice validation-notice" key={`${violation.rule}-${index}`}>
                    {violation.rule}: {violation.message}
                  </div>
                ))}
                {(displayedOutput.validation?.warnings || []).map((warning, index) => (
                  <div className="notice warning-notice validation-notice" key={index}>{warning}</div>
                ))}

                <details className="output-details">
                  <summary>Hubs</summary>
                  <div className="table-wrap">
                    <table>
                      <thead><tr><th>hub</th><th>room</th><th>rooms</th><th>nodes</th><th>area m²</th><th>sequence</th><th>ring m</th></tr></thead>
                      <tbody>{displayedOutput.hubs.map((hub) => (
                        <tr key={hub.hub_id}>
                          <td>{hub.hub_id}</td><td>{hub.room_id}</td><td>{hub.assigned_rooms.join(", ")}</td><td>{hub.assigned_nodes.length}</td>
                          <td>{hub.assigned_area_m2}</td><td>{hub.closed_sequence.join(" → ")}</td><td>{hub.ring_length_m}</td>
                        </tr>
                      ))}</tbody>
                    </table>
                  </div>
                </details>
                <details className="output-details">
                  <summary>Solver details</summary>
                  <div className="solver-summary">
                    <div><span>Engine</span><strong>{solver.mode || "Not reported"}</strong></div>
                    <div><span>Hub lower bound</span><strong>{solver.hub_lower_bound ?? "Not reported"}</strong></div>
                    <div><span>Designs evaluated</span><strong>{solver.designs_evaluated ?? "Not reported"}</strong></div>
                    <div><span>Runtime</span><strong>{solver.time_s !== undefined ? `${solver.time_s} s` : "Not reported"}</strong></div>
                    {!!solver.dropped_rooms?.length && <div><span>Dropped rooms</span><strong>{solver.dropped_rooms.join(", ")}</strong></div>}
                  </div>
                  <details className="raw-solver-details">
                    <summary>Raw solver payload</summary>
                    <pre>{JSON.stringify(solver, null, 2)}</pre>
                  </details>
                </details>

                <section className="download-section">
                  <h2>Output file</h2>
                  <p>This JSON is the submission file for this plan: hubs, nodes, visiting order and every cable polyline.</p>
                  <a className="primary-button download-button" href={`data:application/json;charset=utf-8,${encodeURIComponent(displayedOutputJson)}`}
                    download={`${plan.plan_id}_output.json`}>Download output JSON</a>
                </section>
              </>
            )}
          </>
        )}
      </main>
      {plan && assistantOpen && (
        <section className="assistant-panel assistant-drawer" id="assistant-drawer"
          role="dialog" aria-labelledby="assistant-heading">
          <header className="assistant-drawer-header">
            <div className="assistant-brand">
              <span className="assistant-brand-mark"><Bot size={19} aria-hidden="true" /></span>
              <h2 id="assistant-heading">Cable Ring Assistant</h2>
            </div>
            <button className="assistant-minimize" type="button" aria-label="Minimize assistant"
              onClick={() => setAssistantOpen(false)}>Minimize</button>
          </header>
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
      )}
    </div>
  );
}

createRoot(document.getElementById("root")).render(<App />);