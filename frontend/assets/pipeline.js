// Pipeline console: everything comes from GET /pipeline (read-only).
// Five summary cards on top; details in five tabs: Workflows, Pipeline, Live data, Model,
// Monitoring.

const REFRESH_MS = 60 * 1000;
const STATUS = {
  "up to date": { cls: "ok", label: "Up to date", fill: "--ok-bg", stroke: "--ok" },
  changed: { cls: "attention", label: "Inputs changed, needs a re-run", fill: "--warn-bg", stroke: "--warn" },
  "runs every time": { cls: "neutral", label: "Runs on every pipeline run", fill: "--neutral-bg", stroke: "--neutral" },
  "waits for retrain": { cls: "neutral", label: "New live data, used at the next retrain", fill: "--neutral-bg", stroke: "--neutral" },
  unknown: { cls: "neutral", label: "Status unknown", fill: "--raised", stroke: "--line-strong" },
};
const WORD = { ok: "OK", attention: "Needs attention", bad: "Problem", neutral: "No data" };
const TABS = ["workflows", "pipeline", "live", "model", "monitoring"];
const page = { data: null, selected: null, chart: null, tabs: null, errChart: null, grafana: false };

const statusOf = (s) => STATUS[s] || STATUS.unknown;
const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);

// ---------- layout: cards on top, tabs below ----------

function buildPage() {
  const panel = (...children) => el("div", {}, ...children);
  const workflows = panel(
    el("p", { class: "hint" }, "Airflow runs the pipeline on a schedule. Each row shows a workflow's latest run and when it runs next; the link opens it in Airflow with the full graph and logs."),
    el("div", { class: "table-wrap" }, el("table", { class: "data stack", id: "workflows" })),
    el("h3", { class: "sub-h" }, "Recent warnings and errors"),
    el("div", { id: "problems" }),
  );
  const pipeline = panel(
    el("p", { class: "hint graph-hint" }, "Each box is one step of the DVC pipeline; arrows show which step feeds which. Select a step to see its latest report."),
    el("div", { class: "legend", id: "legend" }),
    el("div", { class: "pipe-grid" },
      el("div", { class: "dag-wrap" }, svgEl("svg", { id: "dag", role: "img", "aria-label": "Pipeline graph" })),
      el("aside", { id: "stage-detail", class: "stage-detail", "aria-live": "polite" })),
    el("h3", { class: "sub-h" }, "All steps"),
    el("div", { class: "table-wrap" }, el("table", { class: "data stack", id: "stages" })),
    el("h3", { class: "sub-h" }, "Processing speed"),
    el("p", { class: "hint" }, "Rows handled per second by each step in its latest run (log scale)."),
    el("div", { class: "chart-box short" }, el("canvas", { id: "speed", role: "img", "aria-label": "Rows per second for each pipeline step" })),
  );
  const live = panel(
    el("p", { class: "hint" }, "The collector saves a snapshot of the CPCB feed every 30 minutes. Each box below is one half-hour of the last 24 hours."),
    el("div", { class: "big-stats", id: "live-stats" }),
    el("div", { class: "slots-days", id: "live-days", "aria-hidden": "true" }),
    el("div", { class: "slots", id: "live-strip", role: "img" }),
    el("div", { class: "slots-axis", id: "live-axis", "aria-hidden": "true" }),
    el("div", { class: "key" },
      el("span", {}, el("i", { style: "background:var(--ok)" }), "Collected"),
      el("span", {}, el("i", { style: "background:color-mix(in srgb, var(--bad) 30%, var(--paper))" }), "Missing"),
      el("span", {}, el("i", { style: "background:var(--raised);box-shadow:inset 0 0 0 1px var(--line-strong)" }), "Not due yet")),
    el("h3", { class: "sub-h" }, "Gaps in the last 24 hours"),
    el("ul", { class: "plain", id: "gaps" }),
  );
  const model = panel(el("div", { class: "model-grid" },
    el("div", {},
      el("h3", {}, "Live model"),
      el("p", { class: "hint" }, "“No change” means assuming the 24-hour average stays as it is now; the model must beat it."),
      el("div", { class: "big-stats", id: "model-stats" }),
      el("dl", { class: "kv", id: "model-kv" })),
    el("div", {},
      el("h3", {}, "Quality gate"),
      el("p", { class: "hint", id: "gate-line" }),
      el("ul", { class: "checks", id: "checks" }))));
  const monitoring = panel(
    el("p", { class: "hint" }, "Is the service healthy and is the model still good? Alerts come from Prometheus and Alertmanager; the forecast check and the drift check are run by the hourly monitor workflow."),
    el("h3", { class: "sub-h first" }, "Alerts"),
    el("div", { id: "alerts-now" }),
    el("details", { class: "resolved", id: "alerts-past-wrap" },
      el("summary", {}, "Recently resolved"), el("ul", { class: "plain", id: "alerts-past" })),
    el("h3", { class: "sub-h" }, "Forecasts vs what happened"),
    el("p", { class: "hint", id: "eval-line" }),
    el("div", { class: "big-stats", id: "eval-stats" }),
    el("div", { class: "mon-grid" },
      el("div", {},
        el("p", { class: "hint" }, "Average miss per day, in µg/m³: the model against “no change” (assume the 24-hour average stays as it is)."),
        el("div", { class: "chart-box short" }, el("canvas", { id: "err-chart", role: "img", "aria-label": "Daily forecast error of the model and of no change" }))),
      el("div", {},
        el("p", { class: "hint" }, "By how far ahead the forecast was made (last days)."),
        el("div", { class: "table-wrap" }, el("table", { class: "data", id: "eval-horizon" })))),
    el("h3", { class: "sub-h" }, "Do the live inputs still look like the training data?"),
    el("p", { class: "hint", id: "drift-line" }),
    el("div", { class: "drift", id: "drift-bars" }),
    el("div", { class: "key", id: "drift-key" }),
    el("h3", { class: "sub-h" }, "Automatic retraining"),
    el("dl", { class: "kv", id: "retrain-kv" }),
    el("h3", { class: "sub-h" }, "Live panels from Grafana"),
    el("div", { id: "grafana" }),
    el("div", { class: "tool-links", id: "tool-links" }),
  );

  const first = TABS.includes(location.hash.slice(1)) ? location.hash.slice(1) : "workflows";
  page.tabs = makeTabs("Pipeline details", [
    { id: "workflows", label: "Workflows", panel: workflows },
    { id: "pipeline", label: "Pipeline", panel: pipeline },
    { id: "live", label: "Live data", panel: live },
    { id: "model", label: "Model", panel: model },
    { id: "monitoring", label: "Monitoring", panel: monitoring },
  ], first, (id) => {
    showTab(id);
    history.replaceState(null, "", `#${id}`);
    document.querySelectorAll(".status").forEach((c) => c.classList.toggle("current", c.dataset.tab === id));
    if (id === "pipeline" && page.data) renderSpeed(page.data);  // canvas needs a visible size
    if (id === "monitoring" && page.data) { renderErrChart(page.data); renderGrafana(page.data); }
  });
  fill(document.getElementById("console-tabs"), page.tabs);
  showTab(first);
}

