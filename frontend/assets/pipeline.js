// Pipeline console: everything comes from GET /pipeline (read-only).

const REFRESH_MS = 60 * 1000;
const STATUS = {
  "up to date": { cls: "ok", label: "Up to date", fill: "#e6f3ec", stroke: "#2e7d5b" },
  changed: { cls: "attention", label: "Inputs changed, needs a re-run", fill: "#fff1d9", stroke: "#e08a00" },
  "runs every time": { cls: "neutral", label: "Runs on every pipeline run", fill: "#eef2f6", stroke: "#6b7c8d" },
  unknown: { cls: "neutral", label: "Status unknown", fill: "#f4f6f8", stroke: "#9aa7b4" },
};
const page = { data: null, selected: null, chart: null };

const minutesSince = (iso) => (Date.now() - new Date(iso).getTime()) / 60000;
const statusOf = (s) => STATUS[s] || STATUS.unknown;

function showBanner(text) {
  const b = document.getElementById("banner");
  b.textContent = text;
  b.hidden = !text;
}

// ---------- summary row ----------

function statusBlock(cls, title, line, detail) {
  return el("div", { class: `status ${cls}` }, el("h3", {}, title), el("p", { class: "big-line" }, line), el("p", { class: "hint" }, detail));
}

function renderSummary(d) {
  const counts = {};
  d.stages.forEach((s) => (counts[s.status] = (counts[s.status] || 0) + 1));
  const changed = d.stages.filter((s) => s.status === "changed").map((s) => s.name);
  const pipe = d.dvc_status_error
    ? statusBlock("bad", "Pipeline steps", "Status unavailable", d.dvc_status_error)
    : statusBlock(
        changed.length ? "attention" : "ok",
        "Pipeline steps",
        changed.length ? `${changed.length} of ${d.stages.length} need a re-run` : `All ${d.stages.length} up to date`,
        changed.length ? `Changed inputs: ${changed.join(", ")}` : `${counts["runs every time"] || 0} checks run on every pipeline run`,
      );

  const c = d.collection;
  const age = c.minutes_since_last;
  const live = statusBlock(
    age == null ? "bad" : age <= 45 ? "ok" : age <= 180 ? "attention" : "bad",
    "Live data collection",
    age == null ? "No snapshots yet" : `Last snapshot ${fmtAgo(age)}`,
    `${c.snapshots_24h} of ${c.expected_24h} expected snapshots in the last 24 hours; ${c.gaps_24h.length} ${c.gaps_24h.length === 1 ? "gap" : "gaps"}`,
  );

  const m = d.champion || {};
  const model = statusBlock(
    m.version ? "ok" : "bad",
    "Live model",
    m.version ? `${m.name} version ${m.version}` : "No champion model",
    m.test_mae != null ? `Test error ${m.test_mae.toFixed(2)} µg/m³, ${(m.test_skill * 100).toFixed(0)}% better than "no change"` : "",
  );

  const g = d.gate;
  const passed = g ? g.checks.filter((x) => x.passed).length : 0;
  const gate = g
    ? statusBlock(g.passed ? "ok" : "bad", "Quality gate", `${passed} of ${g.checks.length} checks passed`, `Checked ${fmtTimeIST(g.checked_at_utc)} IST on the validation data`)
    : statusBlock("neutral", "Quality gate", "No report yet", "");

  document.getElementById("status-row").replaceChildren(pipe, live, model, gate);
}

// ---------- graph ----------

function layout(stages, edges) {
  // Top-to-bottom layers: a step sits one row below the deepest step that feeds it.
  const level = Object.fromEntries(stages.map((s) => [s.name, 0]));
  for (let i = 0; i < stages.length; i++) {
    for (const [a, b] of edges) level[b] = Math.max(level[b], level[a] + 1);
  }
  const rows = [];
  stages.forEach((s) => (rows[level[s.name]] = rows[level[s.name]] || []).push(s.name));
  const W = 186, H = 46, GX = 22, GY = 34, PAD = 16;
  const width = PAD * 2 + Math.max(...rows.map((r) => r.length)) * (W + GX) - GX;
  const pos = {};
  rows.forEach((row, lv) => {
    if (lv > 0) {  // order each row by where its inputs sit, to keep arrows short
      const centre = (n) => {
        const xs = edges.filter(([, b]) => b === n).map(([a]) => pos[a].x);
        return xs.length ? xs.reduce((x, y) => x + y, 0) / xs.length : Infinity;
      };
      row.sort((m, n) => centre(m) - centre(n));
    }
    const rowWidth = row.length * (W + GX) - GX;
    row.forEach((n, i) => (pos[n] = { x: (width - rowWidth) / 2 + i * (W + GX), y: PAD + lv * (H + GY) }));
  });
  return { pos, W, H, width, height: PAD * 2 + rows.length * (H + GY) - GY };
}