// On a phone the five tabs scroll sideways: keep the selected one in view
// (without moving the page up or down).
function showTab(id) {
  const tab = document.getElementById(`tab-${id}`);
  if (!tab) return;
  const bar = tab.parentElement;
  const t = tab.getBoundingClientRect(), b = bar.getBoundingClientRect();
  if (t.right > b.right) bar.scrollLeft += t.right - b.right + 4;
  else if (t.left < b.left) bar.scrollLeft -= b.left - t.left + 4;
}

function setBadge(tab, text) {
  const b = document.getElementById(`tab-${tab}`);
  let badge = b.querySelector(".tab-badge");
  if (!text) { if (badge) badge.remove(); return; }
  if (!badge) { badge = el("span", { class: "tab-badge" }); b.append(badge); }
  badge.textContent = text;
}

function openTab(id) {
  page.tabs.selectTab(id);
  document.getElementById("console-tabs").scrollIntoView({ behavior: "smooth", block: "start" });
}

// ---------- summary cards ----------

function card(tab, cls, title, line, detail) {
  return el("button", { type: "button", class: `status ${cls}`, "data-tab": tab, onclick: () => openTab(tab) },
    el("div", { class: "status-top" }, el("h3", {}, title), el("span", { class: `pill ${cls}` }, WORD[cls])),
    el("p", { class: "big-line" }, line),
    el("p", { class: "detail-line" }, detail));
}

function workflowState(d) {
  const runs = d.workflows || [];
  if (!runs.length) return { cls: "neutral", line: "No runs recorded", detail: "Start Airflow with: bash airflow/start.sh" };
  const failed = runs.filter((w) => w.state === "failed");
  const live = runs.find((w) => w.dag_id === "airsense_live");
  const stale = live && live.minutes_ago != null && live.minutes_ago > WORKFLOWS.airsense_live.staleMin;
  if (failed.length) return { cls: "bad", line: `${failed.length} of ${runs.length} failed`, detail: `Failed: ${failed.map((w) => w.dag_id).join(", ")}` };
  if (stale) return { cls: "attention", line: "Airflow may be stopped", detail: `No live-data run since ${fmtWhenAgo(live.end_utc)}` };
  if (live && live.state === "source_empty") {
    return { cls: "attention", line: "CPCB feed empty", detail: `The latest live run, ${fmtWhenAgo(live.end_utc)}, found no data at the source; the pipeline itself is fine` };
  }
  return {
    cls: "ok", line: `All ${runs.length} succeeded`,
    detail: live ? `Latest live-data run ${fmtWhenAgo(live.end_utc)}` : `${runs.length} workflows recorded`,
  };
}

function renderSummary(d) {
  const w = workflowState(d);
  const changed = d.stages.filter((s) => s.status === "changed").map((s) => s.name);
  const always = d.stages.filter((s) => s.status === "runs every time").length;
  const waiting = d.stages.filter((s) => s.status === "waits for retrain").length;
  const pipe = d.dvc_status_error
    ? card("pipeline", "bad", "Pipeline steps", "Status unavailable", d.dvc_status_error)
    : card("pipeline", changed.length ? "attention" : "ok", "Pipeline steps",
        changed.length ? `${changed.length} of ${d.stages.length} need a re-run` : `${d.stages.length - always - waiting} up to date`,
        changed.length ? `Changed inputs: ${changed.join(", ")}`
          : [`${always} more re-run every time by design`, waiting ? `${waiting} waits for the next retrain` : null].filter(Boolean).join("; "));

  const c = d.collection;
  const age = c.minutes_since_last;
  const liveCls = age == null || age > 180 ? "bad" : age > 45 || c.gaps_24h.length ? "attention" : "ok";
  const live = card("live", liveCls, "Live data collection",
    age == null ? "No snapshots yet" : `${c.snapshots_24h} of ${c.expected_24h} collected`,
    age == null ? "Nothing collected so far" : `Last snapshot ${fmtWhenAgo(c.last_snapshot_utc)}`);

  const m = d.champion || {};
  const g = d.gate;
  const passed = g ? g.checks.filter((x) => x.passed).length : 0;
  const model = card("model", !m.version ? "bad" : g && !g.passed ? "attention" : "ok", "Live model",
    m.version ? `${m.name}, version ${m.version}` : "No champion model",
    [m.test_mae != null ? `Test error ${m.test_mae.toFixed(2)} µg/m³` : null,
      g ? `${passed} of ${g.checks.length} gate checks passed` : null].filter(Boolean).join("; "));

  const mon = monitoringState(d);
  fill(document.getElementById("status-row"),
    card("workflows", w.cls, "Scheduled workflows", w.line, w.detail), pipe, live, model,
    card("monitoring", mon.cls, "Monitoring", mon.line, mon.detail));
  const current = location.hash.slice(1) || "workflows";
  document.querySelectorAll(".status").forEach((x) => x.classList.toggle("current", x.dataset.tab === current));
  setBadge("workflows", w.cls === "ok" || w.cls === "neutral" ? "" : "!");
  setBadge("pipeline", changed.length ? String(changed.length) : "");
  setBadge("live", c.gaps_24h.length ? String(c.gaps_24h.length) : "");  // number of gaps
  setBadge("monitoring", mon.badge);
  showTab(current);  // a new badge can widen the selected tab
}

// ---------- workflows (Airflow) ----------

// A table cell with a label for the stacked phone layout; content kept in one block.
function cell(label, attrs, ...children) {
  return el("td", { "data-label": label, ...attrs }, el("div", {}, ...children));
}

const WORKFLOWS = {
  airsense_live: { what: "Collect live data, process, validate, refresh the live features", when: "Every 30 min, at :05 and :35", staleMin: 45, times: () => Array.from({ length: 48 }, (_, i) => [Math.floor(i / 2), i % 2 ? 35 : 5]) },
  airsense_retrain: { what: "Retrain, quality gate, register; serve a new champion", when: "Every Sunday at 13:00 IST, or started manually", staleMin: null, times: () => [[13, 0]], weekday: 0 },
  airsense_housekeeping: { what: "Version the live data and saved forecasts, daily Git checkpoint, clean-up", when: "Daily at 23:15 IST", staleMin: null, times: () => [[23, 15]] },
  airsense_monitor: { what: "Save every city's forecast; compare earlier forecasts with what was measured; daily drift check; start a retrain if needed", when: "Every hour at :50", staleMin: 75, times: () => Array.from({ length: 24 }, (_, h) => [h, 50]) },
};

// The next scheduled start (schedules are in IST, UTC+5:30, no daylight saving).
function nextRun(meta, now = Date.now()) {
  const OFF = 330 * 60000;
  const wall = new Date(now + OFF);
  for (let day = 0; day <= 7; day++) {
    const base = Date.UTC(wall.getUTCFullYear(), wall.getUTCMonth(), wall.getUTCDate() + day);
    if (meta.weekday != null && new Date(base).getUTCDay() !== meta.weekday) continue;
    for (const [h, m] of meta.times()) {
      const t = base + (h * 60 + m) * 60000 - OFF;
      if (t > now) return new Date(t);
    }
  }
  return null;
}

const RUN_STATE = {
  success: ["ok", "Succeeded"], failed: ["bad", "Failed"], running: ["attention", "Running"],
  source_empty: ["attention", "Source empty"],  // the CPCB feed had no data; not a pipeline fault
};

function renderWorkflows(d) {
  const table = document.getElementById("workflows");
  const runs = d.workflows || [];
  if (!runs.length) {
    fill(table, el("caption", { class: "hint" }, "No Airflow runs recorded yet. Start Airflow with: bash airflow/start.sh"));
    return;
  }
  const cols = ["Workflow", "Schedule", "Latest run", "Next run", "Took", "Result", ""];
  fill(table, 
    el("thead", {}, el("tr", {}, cols.map((h) => el("th", { scope: "col" }, h)))),
    el("tbody", {}, ...runs.map((w) => {
      const meta = WORKFLOWS[w.dag_id] || { what: "", when: "", staleMin: null };
      const [cls, label] = RUN_STATE[w.state] || ["neutral", cap(w.state || "unknown")];
      const stale = meta.staleMin && w.minutes_ago != null && w.minutes_ago > meta.staleMin;
      const next = meta.times ? nextRun(meta) : null;
      return el("tr", {},
        cell("Workflow", { class: "full" }, el("span", { class: "wf-name" }, w.dag_id), el("span", { class: "wf-what" }, meta.what)),
        cell("Schedule", {}, meta.when || "—"),
        cell("Latest run", {}, el("span", {}, w.end_utc ? fmtWhen(w.end_utc) : "—"),
          w.end_utc ? el("span", { class: "sub" }, fmtAgo(w.minutes_ago)) : null,
          stale ? el("span", { class: "warn-line" }, "No run for a while: is Airflow running?") : null),
        cell("Next run", {}, next ? fmtWhen(next) : "—",
          next ? el("span", { class: "sub" }, `in ${fmtAgo((next - Date.now()) / 60000).replace(" ago", "")}`) : null),
        cell("Took", { class: "nowrap" }, fmtDuration(w.duration_min)),
        cell("Result", {}, el("span", { class: `pill ${stale ? "attention" : cls}` }, stale ? "Overdue" : label)),
        cell("Airflow", {}, w.url ? el("a", { class: "ext", href: w.url, target: "_blank", rel: "noopener" }, "Open in Airflow", svgIcon("external")) : ""),
      );
    })),
  );
}

function renderProblems(d) {
  fill(document.getElementById("problems"), 
    ...(d.recent_problems.length
      ? d.recent_problems.map((p) => el("div", { class: "problem" },
          el("span", {}, p.time), el("span", { class: `lvl-${p.level}` }, cap(p.level.toLowerCase())), el("span", {}, `${p.file}: ${p.message}`)))
      : [el("p", { class: "empty-ok" }, el("span", { class: "pill ok" }, "OK"), "No warnings or errors in the collector logs.")]),
  );
}

// ---------- pipeline graph ----------

function svgEl(tag, attrs, ...children) {
  const n = document.createElementNS("http://www.w3.org/2000/svg", tag);
  Object.entries(attrs).forEach(([k, v]) => n.setAttribute(k, v));
  children.forEach((c) => n.append(c));
  return n;
}

function layout(stages, edges) {
  // Top-to-bottom layers: a step sits one row below the deepest step that feeds it.
  const level = Object.fromEntries(stages.map((s) => [s.name, 0]));
  for (let i = 0; i < stages.length; i++) {
    for (const [a, b] of edges) level[b] = Math.max(level[b], level[a] + 1);
  }
  const rows = [];
  stages.forEach((s) => (rows[level[s.name]] = rows[level[s.name]] || []).push(s.name));
  // Order each row by the average position of its neighbours (down, then up, twice)
  // so arrows cross less.
  const index = () => Object.fromEntries(rows.flatMap((r) => r.map((n, i) => [n, i / Math.max(1, r.length - 1)])));
  const sortBy = (r, nb) => {
    const at = index();
    const key = (n) => { const xs = nb(n).map((m) => at[m]); return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : at[n]; };
    r.sort((a, b) => key(a) - key(b));
  };
  for (let pass = 0; pass < 2; pass++) {
    rows.forEach((r, lv) => lv > 0 && sortBy(r, (n) => edges.filter(([, b]) => b === n).map(([a]) => a)));
    [...rows].reverse().forEach((r, k) => k > 0 && sortBy(r, (n) => edges.filter(([a]) => a === n).map(([, b]) => b)));
  }
  const W = 160, H = 46, GX = 14, GY = 36, PAD = 16;
  const width = PAD * 2 + Math.max(...rows.map((r) => r.length)) * (W + GX) - GX;
  const pos = {};
  rows.forEach((row, lv) => {
    const rowWidth = row.length * (W + GX) - GX;
    row.forEach((n, i) => (pos[n] = { x: (width - rowWidth) / 2 + i * (W + GX), y: PAD + lv * (H + GY) }));
  });
  return { pos, W, H, width, height: PAD * 2 + rows.length * (H + GY) - GY };
}