function svgEl(tag, attrs, ...children) {
  const n = document.createElementNS("http://www.w3.org/2000/svg", tag);
  Object.entries(attrs).forEach(([k, v]) => n.setAttribute(k, v));
  children.forEach((c) => n.append(c));
  return n;
}

function renderGraph(d) {
  const svg = document.getElementById("dag");
  const { pos, W, H, width, height } = layout(d.stages, d.edges);
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("width", width);
  svg.setAttribute("height", height);
  const defs = svgEl("defs", {},
    svgEl("marker", { id: "arrow", viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" },
      svgEl("path", { d: "M0,0 L10,5 L0,10 z", fill: "#8b99a8" })));
  const edges = d.edges.map(([a, b]) => {
    const p = pos[a], q = pos[b];
    const x1 = p.x + W / 2, y1 = p.y + H, x2 = q.x + W / 2, y2 = q.y - 2, my = (y1 + y2) / 2;
    return svgEl("path", { class: "edge", d: `M${x1},${y1} C${x1},${my} ${x2},${my} ${x2},${y2}`, "marker-end": "url(#arrow)" });
  });
  const nodes = d.stages.map((s) => {
    const st = statusOf(s.status);
    const g = svgEl("g", {
      class: "node" + (page.selected === s.name ? " selected" : ""),
      transform: `translate(${pos[s.name].x},${pos[s.name].y})`,
      tabindex: 0, role: "button", "aria-label": `${s.name}: ${st.label}`,
    },
      svgEl("rect", { width: W, height: H, rx: 6, fill: st.fill, stroke: st.stroke, "stroke-dasharray": s.status === "unknown" ? "4 3" : "none" }),
      svgEl("text", { x: 10, y: 19, "font-weight": 700, fill: "#1d2733" }, document.createTextNode(s.name)),
      svgEl("text", { x: 10, y: 36, class: "sub" }, document.createTextNode(s.last_run_utc ? `ran ${fmtAgo(minutesSince(s.last_run_utc))}` : "not run yet")));
    const pick = () => selectStage(s.name);
    g.addEventListener("click", pick);
    g.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); } });
    return g;
  });
  svg.replaceChildren(defs, ...edges, ...nodes);
  document.getElementById("legend").replaceChildren(
    ...["up to date", "changed", "runs every time"].map((k) =>
      el("span", {}, el("i", { style: `background:${STATUS[k].fill};border-color:${STATUS[k].stroke}` }), STATUS[k].label)),
  );
}

function selectStage(name) {
  page.selected = name;
  renderGraph(page.data);
  const s = page.data.stages.find((x) => x.name === name);
  const st = statusOf(s.status);
  const rows = s.report ? Object.entries(s.report) : [];
  document.getElementById("stage-detail").replaceChildren(
    el("h3", {}, s.name, " ", el("span", { class: `pill ${st.cls}` }, st.label)),
    s.reasons.length ? el("p", { class: "reasons" }, "Why: ", s.reasons.join("; ")) : "",  // not null: replaceChildren would show "null"
    el("dl", { class: "kv" },
      el("dt", {}, "Command"), el("dd", {}, el("code", { class: "cmd" }, s.cmd)),
      el("dt", {}, "Last run"), el("dd", {}, s.last_run_utc ? `${fmtTimeIST(s.last_run_utc)} IST (${fmtAgo(minutesSince(s.last_run_utc))})` : "—"),
      el("dt", {}, "Report"), el("dd", {}, s.report_file || "none"),
      rows.flatMap(([k, v]) => [el("dt", {}, k.replaceAll("_", " ")), el("dd", {}, Array.isArray(v) ? v.join(", ") || "none" : typeof v === "number" ? v.toLocaleString("en-IN") : String(v))])),
  );
}

// ---------- scheduled workflows (Airflow) ----------