function renderGraph(d) {
  const svg = document.getElementById("dag");
  const { pos, W, H, width, height } = layout(d.stages, d.edges);
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("width", width);
  svg.setAttribute("height", height);
  const defs = svgEl("defs", {},
    svgEl("marker", { id: "arrow", viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" },
      svgEl("path", { d: "M0,0 L10,5 L0,10 z", fill: cssVar("--line-strong") })));
  const edges = d.edges.map(([a, b]) => {
    const p = pos[a], q = pos[b];
    const x1 = p.x + W / 2, y1 = p.y + H, x2 = q.x + W / 2, y2 = q.y - 2, my = (y1 + y2) / 2;
    const hot = page.selected === a || page.selected === b;
    return svgEl("path", { class: "edge" + (hot ? " hot" : ""), d: `M${x1},${y1} C${x1},${my} ${x2},${my} ${x2},${y2}`, "marker-end": "url(#arrow)" });
  });
  const nodes = d.stages.map((s) => {
    const st = statusOf(s.status);
    const g = svgEl("g", {
      class: "node" + (page.selected === s.name ? " selected" : ""),
      transform: `translate(${pos[s.name].x},${pos[s.name].y})`,
      tabindex: 0, role: "button", "aria-label": `${s.name}: ${st.label}`,
    },
      svgEl("rect", { width: W, height: H, rx: 8, fill: cssVar(st.fill), stroke: cssVar(st.stroke), "stroke-dasharray": s.status === "unknown" ? "4 3" : "none" }),
      svgEl("text", { x: 10, y: 19, "font-weight": 700 }, document.createTextNode(s.name)),
      svgEl("text", { x: 10, y: 36, class: "sub-t" }, document.createTextNode(s.last_run_utc ? `ran ${fmtAgo(minutesSince(s.last_run_utc))}` : "no run recorded")));
    const pick = () => selectStage(s.name);
    g.addEventListener("click", pick);
    g.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); } });
    return g;
  });
  fill(svg, defs, ...edges, ...nodes);
  fill(document.getElementById("legend"), 
    ...["up to date", "changed", "runs every time", "waits for retrain"].map((k) =>
      el("span", {}, el("i", { style: `background:${cssVar(STATUS[k].fill)};border-color:${cssVar(STATUS[k].stroke)}` }), STATUS[k].label)),
  );
}

function reportValue(v) {
  if (Array.isArray(v)) return v.join(", ") || "none";
  if (typeof v === "number") return Number.isInteger(v) ? fmtNum(v) : v.toLocaleString("en-US", { maximumFractionDigits: 3 });
  return String(v);
}

function selectStage(name) {
  page.selected = name;
  renderGraph(page.data);
  document.querySelectorAll("#stages tbody tr").forEach((tr) => tr.classList.toggle("selected", tr.dataset.stage === name));
  const s = page.data.stages.find((x) => x.name === name);
  const st = statusOf(s.status);
  const rows = s.report ? Object.entries(s.report) : [];
  fill(document.getElementById("stage-detail"), ...[
    el("h3", {}, s.name),
    el("span", { class: `pill ${st.cls}` }, st.label),
    s.reasons.length ? el("p", { class: "reasons" }, "Why: ", s.reasons.join("; ")) : null,
    el("dl", { class: "kv" },
      el("dt", {}, "Command"), el("dd", {}, el("code", { class: "cmd" }, s.cmd)),
      el("dt", {}, "Last run"), el("dd", {}, s.last_run_utc ? fmtWhenAgo(s.last_run_utc) : "No run recorded"),
      el("dt", {}, "Report"), el("dd", {}, s.report_file || "none"),
      rows.flatMap(([k, v]) => [el("dt", {}, cap(k.replaceAll("_", " "))), el("dd", {}, reportValue(v))])),
  ].filter(Boolean));
}

function renderTable(d) {
  const rate = Object.fromEntries(d.throughput.map((t) => [t.stage, t.rows_per_second]));
  const anyReason = d.stages.some((s) => s.reasons.length);
  const cols = ["Step", "Status", "Last run", "Rows per second", anyReason ? "Why it needs a re-run" : null].filter(Boolean);
  fill(document.getElementById("stages"), 
    el("thead", {}, el("tr", {}, cols.map((h) => el("th", { scope: "col" }, h)))),
    el("tbody", {}, d.stages.map((s) => {
      const st = statusOf(s.status);
      return el("tr", { "data-stage": s.name, class: s.name === page.selected ? "selected" : "" },
        cell("Step", {}, el("button", { type: "button", class: "linklike", onclick: () => {
          selectStage(s.name);
          if (window.innerWidth <= 760) document.getElementById("stage-detail").scrollIntoView({ behavior: "smooth", block: "start" });
        } }, s.name)),
        cell("Status", {}, el("span", { class: `pill ${st.cls}` }, cap(s.status))),
        cell("Last run", {}, s.last_run_utc ? [el("span", {}, fmtWhen(s.last_run_utc)), el("span", { class: "sub" }, fmtAgo(minutesSince(s.last_run_utc)))] : "—"),
        cell("Rows per second", {}, rate[s.name] ? fmtNum(rate[s.name]) : "—"),
        anyReason ? cell("Why", {}, s.reasons.join("; ") || "—") : null);
    })),
  );
}

function renderSpeed(d) {
  const canvas = document.getElementById("speed");
  if (!canvas.offsetParent) return;  // tab hidden: drawn when it is opened
  const rows = [...d.throughput].sort((a, b) => b.rows_per_second - a.rows_per_second);
  if (page.chart) page.chart.destroy();
  page.chart = new Chart(canvas, {
    type: "bar",
    data: { labels: rows.map((r) => r.stage), datasets: [{ data: rows.map((r) => r.rows_per_second), backgroundColor: cssVar("--accent"), borderRadius: 4, barPercentage: 0.7 }] },
    options: {
      indexAxis: "y", maintainAspectRatio: false, animation: false,
      scales: {
        x: { type: "logarithmic", title: { display: true, text: "rows per second", color: cssVar("--muted") },
          ticks: { color: cssVar("--muted"), maxRotation: 0, autoSkipPadding: 14, callback: (v) => ([1, 2, 5].includes(Number(String(v)[0])) && /^\d0*$/.test(String(v).slice(1)) ? fmtNum(v) : "") },
          grid: { color: cssVar("--grid") }, border: { display: false } },
        y: { ticks: { color: cssVar("--ink") }, grid: { display: false }, border: { display: false } },
      },
      plugins: {
        legend: { display: false },
        tooltip: { backgroundColor: cssVar("--tip-bg"), titleColor: cssVar("--tip-ink"), bodyColor: cssVar("--tip-ink"), displayColors: false,
          callbacks: { label: (c) => `${fmtNum(c.parsed.x)} rows per second` } },
      },
    },
  });
}