const WORKFLOWS = {
  airsense_live: { what: "Collect live data, process, validate, refresh the live features", when: "Every 30 min, at :05 and :35", staleMin: 45 },
  airsense_retrain: { what: "Retrain, quality gate, register; serve a new champion", when: "Every Sunday at 13:00 IST, or started manually", staleMin: null },
  airsense_housekeeping: { what: "Version the live data, daily Git checkpoint, clean-up", when: "Daily at 23:15 IST", staleMin: null },
};
const RUN_STATE = { success: ["ok", "Succeeded"], failed: ["bad", "Failed"], running: ["attention", "Running"] };

function renderWorkflows(d) {
  const table = document.getElementById("workflows");
  const runs = d.workflows || [];
  if (!runs.length) {
    table.replaceChildren(el("caption", { class: "hint" },
      "No Airflow runs recorded yet. Start Airflow with: bash airflow/start.sh"));
    return;
  }
  table.replaceChildren(
    el("thead", {}, el("tr", {}, ["Workflow", "What it does", "Schedule", "Latest run", "Result", ""].map((h) => el("th", {}, h)))),
    el("tbody", {}, ...runs.map((w) => {
      const meta = WORKFLOWS[w.dag_id] || { what: "", when: "", staleMin: null };
      const [cls, label] = RUN_STATE[w.state] || ["neutral", w.state || "unknown"];
      const stale = meta.staleMin && w.minutes_ago != null && w.minutes_ago > meta.staleMin;
      return el("tr", {},
        el("td", {}, el("code", {}, w.dag_id)),
        el("td", {}, meta.what),
        el("td", {}, meta.when),
        el("td", {}, w.end_utc ? `${fmtTimeIST(w.end_utc)} IST (${fmtAgo(w.minutes_ago)})` : "—",
          stale ? el("div", { class: "hint" }, "No run for a while: is Airflow running?") : ""),
        el("td", {}, el("span", { class: `pill ${stale ? "attention" : cls}` }, label)),
        el("td", {}, w.url ? el("a", { href: w.url, target: "_blank", rel: "noopener" }, "Open in Airflow") : ""),
      );
    })),
  );
}

// ---------- live collection ----------

function renderLive(d) {
  const c = d.collection;
  const end = Date.now();
  const start = end - 24 * 3600 * 1000;
  const times = c.times_24h.map((t) => new Date(t).getTime());
  const slots = Array.from({ length: 48 }, (_, i) => {
    const a = start + i * 30 * 60 * 1000, b = a + 30 * 60 * 1000;
    const hit = times.some((t) => t >= a && t < b);
    return el("span", { class: hit ? "" : "miss", title: `${fmtTimeIST(new Date(a).toISOString())} IST: ${hit ? "collected" : "missing"}` });
  });
  const strip = document.getElementById("live-strip");
  strip.replaceChildren(...slots);
  strip.setAttribute("aria-label", `${c.snapshots_24h} of ${c.expected_24h} half-hour snapshots collected in the last 24 hours`);
  document.getElementById("live-axis").replaceChildren(
    ...Array.from({ length: 48 }, (_, i) =>
      el("span", {}, i % 8 === 0 ? fmtIST(new Date(start + i * 30 * 60 * 1000).toISOString(), { hour: "2-digit", minute: "2-digit", hour12: false }) : "")),
  );
  document.getElementById("live-text").textContent =
    `The collector saves a snapshot of the CPCB feed every 30 minutes (green = collected, pink = missing). ${c.total_snapshots} snapshots in total.`;
  document.getElementById("gaps").replaceChildren(
    ...(c.gaps_24h.length
      ? c.gaps_24h.map((g) => el("li", {}, `Gap of ${Math.round(g.minutes / 6) / 10} h: ${fmtTimeIST(g.from_utc)} to ${fmtTimeIST(g.to_utc)} IST`))
      : [el("li", {}, "No gaps in the last 24 hours.")]),
  );
}

// ---------- model ----------