// ---------- live data collection ----------

function fmtRange(a, b) {  // "Sun 1 Nov, 00:05 and 04:05 IST", the day once if it is the same
  return fmtDay(a) === fmtDay(b) ? `${fmtDay(a)}, ${fmtClock(a)} and ${fmtClock(b)} IST` : `${fmtWhen(a)} and ${fmtWhen(b)}`;
}

function renderLive(d) {
  const c = d.collection;
  const SLOT = 30 * 60 * 1000;
  const now = Date.now();
  const start = Math.floor(now / SLOT) * SLOT - 47 * SLOT;  // 48 half-hours, the last one is now
  const times = c.times_24h.map((t) => new Date(t).getTime());
  const slotStart = (i) => new Date(start + i * SLOT);
  const longest = c.gaps_24h.reduce((m, g) => Math.max(m, g.minutes), 0);
  const age = c.minutes_since_last;

  fill(document.getElementById("live-stats"), 
    el("div", { class: "big-stat" + (c.snapshots_24h < c.expected_24h * 0.9 ? " warn" : "") },
      el("span", {}, "Collected, last 24 hours"), el("strong", {}, `${c.snapshots_24h} of ${c.expected_24h}`), el("em", {}, "one snapshot every 30 minutes")),
    el("div", { class: "big-stat" + (c.gaps_24h.length ? " warn" : "") },
      el("span", {}, "Gaps"), el("strong", {}, String(c.gaps_24h.length)),
      el("em", {}, c.gaps_24h.length ? `longest ${fmtDuration(longest)}` : "none in the last 24 hours")),
    el("div", { class: "big-stat" + (age == null || age > 180 ? " bad" : age > 45 ? " warn" : "") },
      el("span", {}, "Last snapshot"), el("strong", {}, c.last_snapshot_utc ? fmtClock(c.last_snapshot_utc) : "—"),
      el("em", {}, c.last_snapshot_utc ? `${fmtDay(c.last_snapshot_utc)} (${fmtAgo(age)})` : "nothing collected yet")),
    el("div", { class: "big-stat" },
      el("span", {}, "All snapshots"), el("strong", {}, fmtNum(c.total_snapshots)), el("em", {}, "since collection started")),
  );

  const days = [];
  const slots = Array.from({ length: 48 }, (_, i) => {
    const a = slotStart(i).getTime();
    const hit = times.some((t) => t >= a && t < a + SLOT);
    const pending = !hit && now - a < 35 * 60000;  // this half-hour's snapshot is not due yet
    const day = fmtDay(a);
    if (!days.length || days[days.length - 1].day !== day) days.push({ day, start: i, n: 0 });
    days[days.length - 1].n += 1;
    return el("span", {
      class: hit ? "" : pending ? "pending" : "miss",
      "data-tip": `${fmtDay(a)}, ${fmtClock(a)} to ${fmtClock(a + SLOT)} IST\n${hit ? "Collected" : pending ? "Not due yet" : "Missing"}`,
    });
  });
  const strip = document.getElementById("live-strip");
  fill(strip, ...slots);
  strip.setAttribute("aria-label", `${c.snapshots_24h} of ${c.expected_24h} half-hour snapshots collected in the last 24 hours`);
  fill(document.getElementById("live-days"), 
    ...days.map((x) => el("span", { style: `grid-column:${x.start + 1} / span ${x.n}` }, x.n >= 6 ? x.day : "")));
  fill(document.getElementById("live-axis"), 
    ...Array.from({ length: 48 }, (_, i) => el("span", {}, i % 8 === 0 ? fmtClock(slotStart(i)) : "")));
  fill(document.getElementById("gaps"), 
    ...(c.gaps_24h.length
      ? c.gaps_24h.map((g) => el("li", {},
          el("span", {}, g.ongoing ? `No snapshot since ${fmtWhen(g.from_utc)}` : `No snapshot between ${fmtRange(g.from_utc, g.to_utc)}`),
          el("span", { class: "pill attention" }, g.ongoing ? `${fmtDuration(g.minutes)}, still missing` : fmtDuration(g.minutes))))
      : [el("li", {}, el("span", {}, "No gaps in the last 24 hours."), el("span", { class: "pill ok" }, "OK"))]),
  );
}

// ---------- model and quality gate ----------

const pct = (x) => `${(x * 100).toFixed(1)}%`;
const ruleNumber = (t) => { const m = String(t).match(/(-?[\d.]+)\s*(x|%)?\s*[a-z ]*$/i) || String(t).match(/(-?[\d.]+)/); return m ? m[1] : t; };
const CHECKS = {
  overall_skill: { name: "Better than “no change” overall", text: (v, t) => `${pct(v)} better (needs at least ${pct(t)})`, meter: (v, t) => [v, t, Math.max(v, t) * 1.4] },
  aqi_category_accuracy: { name: "AQI category right", text: (v, t) => `${pct(v)} of forecasts (needs at least ${pct(t)})`, meter: (v, t) => [v, t, 1] },
  every_horizon_vs_persistence: { name: "Every forecast hour beats “no change”", text: (v, t) => `${String(v).replace("horizons", "forecast hours")} (each hour's error at most ${(String(t).match(/([\d.]+)\s*x/) || [0, "1.05"])[1]} × the “no change” error)` },
  every_city_vs_persistence: { name: "Every city beats “no change”", text: (v, t) => `${v} (each city's skill at least ${ruleNumber(t).replace("-", "−")})` },
  predictions_valid: {
    name: "Forecasts are valid numbers",
    text: (v) => {
      const m = String(v).match(/min ([\d.-]+), max ([\d.-]+), non-finite (\d+), no forecast ([\d.]+%)/);
      return m ? `Range ${m[1]} to ${m[2]} µg/m³, ${m[3]} invalid, ${m[4]} without a forecast (needs all within 0–1000 and at most 1% without)` : String(v);
    },
  },
};

function renderModel(d) {
  const m = d.champion || {};
  const g = d.gate;
  const r = d.registry;
  const acc = g && g.checks.find((x) => x.check === "aqi_category_accuracy");
  const stats = [
    m.test_mae != null ? ["Test error", `${m.test_mae.toFixed(2)} µg/m³`, "average miss on the test period"] : null,
    m.test_skill != null ? ["Better than “no change”", `${Math.round(m.test_skill * 100)}%`, `skill ${m.test_skill.toFixed(2)} on the test period`] : null,
    acc ? ["AQI category right", pct(acc.value), "on the validation data"] : null,
    m.version ? ["Version", String(m.version), `${m.name}, the champion`] : null,
  ].filter(Boolean);
  fill(document.getElementById("model-stats"), ...stats.map(([a, b, c]) =>
    el("div", { class: "big-stat" }, el("span", {}, a), el("strong", {}, b), el("em", {}, c))));
  const kv = [
    ["Model", m.name ? `${m.name}, version ${m.version} (champion)` : "No champion model"],
    m.trained_at ? ["Trained", fmtWhenAgo(m.trained_at)] : null,
    ["Predicts", m.target_mode === "change" ? "the change from the current 24-hour average" : "the 24-hour average"],
    m.val_mae != null ? ["Validation error", `${m.val_mae.toFixed(2)} µg/m³ (mean absolute error)`] : null,
    r ? ["Last decision", `${r.promoted ? `Version ${r.champion_version} promoted` : `Kept version ${r.champion_version}`}: ${r.reason}${r.decided_at_utc ? `, ${fmtWhen(r.decided_at_utc)}` : ""}`] : null,
  ].filter(Boolean);
  fill(document.getElementById("model-kv"), ...kv.flatMap(([a, b]) => [el("dt", {}, a), el("dd", {}, b)]));

  const line = document.getElementById("gate-line");
  const list = document.getElementById("checks");
  if (!g) { line.textContent = "No quality gate report yet."; fill(list); return; }
  const passed = g.checks.filter((x) => x.passed).length;
  fill(line, el("span", { class: `pill ${g.passed ? "ok" : "bad"}` }, g.passed ? "Passed" : "Failed"), " ",
    `${passed} of ${g.checks.length} checks passed, ${fmtWhen(g.checked_at_utc)}, on the validation data.`);
  fill(list, ...g.checks.map((x) => {
    const spec = CHECKS[x.check];
    const meter = spec && spec.meter && typeof x.value === "number" ? spec.meter(x.value, x.threshold) : null;
    return el("li", { class: "check" },
      el("span", { class: `mark ${x.passed ? "pass" : "fail"}`, "aria-label": x.passed ? "passed" : "failed" }, x.passed ? "✓" : "✗"),
      el("div", {},
        el("div", { class: "check-name" }, spec ? spec.name : cap(x.check.replaceAll("_", " "))),
        el("div", { class: "check-val" }, spec ? spec.text(x.value, x.threshold) : `${x.value} (rule: ${x.threshold})`),
        meter ? el("div", { class: "meter" + (x.passed ? "" : " fail"), "data-tip": `${spec.name}\nValue ${pct(meter[0])}, limit ${pct(meter[1])}` },
          el("b", { style: `width:${Math.min(100, (meter[0] / meter[2]) * 100)}%` }),
          el("i", { style: `left:${Math.min(100, (meter[1] / meter[2]) * 100)}%` })) : null));
  }));
}

// ---------- monitoring ----------

const MON_STALE_H = 3;  // the monitor runs hourly: an older report means it is not running
const fmtMae = (x) => (x == null ? "—" : `${x.toFixed(1)} µg/m³`);
const INPUT_NAME = {
  pm25_24h: "PM2.5, 24-hour average", pm25_change_24h: "PM2.5 change over 24 hours",
  temperature_2m: "Temperature", relative_humidity_2m: "Humidity", wind_speed_10m: "Wind speed", rain_24h: "Rain over 24 hours",
};
const LEVEL = { stable: "ok", moderate: "attention", drift: "bad" };

function monitoringState(d) {
  const m = d.monitoring || {};
  const firing = (d.alerts && d.alerts.firing) || [];
  const ev = m.evaluation, dr = m.drift;
  const crit = firing.filter((a) => a.severity === "critical");
  const evalLine = ev && ev.overall && ev.overall.pairs
    ? `Forecast error ${fmtMae(ev.overall.mae_model)} vs “no change” ${fmtMae(ev.overall.mae_no_change)}`
    : "No forecast/actual pairs yet";
  const badge = firing.length ? String(firing.length) : (ev && ev.error_high) || (dr && dr.drift) ? "!" : "";
  if (crit.length) return { cls: "bad", line: `${firing.length} alert${firing.length > 1 ? "s" : ""} firing`, detail: crit.map((a) => a.summary).join("; "), badge };
  if (firing.length) return { cls: "attention", line: `${firing.length} alert${firing.length > 1 ? "s" : ""} firing`, detail: firing.map((a) => a.summary).join("; "), badge };
  if (!ev && !dr) return { cls: "neutral", line: "No checks yet", detail: "The hourly monitor workflow has not run yet", badge };
  if (ev && ev.age_hours > MON_STALE_H) return { cls: "attention", line: "Monitor not running", detail: `Last forecast check ${fmtWhenAgo(ev.evaluated_at_utc)}`, badge };
  if (ev && ev.error_high) return { cls: "attention", line: "Forecast error high", detail: evalLine, badge };
  if (dr && dr.drift) return { cls: "attention", line: "Data drift", detail: "Live inputs differ from the training data", badge };
  return { cls: "ok", line: "No alerts", detail: evalLine, badge };
}

function renderAlerts(d) {
  const a = d.alerts || { firing: [], resolved: [] };
  fill(document.getElementById("alerts-now"), ...(a.firing.length
    ? a.firing.map((x) => el("div", { class: `alert ${x.severity === "critical" ? "bad" : "attention"}` },
        el("div", { class: "alert-top" },
          el("span", { class: `pill ${x.severity === "critical" ? "bad" : "attention"}` }, x.severity === "critical" ? "Critical" : "Warning"),
          el("strong", {}, x.summary || x.alertname),
          el("span", { class: "sub" }, x.starts_at ? `since ${fmtWhenAgo(x.starts_at)}` : "")),
        el("p", {}, x.description)))
    : [el("p", { class: "empty-ok" }, el("span", { class: "pill ok" }, "OK"), "No alerts firing: the service, the live data and the model are within their limits.")]));
  const past = a.resolved || [];
  document.getElementById("alerts-past-wrap").hidden = !past.length;
  fill(document.getElementById("alerts-past"), ...past.map((x) => {
    const lasted = x.starts_at && x.ends_at ? (new Date(x.ends_at) - new Date(x.starts_at)) / 60000 : null;
    return el("li", {},
      el("span", {}, el("strong", {}, x.summary || x.alertname), el("span", { class: "sub" }, x.alertname)),
      el("span", { class: "sub" }, `resolved ${x.ends_at ? fmtWhenAgo(x.ends_at) : ""}${lasted != null ? `, lasted ${fmtDuration(lasted)}` : ""}`));
  }));
}