function renderModel(d) {
  const m = d.champion || {};
  const g = d.gate;
  const r = d.registry;
  const box = document.getElementById("model");
  box.replaceChildren(
    el("dl", { class: "kv" },
      el("dt", {}, "Model"), el("dd", {}, m.name ? `${m.name}, version ${m.version} (champion)` : "—"),
      el("dt", {}, "Trained"), el("dd", {}, m.trained_at ? `${fmtTimeIST(m.trained_at)} IST` : "—"),
      el("dt", {}, "Predicts"), el("dd", {}, m.target_mode === "change" ? "the change from the current 24-hour average" : "the 24-hour average"),
      el("dt", {}, "Validation error"), el("dd", {}, m.val_mae != null ? `${m.val_mae.toFixed(2)} µg/m³ (mean absolute error)` : "—"),
      el("dt", {}, "Test error"), el("dd", {}, m.test_mae != null ? `${m.test_mae.toFixed(2)} µg/m³; skill ${m.test_skill.toFixed(2)} vs "no change"` : "—"),
      el("dt", {}, "Last decision"), el("dd", {}, r ? (r.promoted ? `version ${r.champion_version} promoted: ${r.reason}` : `kept version ${r.champion_version}: ${r.reason}`) : "—")),
    g ? el("h3", { class: "section-h", style: "margin-top:14px" }, "Quality gate checks") : null,
    g ? el("ul", { class: "checks" },
      g.checks.map((x) => el("li", {},
        el("span", { class: `mark ${x.passed ? "pass" : "fail"}`, "aria-label": x.passed ? "passed" : "failed" }, x.passed ? "✓" : "✗"),
        el("span", {}, `${x.check.replaceAll("_", " ")}: ${x.value} (rule: ${x.threshold})`)))) : null,
  );
}

// ---------- speed, table, problems ----------

function renderSpeed(d) {
  const rows = [...d.throughput].sort((a, b) => b.rows_per_second - a.rows_per_second);
  if (page.chart) page.chart.destroy();
  page.chart = new Chart(document.getElementById("speed"), {
    type: "bar",
    data: { labels: rows.map((r) => r.stage), datasets: [{ data: rows.map((r) => r.rows_per_second), backgroundColor: "#1b6ec2" }] },
    options: {
      indexAxis: "y", maintainAspectRatio: false, animation: false,
      scales: { x: { type: "logarithmic", title: { display: true, text: "rows per second" } } },
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: (c) => ` ${Math.round(c.parsed.x).toLocaleString("en-IN")} rows/s` } } },
    },
  });
}

function renderTable(d) {
  const rate = Object.fromEntries(d.throughput.map((t) => [t.stage, t.rows_per_second]));
  document.getElementById("stages").replaceChildren(
    el("thead", {}, el("tr", {}, ["Step", "Status", "Last run (IST)", "Rows per second", "Why it needs a re-run"].map((h) => el("th", { scope: "col" }, h)))),
    el("tbody", {}, d.stages.map((s) => {
      const st = statusOf(s.status);
      return el("tr", {},
        el("td", {}, el("button", { type: "button", class: "linklike", onclick: () => { selectStage(s.name); document.getElementById("graph-h").scrollIntoView(); } }, s.name)),
        el("td", {}, el("span", { class: `pill ${st.cls}` }, s.status)),
        el("td", {}, s.last_run_utc ? fmtTimeIST(s.last_run_utc) : "—"),
        el("td", {}, rate[s.name] ? Math.round(rate[s.name]).toLocaleString("en-IN") : "—"),
        el("td", {}, s.reasons.join("; ") || "—"));
    })),
  );
}

function renderProblems(d) {
  const box = document.getElementById("problems");
  box.replaceChildren(
    ...(d.recent_problems.length
      ? d.recent_problems.map((p) => el("div", { class: "problem" },
          el("span", {}, p.time), el("span", { class: `lvl-${p.level}` }, p.level), el("span", {}, `${p.file}: ${p.message}`)))
      : [el("p", { class: "hint" }, "No warnings or errors in the collector logs.")]),
  );
}

async function refresh() {
  try {
    const d = await getJSON("/pipeline");
    page.data = d;
    showBanner("");
    document.getElementById("asof").textContent = `Status as of ${fmtTimeIST(d.generated_at_utc)} IST`;
    renderSummary(d);
    renderWorkflows(d);
    renderGraph(d);
    renderLive(d);
    renderModel(d);
    renderSpeed(d);
    renderTable(d);
    renderProblems(d);
    selectStage(page.selected || (d.stages.find((s) => s.status === "changed") || d.stages[0]).name);
  } catch (e) {
    showBanner(`Could not load the pipeline status: ${reason(e)}. Use Refresh to try again in a moment.`);
    if (!page.data) document.getElementById("asof").textContent = "No status right now";
  }
}

document.getElementById("refresh").addEventListener("click", refresh);
refresh();
setInterval(refresh, REFRESH_MS);