function renderEvaluation(d) {
  const ev = (d.monitoring || {}).evaluation;
  const line = document.getElementById("eval-line");
  const stats = document.getElementById("eval-stats");
  const table = document.getElementById("eval-horizon");
  if (!ev) {
    line.textContent = "No forecast check yet: the monitor saves every forecast and compares it with the measurement once its hour has passed.";
    fill(stats); fill(table);
    return;
  }
  const o = ev.overall || {};
  const stale = ev.age_hours > MON_STALE_H;
  fill(line,
    `Every forecast the service showed is saved and, once its hour has passed, compared with what was measured. Last ${ev.window_days} days; checked ${fmtWhenAgo(ev.evaluated_at_utc)}.`,
    stale ? el("span", { class: "warn-line" }, "This check is old: is Airflow running?") : null);
  const enough = o.pairs >= ev.min_pairs;
  fill(stats,
    el("div", { class: "big-stat" + (ev.error_high ? " bad" : "") },
      el("span", {}, "Model error"), el("strong", {}, fmtMae(o.mae_model)),
      el("em", {}, ev.limit_mae != null ? `alert above ${ev.limit_mae.toFixed(1)} (1.5 × test error ${ev.test_mae})` : "average miss")),
    el("div", { class: "big-stat" }, el("span", {}, "“No change” error"), el("strong", {}, fmtMae(o.mae_no_change)), el("em", {}, "the baseline to beat")),
    el("div", { class: "big-stat" + (o.skill != null && o.skill < 0 ? " warn" : "") },
      el("span", {}, "Better than “no change”"), el("strong", {}, o.skill == null ? "—" : `${Math.round(o.skill * 100)}%`), el("em", {}, "lower error than the baseline")),
    el("div", { class: "big-stat" + (enough ? "" : " warn") },
      el("span", {}, "Forecast/actual pairs"), el("strong", {}, fmtNum(o.pairs || 0)),
      el("em", {}, enough ? `enough to judge (≥ ${ev.min_pairs})` : `under ${ev.min_pairs}: too few to alert`)));
  const H = { "1-6h": "1–6 hours ahead", "7-12h": "7–12 hours ahead", "13-24h": "13–24 hours ahead" };
  const rows = Object.entries(ev.by_horizon || {});
  fill(table,
    el("thead", {}, el("tr", {}, ["Forecast", "Model", "No change", "Pairs"].map((h) => el("th", { scope: "col" }, h)))),
    el("tbody", {}, ...rows.map(([k, s]) => el("tr", {},
      el("td", {}, H[k] || k),
      el("td", { class: s.mae_model != null && s.mae_no_change != null && s.mae_model > s.mae_no_change ? "worse" : "" }, fmtMae(s.mae_model)),
      el("td", {}, fmtMae(s.mae_no_change)),
      el("td", {}, fmtNum(s.pairs || 0))))));
}

function renderErrChart(d) {
  const canvas = document.getElementById("err-chart");
  if (!canvas.offsetParent) return;  // tab hidden: drawn when it is opened
  const ev = (d.monitoring || {}).evaluation;
  const days = (ev && ev.daily) || [];
  if (page.errChart) page.errChart.destroy();
  page.errChart = null;
  if (!days.length) return;
  const at = (x) => new Date(`${x.date}T12:00:00+05:30`);
  const ds = (label, key, color, dash) => ({
    label, data: days.map((x) => x[key]), borderColor: color, backgroundColor: color, borderWidth: 2.5,
    borderDash: dash, pointRadius: 3.5, pointHoverRadius: 5, tension: 0.25, spanGaps: true,
  });
  page.errChart = new Chart(canvas, {
    type: "line",
    data: { labels: days.map((x) => fmtDay(at(x))), datasets: [ds("Model", "mae_model", cssVar("--accent"), []), ds("No change", "mae_no_change", cssVar("--muted"), [6, 4])] },
    options: {
      maintainAspectRatio: false, animation: false, interaction: { mode: "index", intersect: false },
      scales: {
        x: { ticks: { color: cssVar("--muted"), maxRotation: 0, autoSkipPadding: 10 }, grid: { display: false }, border: { display: false } },
        y: { beginAtZero: true, title: { display: true, text: "µg/m³", color: cssVar("--muted") }, ticks: { color: cssVar("--muted") }, grid: { color: cssVar("--grid") }, border: { display: false } },
      },
      plugins: {
        legend: { labels: { color: cssVar("--ink"), usePointStyle: true, boxHeight: 6 } },
        tooltip: {
          backgroundColor: cssVar("--tip-bg"), titleColor: cssVar("--tip-ink"), bodyColor: cssVar("--tip-ink"),
          callbacks: { label: (c) => `${c.dataset.label}: ${fmtMae(c.parsed.y)}`, footer: (items) => `${fmtNum(days[items[0].dataIndex].pairs)} pairs` },
        },
      },
    },
  });
}

function renderDrift(d) {
  const m = d.monitoring || {};
  const dr = m.drift;
  const line = document.getElementById("drift-line");
  const bars = document.getElementById("drift-bars");
  const nextAt = m.drift_hour_ist != null ? nextRun({ times: () => [[m.drift_hour_ist, 50]] }) : null;
  const next = nextAt ? ` Next check ${fmtWhen(nextAt)}.` : "";
  if (!dr) { line.textContent = `No drift check yet.${next}`; fill(bars); fill(document.getElementById("drift-key")); return; }
  fill(line, `PSI (population stability index) of each input: the last ${dr.live_days} days of live data against ${dr.reference}. Checked ${fmtWhenAgo(dr.checked_at_utc)}.${next}`);
  const values = Object.values(dr.inputs).map((v) => v.psi).filter((v) => v != null);
  const top = Math.max(0.5, dr.psi_drift * 1.6, ...values.map((v) => v * 1.15));
  const pos = (x) => `${Math.min(100, (x / top) * 100)}%`;
  fill(bars, ...Object.entries(dr.inputs).map(([k, v]) => {
    const cls = LEVEL[v.level] || "neutral";
    const has = v.psi != null;
    return el("div", { class: "drift-row" },
      el("span", { class: "drift-name" }, INPUT_NAME[k] || k),
      el("div", { class: "drift-bar", "data-tip": has ? `${INPUT_NAME[k] || k}
PSI ${v.psi.toFixed(2)} (${v.level})
Median live ${v.live_median}, training ${v.train_median}
${fmtNum(v.live_rows)} live values` : `${INPUT_NAME[k] || k}
${v.level} (${fmtNum(v.live_rows)} live values)` },
        has ? el("b", { class: cls, style: `width:${pos(v.psi)}` }) : null,
        el("i", { style: `left:${pos(dr.psi_moderate)}` }), el("i", { class: "strong", style: `left:${pos(dr.psi_drift)}` })),
      el("span", { class: "drift-val" }, has ? v.psi.toFixed(2) : "—"),
      el("span", { class: `pill ${has ? cls : "neutral"}` }, cap(v.level)));
  }));
  fill(document.getElementById("drift-key"),
    el("span", {}, el("i", { class: "tick" }), `${dr.psi_moderate}: moderate change`),
    el("span", {}, el("i", { class: "tick strong" }), `${dr.psi_drift}: drift (alert, may start a retrain)`));
}

function renderRetrain(d) {
  const m = d.monitoring || {};
  const dec = m.decision, trig = m.last_trigger;
  const recent = d.gate && d.gate.info && d.gate.info.recent_live;
  const r = d.registry;
  let nextOk = null;
  if (trig && m.cooldown_days) nextOk = new Date(new Date(trig.triggered_at_utc).getTime() + m.cooldown_days * 86400000);
  const kv = [
    ["Automatic retraining", m.auto_retrain ? `On: started when the forecast error is high or there is drift, at most every ${m.cooldown_days} days` : "Off (monitoring.auto_retrain in params.yaml)"],
    ["Latest decision", dec ? [el("span", { class: `pill ${dec.retrain ? "attention" : "ok"}` }, dec.retrain ? "Retrain started" : "No retrain"), " ", `${cap(dec.reason)}. ${fmtWhenAgo(dec.decided_at_utc)}`] : "No decision yet"],
    ["Last automatic start", trig ? `${fmtWhenAgo(trig.triggered_at_utc)}: ${trig.reason}` : "Never"],
    nextOk && nextOk > Date.now() ? ["Next possible", `${fmtWhen(nextOk)} (cooldown)`] : null,
    recent ? ["Newest live days (latest retrain)",
      `${fmtMae(recent.model.mae)} vs “no change” ${fmtMae(recent.persistence.mae)} (${fmtNum(recent.model.n)} forecasts, ${fmtDay(recent.from_utc)} to ${fmtDay(recent.to_utc)}; reported, not used by the gate)`] : null,
    r ? ["Champion decision", `${r.promoted ? `Version ${r.champion_version} promoted` : `Kept version ${r.champion_version}`}: ${r.reason}`] : null,
  ].filter(Boolean);
  fill(document.getElementById("retrain-kv"), ...kv.flatMap(([a, b]) => [el("dt", {}, a), el("dd", {}, ...[].concat(b))]));
}

// Grafana panels: [dashboard uid, panel id, title]. Embedded only on the laptop,
// where Grafana listens (127.0.0.1:3000); loaded the first time the tab is opened.
const GRAFANA_PANELS = [
  ["airsense-service", 5, "Requests per minute by endpoint"],
  ["airsense-service", 7, "Forecast response time"],
  ["airsense-data", 6, "Newest and oldest data age"],
  ["airsense-data", 7, "Snapshots in the last 24 h over time"],
];
const onLaptop = () => ["localhost", "127.0.0.1", "::1", "[::1]"].includes(location.hostname);
const panelUrl = (g, [uid, id]) => `${g.replace(/\/$/, "")}/d-solo/${uid}/airsense?orgId=1&panelId=${id}&theme=${currentTheme()}&from=now-24h&to=now&refresh=1m`;

function renderGrafana(d) {
  const box = document.getElementById("grafana");
  const links = d.monitoring_links || {};
  const g = links.grafana;
  const tools = [
    g ? ["Grafana: service dashboard", `${g.replace(/\/$/, "")}/d/airsense-service`] : null,
    g ? ["Grafana: data and model dashboard", `${g.replace(/\/$/, "")}/d/airsense-data`] : null,
    links.prometheus ? ["Prometheus: alert rules", `${links.prometheus.replace(/\/$/, "")}/alerts`] : null,
    links.alertmanager ? ["Alertmanager", links.alertmanager] : null,
  ].filter(Boolean);
  fill(document.getElementById("tool-links"), ...tools.map(([t, u]) =>
    el("a", { class: "ext", href: u, target: "_blank", rel: "noopener" }, t, svgIcon("external"))));
  if (!g) { fill(box, el("p", { class: "hint" }, "No Grafana address configured (serving.grafana_url).")); return; }
  if (!onLaptop()) {
    fill(box, el("p", { class: "note-local" }, "Grafana, Prometheus and Alertmanager run only on the laptop (not reachable from other devices), so the live panels and links work only when this page is opened on the laptop itself."));
    page.grafana = false;
    return;
  }
  if (!box.offsetParent) return;  // tab hidden
  if (page.grafana === currentTheme()) return;  // already showing; the panels refresh themselves
  page.grafana = currentTheme();
  fill(box, el("div", { class: "grafana-grid" }, ...GRAFANA_PANELS.map((p) =>
    el("iframe", { src: panelUrl(g, p), title: `Grafana: ${p[2]}`, loading: "lazy", referrerpolicy: "no-referrer" }))),
  el("p", { class: "hint" }, "Live from Grafana, last 24 hours, refreshed every minute. If the panels stay empty, check that the Grafana container is running (docker compose ps)."));
}

// ---------- refresh ----------

async function refresh() {
  try {
    const d = await getJSON("/pipeline");
    page.data = d;
    showBanner("");
    document.getElementById("asof").textContent = `Updated ${fmtWhen(d.generated_at_utc)}`;
    renderSummary(d);
    renderWorkflows(d);
    renderProblems(d);
    renderTable(d);
    selectStage(page.selected || (d.stages.find((s) => s.status === "changed") || d.stages[0]).name);
    renderSpeed(d);
    renderLive(d);
    renderModel(d);
    renderAlerts(d);
    renderEvaluation(d);
    renderErrChart(d);
    renderDrift(d);
    renderRetrain(d);
    renderGrafana(d);
  } catch (e) {
    showBanner(`Could not load the pipeline status: ${reason(e)}. Use Refresh to try again in a moment.`);
    if (!page.data) document.getElementById("asof").textContent = "No status right now";
  }
}

setupShell();
buildPage();
document.getElementById("refresh").addEventListener("click", refresh);
document.addEventListener("themechange", () => {
  if (page.data) { renderGraph(page.data); renderSpeed(page.data); renderErrChart(page.data); renderGrafana(page.data); }
});
refresh();
setInterval(refresh, REFRESH_MS);
