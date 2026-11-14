// Forecast page: city tiles from /cities, details from /forecast/{city}?group=...

const REFRESH_MS = 10 * 60 * 1000;
const GROUPS = [
  { id: "general", label: "General public", text: "the general public" },
  { id: "sensitive", label: "Children, elderly, heart or lung conditions", text: "children, older adults and people with heart or lung conditions" },
  { id: "outdoor_workers", label: "Outdoor workers and exercise", text: "people who work or exercise outdoors" },
];
const groupText = (id) => (GROUPS.find((g) => g.id === id) || GROUPS[0]).text;

// Shown with the city name; values are per city (mean of its own stations).
const STATE_OF = {
  Delhi: "Delhi", Lucknow: "Uttar Pradesh", Patna: "Bihar", Kanpur: "Uttar Pradesh",
  Jaipur: "Rajasthan", Chandigarh: "Chandigarh", Mumbai: "Maharashtra", Ahmedabad: "Gujarat",
  Pune: "Maharashtra", Bengaluru: "Karnataka", Chennai: "Tamil Nadu", Hyderabad: "Telangana",
  Kolkata: "West Bengal", Guwahati: "Assam",
};

const state = { city: null, group: "general", tab: "advice", cities: [], f: null, chart: null, india: null, cats: null, pickedCat: null, indiaTab: "by-city", indiaTabs: null };

const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);
const hasValue = (c) => c.pm25_24h != null;
// worst first; cities without a recent value last
const ranked = () => [...state.cities].sort((a, b) => (hasValue(b) - hasValue(a)) || ((b.pm25_24h || 0) - (a.pm25_24h || 0)));
// The exact hour from /cities; the age is rounded to 0.1 h and would drift by minutes (F1).
const measuredAt = (c) => (c.time_utc ? new Date(c.time_utc)
  : c.age_hours == null ? null : new Date(Date.now() - c.age_hours * 3600 * 1000));

// ---------- city tiles and phone dropdown ----------

function renderCities() {
  fill(document.getElementById("cities"), 
    ...ranked().map((c) => {
      const info = aqiInfo(c.aqi_category);
      const known = hasValue(c);
      const stale = !known || (c.age_hours != null && c.age_hours > 4);
      const st = STATE_OF[c.city];
      return el("button", {
        class: "city" + (stale ? " stale" : ""),
        type: "button", role: "listitem",
        "aria-pressed": String(c.city === state.city),
        style: bandStyle(info),
        onclick: () => selectCity(c.city),
        "data-tip": [
          st && st !== c.city ? `${c.city}, ${st}` : c.city,
          known ? `${Math.round(c.pm25_24h)} µg/m³, ${info.name}` : "No recent measurement",
          c.age_hours != null ? `Measured ${fmtWhenAgo(measuredAt(c))}` : null,
        ].filter(Boolean).join("\n"),
      },
        el("span", { class: "name" }, c.city),
        el("span", { class: "val" }, known ? Math.round(c.pm25_24h) : "—"),
        el("span", { class: "cat" }, known ? info.name : "No recent data"),
      );
    }),
  );
}

function renderCitySelect() {
  const sel = document.getElementById("city-select");
  fill(sel, ...ranked().map((c) =>
    el("option", { value: c.city }, hasValue(c) ? `${c.city}: ${Math.round(c.pm25_24h)} µg/m³, ${c.aqi_category}` : `${c.city}: no recent data`)));
  sel.value = state.city;
}

async function loadCities() {
  state.cities = await getJSON("/cities");
  // The exact hour of the newest value (the rounded age would drift by minutes).
  const times = state.cities.filter((c) => c.time_utc).map((c) => new Date(c.time_utc).getTime());
  document.getElementById("asof").textContent = times.length
    ? `Latest measurement ${fmtWhenAgo(new Date(Math.max(...times)))}`
    : "No recent measurements";
  if (!state.city) {
    const first = ranked().find(hasValue) || state.cities[0];
    state.city = first ? first.city : null;
  }
  renderCities();
  renderCitySelect();
  renderOverview();
}

// ---------- across India: summary and bar chart ----------

// Category names above the chart, between the AQI boundaries.
const bandEdges = {
  id: "bandEdges",
  beforeDatasetsDraw(chart) {
    const { ctx, chartArea: a, scales: { x } } = chart;
    ctx.save();
    ctx.font = `600 11px ${cssVar("--font")}`;
    ctx.textBaseline = "bottom";
    let low = 0;
    for (const band of AQI) {
      if (low >= x.max) break;
      const x1 = x.getPixelForValue(low);
      const x2 = x.getPixelForValue(Math.min(band.max, x.max));
      if (low > 0) {
        ctx.strokeStyle = cssVar("--line-strong");
        ctx.setLineDash([3, 4]);
        ctx.beginPath(); ctx.moveTo(x1, a.top - 2); ctx.lineTo(x1, a.bottom); ctx.stroke();
      }
      ctx.fillStyle = cssVar("--muted");
      if (x2 - x1 > ctx.measureText(band.name).width + 10) ctx.fillText(band.name, x1 + 5, a.top - 4);
      low = band.max;
    }
    ctx.restore();
  },
  afterDatasetsDraw(chart) {  // the value at the end of each bar
    const { ctx, scales: { x } } = chart;
    const meta = chart.getDatasetMeta(0);
    ctx.save();
    ctx.textBaseline = "middle";
    meta.data.forEach((bar, i) => {
      const v = chart.data.datasets[0].data[i];
      ctx.fillStyle = v == null ? cssVar("--muted") : cssVar("--ink");
      ctx.font = v == null ? `italic 12px ${cssVar("--font")}` : `700 12px ${cssVar("--font")}`;
      ctx.fillText(v == null ? "No recent data" : Math.round(v), v == null ? x.getPixelForValue(0) + 6 : bar.x + 6, bar.y);
    });
    ctx.restore();
  },
};

function callout(kind, c) {
  const info = aqiInfo(c.aqi_category);
  return el("button", {
    type: "button", class: "callout", style: bandStyle(info), onclick: () => selectCity(c.city, true),
  },
    el("span", { class: "callout-kind" }, kind),
    el("span", { class: "callout-city" }, c.city),
    el("span", { class: "callout-val" }, hasValue(c) ? `${Math.round(c.pm25_24h)} µg/m³, ${info.name}` : "No recent data"));
}

// The two charts sit in tabs; only the visible one is drawn (a hidden canvas has no size).
function setupOverview() {
  const cityPanel = el("div", {},
    el("div", { id: "picked", class: "picked", hidden: "" }),
    el("div", { class: "chart-box india" }, el("canvas", { id: "india-chart", role: "img" })));
  const catPanel = el("div", {},
    el("div", { class: "chart-box cats" }, el("canvas", { id: "category-chart", role: "img" })),
    el("p", { class: "section-note" }, "Hover over a bar to see its cities; select it to show them in “By city”."));
  state.indiaTabs = makeTabs("Across India charts", [
    { id: "by-city", label: "By city", panel: cityPanel },
    { id: "by-category", label: "By category", panel: catPanel },
  ], state.indiaTab, (id) => { state.indiaTab = id; drawIndia(); });
  fill(document.getElementById("india-tabs"), state.indiaTabs);
}

function renderOverview() {
  const box = document.getElementById("overview");
  const order = ranked();
  const known = order.filter(hasValue);
  if (!known.length) { box.hidden = true; return; }
  box.hidden = false;
  const missing = order.filter((c) => !hasValue(c));
  fill(document.getElementById("overview-callouts"),
    callout("Most polluted", known[0]),
    callout("Cleanest", known[known.length - 1]),
    missing.length
      ? el("div", { class: "callout muted-callout" },
          el("span", { class: "callout-kind" }, "No recent data"),
          el("span", { class: "callout-city" }, missing.map((c) => c.city).join(", ")),
          el("span", { class: "callout-val" }, "Skipped until new measurements arrive"))
      : null,
  );
  drawIndia();
}

function drawIndia() {
  const order = ranked();
  if (state.india) { state.india.destroy(); state.india = null; }
  if (state.cats) { state.cats.destroy(); state.cats = null; }
  const picked = document.getElementById("picked");
  const n = order.filter((c) => (c.aqi_category || "No data") === state.pickedCat).length;
  picked.hidden = !state.pickedCat;
  if (state.pickedCat) {
    fill(picked, el("span", {}, `Showing ${state.pickedCat}: ${n} ${n === 1 ? "city" : "cities"}`),
      el("button", { type: "button", class: "ghost small", onclick: () => { state.pickedCat = null; drawIndia(); } }, "Show all"));
  }
  if (state.indiaTab === "by-category") drawCategories(order); else drawCities(order);
}

function drawCities(order) {
  const known = order.filter(hasValue);
  const canvas = document.getElementById("india-chart");
  canvas.parentElement.style.height = `${order.length * 30 + 40}px`;
  canvas.setAttribute("aria-label", "Cities by 24-hour average PM2.5: " +
    order.map((c) => `${c.city} ${hasValue(c) ? Math.round(c.pm25_24h) : "no data"}`).join(", "));
  const max = Math.max(...known.map((c) => c.pm25_24h));
  state.india = new Chart(canvas, {
    type: "bar",
    data: {
      labels: order.map((c) => c.city),
      datasets: [{
        data: order.map((c) => c.pm25_24h),
        // a chosen category keeps its colour; the other cities fade
        backgroundColor: order.map((c) => aqiInfo(c.aqi_category).color +
          (state.pickedCat && (c.aqi_category || "No data") !== state.pickedCat ? "40" : "")),
        borderRadius: 4, borderSkipped: false, barPercentage: 0.78, categoryPercentage: 0.92,
      }],
    },
    options: {
      indexAxis: "y", maintainAspectRatio: false, animation: false,
      layout: { padding: { top: 18, right: 34 } },
      scales: {
        x: { min: 0, max: Math.ceil((max * 1.06) / 50) * 50, grid: { display: false }, border: { display: false },
          ticks: { color: cssVar("--muted"), callback: (v) => fmtNum(v) }, title: { display: true, text: "24-hour average PM2.5 (µg/m³)", color: cssVar("--muted") } },
        y: { grid: { display: false }, border: { display: false }, ticks: { color: cssVar("--ink"), font: { weight: 600 } } },
      },
      plugins: { legend: { display: false }, tooltip: { enabled: false } },
      onClick: (e, els) => { if (els.length) selectCity(order[els[0].index].city, true); },
      onHover: (e, els) => { e.native.target.style.cursor = els.length ? "pointer" : "default"; },
    },
    plugins: [bandEdges],
  });
}

// Cities per category: "No data" first, set apart by a dashed line, then Good → Severe.
const splitNoData = {
  id: "splitNoData",
  beforeDatasetsDraw(chart) {
    const { ctx, chartArea: a, scales: { x } } = chart;
    const px = (x.getPixelForValue(0) + x.getPixelForValue(1)) / 2;
    ctx.save();
    ctx.strokeStyle = cssVar("--line-strong");
    ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(px, a.top); ctx.lineTo(px, a.bottom + 22); ctx.stroke();
    ctx.restore();
  },
  afterDatasetsDraw(chart) {  // the count on top of each bar
    const { ctx } = chart;
    ctx.save();
    ctx.textAlign = "center";
    ctx.font = `800 13px ${cssVar("--font")}`;
    ctx.fillStyle = cssVar("--ink");
    chart.getDatasetMeta(0).data.forEach((bar, i) => ctx.fillText(chart.data.datasets[0].data[i], bar.x, bar.y - 6));
    ctx.restore();
  },
};

function drawCategories(order) {
  const groups = [NO_DATA, ...AQI].map((a) => ({
    ...a, cities: order.filter((c) => (hasValue(c) ? c.aqi_category === a.name : a === NO_DATA)).map((c) => c.city),
  }));
  const canvas = document.getElementById("category-chart");
  canvas.setAttribute("aria-label", "Number of cities per AQI category: " +
    groups.map((g) => `${g.name} ${g.cities.length}`).join(", "));
  if (state.cats) state.cats.destroy();
  state.cats = new Chart(canvas, {
    type: "bar",
    data: {
      labels: groups.map((g) => g.name),
      datasets: [{
        data: groups.map((g) => g.cities.length),
        backgroundColor: groups.map((g) => g.color),
        borderRadius: 5, barPercentage: 0.75, minBarLength: 3,
      }],
    },
    options: {
      maintainAspectRatio: false, animation: false,
      layout: { padding: { top: 20 } },
      scales: {
        y: { beginAtZero: true, suggestedMax: Math.max(...groups.map((g) => g.cities.length)) + 1,
          ticks: { precision: 0, stepSize: 1, color: cssVar("--muted") }, grid: { color: cssVar("--grid") }, border: { display: false },
          title: { display: true, text: "Number of cities", color: cssVar("--muted") } },
        x: { grid: { display: false }, ticks: { color: cssVar("--ink"), font: { weight: 600, size: canvas.parentElement.clientWidth < 500 ? 10 : 12 }, maxRotation: 0, autoSkip: false,
          callback: (v, i) => ({ Satisfactory: ["Satis-", "factory"], Moderate: canvas.parentElement.clientWidth < 500 ? ["Mode-", "rate"] : null }[groups[i].name] || groups[i].name.split(" ")) }, border: { color: cssVar("--line") } },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: cssVar("--tip-bg"), titleColor: cssVar("--tip-ink"), bodyColor: cssVar("--tip-ink"), displayColors: false,
          callbacks: {
            title: (items) => `${groups[items[0].dataIndex].name}: ${groups[items[0].dataIndex].cities.length} ${groups[items[0].dataIndex].cities.length === 1 ? "city" : "cities"}`,
            label: (c) => groups[c.dataIndex].cities.join(", ") || "none right now",
            footer: (items) => (groups[items[0].dataIndex].cities.length ? "Select to show these cities in “By city”" : ""),
          },
        },
      },
      onClick: (e, els) => {
        if (!els.length || !groups[els[0].index].cities.length) return;
        state.pickedCat = groups[els[0].index].name;
        // not inside the chart's own click handler: switching tabs destroys this chart
        setTimeout(() => state.indiaTabs.selectTab("by-city"));
      },
      onHover: (e, els) => { e.native.target.style.cursor = els.length ? "pointer" : "default"; },
    },
    plugins: [splitNoData],
  });
}

// ---------- detail: reading, summary, hours, chart ----------

// Where a value sits on the AQI scale: equal-width segments, position inside one.
function scalePosition(v) {
  let low = 0;
  for (let i = 0; i < AQI.length; i++) {
    const high = AQI[i].max === Infinity ? 380 : AQI[i].max;
    if (v <= high || i === AQI.length - 1) {
      const within = Math.min(1, Math.max(0, (v - low) / (high - low)));
      return ((i + within) / AQI.length) * 100;
    }
    low = high;
  }
  return 100;
}

function readingBlock(f) {
  const info = aqiInfo(f.current.aqi_category);
  const st = STATE_OF[f.city];
  return el("div", { class: "reading", style: bandStyle(info) },
    el("p", { class: "where" }, f.city, st && st !== f.city ? el("span", {}, st) : null),
    el("p", { class: "big" }, Math.round(f.current.pm25_24h), el("small", {}, "µg/m³")),
    el("p", { class: "catname" }, info.name),
    el("div", { class: "scale", role: "img", "aria-label": `${info.name} on India's AQI scale` },
      AQI.map((a) => el("span", { class: a.name === info.name ? "on" : "", style: `background:${a.color}` })),
      el("i", { style: `left:${scalePosition(f.current.pm25_24h)}%` })),
    el("p", { class: "meta" }, `24-hour average, ${fmtWhen(f.current.time_utc)}`),
  );
}

function stat(label, value, sub) {
  return el("div", { class: "stat" }, el("dt", {}, label), el("dd", {}, el("strong", {}, value), el("span", {}, sub)));
}

function summaryBlock(f) {
  const now = f.current.pm25_24h;
  const hi = f.forecast.reduce((a, b) => (b.pm25 > a.pm25 ? b : a));
  const lo = f.forecast.reduce((a, b) => (b.pm25 < a.pm25 ? b : a));
  const last = f.forecast[f.forecast.length - 1];
  const nowCat = aqiInfo(f.current.aqi_category);
  const worst = aqiInfo(hi.aqi_category);
  const change = last.pm25 - now;
  const pct = now > 0 ? (change / now) * 100 : 0;
  const sign = (x) => (Math.round(x) > 0 ? "+" : Math.round(x) < 0 ? "−" : "");
  const trend = change > 5 ? "rising" : change < -5 ? "falling" : "about the same";
  return el("div", { class: "summary" },
    el("h3", {}, worst.name === nowCat.name
      ? `Expected to stay ${worst.name} for the next 24 hours`
      : `Expected to reach ${worst.name} in the next 24 hours`),
    el("dl", { class: "stats" },
      stat("Highest", `${Math.round(hi.pm25)} µg/m³`, fmtWhen(hi.time_utc)),
      stat("Lowest", `${Math.round(lo.pm25)} µg/m³`, fmtWhen(lo.time_utc)),
      stat("Change over 24 hours", `${sign(change)}${Math.abs(Math.round(change))} µg/m³ (${sign(pct)}${Math.abs(Math.round(pct))}%)`, `by ${fmtWhen(last.time_utc)}`)),
    el("p", { class: "trend" }, `By ${fmtWhen(last.time_utc)} the 24-hour average is expected to be ${trend} (${Math.round(last.pm25)} µg/m³).`),
    el("p", { class: "effect", style: bandStyle(worst) }, el("span", { class: "dot" }), el("span", {},
      el("strong", {}, worst.name === nowCat.name ? `At ${worst.name}: ` : `If it reaches ${worst.name}: `),
      `${worst.advice.charAt(0).toLowerCase()}${worst.advice.slice(1)}.`)),
    el("p", { class: "basis" }, `Measured at ${f.current.stations} stations, ${fmtAgo(f.current.age_hours * 60)}.`),
  );
}

// Hour strip and chart share one set of times: the measured value, then 24 forecast hours.
function seriesOf(f) {
  return [
    { time: f.current.time_utc, pm25: f.current.pm25_24h, cat: f.current.aqi_category, measured: true },
    ...f.forecast.map((h) => ({ time: h.time_utc, pm25: h.pm25, cat: h.aqi_category, measured: false })),
  ];
}

// One coloured box per hour above the line, centred on its point; the first is the
// measured value (outlined box, diamond point). Hover, tap or arrow keys pick an hour.
function setActiveHour(chart, index) {
  if (index == null) {
    chart.setActiveElements([]);
    chart.tooltip.setActiveElements([], { x: 0, y: 0 });
  } else {
    const p = chart.getDatasetMeta(0).data[index];
    chart.setActiveElements([{ datasetIndex: 0, index }]);
    chart.tooltip.setActiveElements([{ datasetIndex: 0, index }], { x: p.x, y: p.y });
  }
  chart.update("none");
}

const BOX_H = 22;  // height of the hour boxes above the plot
const hourBoxes = {
  id: "hourBoxes",
  afterDraw(chart) {
    const { ctx, chartArea: a } = chart;
    const pts = chart.getDatasetMeta(0).data;
    const times = chart.data.labels;
    const cats = chart.data.datasets[0].cats;  // in the data: the chart draws during construction (#51)
    const step = pts.length > 1 ? pts[1].x - pts[0].x : 20;
    const w = Math.max(4, step * 0.82);
    const top = a.top - BOX_H - 10;
    const act = chart.getActiveElements();
    const on = act.length ? act[0].index : null;
    ctx.save();
    pts.forEach((p, i) => {
      const colour = aqiInfo(cats[i]).color;
      const x = p.x - w / 2;
      const grow = i === on ? 3 : 0;
      ctx.beginPath();
      ctx.roundRect(x, top - grow, w, BOX_H + grow * 2, 4);
      if (i === 0) {  // measured: outlined
        ctx.fillStyle = colour + "55"; ctx.fill();
        ctx.lineWidth = 2; ctx.strokeStyle = colour; ctx.stroke();
      } else {
        ctx.fillStyle = colour; ctx.fill();
      }
      if (i === on) { ctx.lineWidth = 2.5; ctx.strokeStyle = cssVar("--ink"); ctx.stroke(); }
    });
    // day names above the boxes, where a day starts
    ctx.fillStyle = cssVar("--muted");
    ctx.font = `700 11px ${cssVar("--font")}`;
    ctx.textBaseline = "bottom";
    ctx.fillText(fmtDay(times[0]), pts[0].x - w / 2, top - 6);
    ctx.restore();
  },
};

// AQI bands behind the line, midnight line, peak and low labels, hover line.
const forecastDecor = {
  id: "forecastDecor",
  beforeDraw(chart) {
    const { ctx, chartArea: a, scales: { y } } = chart;
    const pts = chart.getDatasetMeta(0).data;
    const lineY = pts.length ? pts[pts.length - 1].y : null;  // the line at the right edge
    const alpha = cssVar("--band-alpha") || "22";
    let low = 0;
    ctx.save();
    for (const band of AQI) {
      const top = Math.min(band.max, y.max);
      if (top > y.min && low < y.max) {
        const y1 = y.getPixelForValue(Math.max(low, y.min));
        const y2 = y.getPixelForValue(top);
        ctx.fillStyle = band.color + alpha;
        ctx.fillRect(a.left, y2, a.right - a.left, y1 - y2);
        if (y1 - y2 > 16) {
          ctx.fillStyle = cssVar("--muted");
          ctx.font = `600 11px ${cssVar("--font")}`;
          ctx.textAlign = "right";
          // top-right of the band, or bottom-right if the line runs there
          const topY = y2 + 13, bottomY = y1 - 5;
          const far = (ty) => (lineY == null ? Infinity : Math.abs(ty - 4 - lineY));
          ctx.fillText(band.name, a.right - 6, far(topY) >= far(bottomY) ? topY : bottomY);
        }
      }
      low = band.max;
    }
    ctx.restore();
  },
  beforeDatasetsDraw(chart) {
    const { ctx, chartArea: a, scales: { x } } = chart;
    const times = chart.data.labels;  // ISO times of the points
    ctx.save();
    ctx.strokeStyle = cssVar("--line-strong");
    ctx.fillStyle = cssVar("--muted");
    ctx.font = `700 11px ${cssVar("--font")}`;
    ctx.textBaseline = "bottom";
    ctx.setLineDash([4, 4]);
    for (let i = 1; i < times.length; i++) {
      if (fmtDay(times[i]) !== fmtDay(times[i - 1])) {  // midnight: line up through the boxes, day name on top
        const px = (x.getPixelForValue(i - 1) + x.getPixelForValue(i)) / 2;
        ctx.beginPath(); ctx.moveTo(px, a.top - BOX_H - 20); ctx.lineTo(px, a.bottom); ctx.stroke();
        ctx.fillText(fmtDay(times[i]), px + 5, a.top - BOX_H - 16);
      }
    }
    const act = chart.getActiveElements();
    if (act.length) {
      const px = act[0].element.x;
      ctx.setLineDash([]);
      ctx.strokeStyle = cssVar("--ink");
      ctx.globalAlpha = 0.35;
      ctx.beginPath(); ctx.moveTo(px, a.top - 8); ctx.lineTo(px, a.bottom); ctx.stroke();
    }
    ctx.restore();
  },
  afterDatasetsDraw(chart) {
    const { ctx, chartArea: a } = chart;
    const pts = chart.getDatasetMeta(0).data;
    const data = chart.data.datasets[0].data;
    const fc = data.slice(1);  // the measured value is not part of Peak and Low
    const hi = 1 + fc.indexOf(Math.max(...fc));
    const lo = 1 + fc.indexOf(Math.min(...fc));
    ctx.save();
    ctx.font = `700 12px ${cssVar("--font")}`;
    ctx.textAlign = "center";
    const tag = (i, text, above, colour) => {
      const p = pts[i];
      let y = above ? p.y - 13 : p.y + 23;
      if (!above && y > a.bottom - 4) y = p.y - 13;  // no room below: put it above
      if (above && y < a.top + 12) y = p.y + 23;     // no room above: put it below
      // keep the label inside the chart at the left and right edges
      const half = ctx.measureText(text).width / 2;
      const x = Math.min(a.right - half - 2, Math.max(a.left + half + 2, p.x));
      ctx.lineJoin = "round";
      ctx.lineWidth = 4;
      ctx.strokeStyle = cssVar("--paper");  // halo: readable where the line crosses
      ctx.strokeText(text, x, y);
      ctx.fillStyle = colour;
      ctx.fillText(text, x, y);
    };
    tag(hi, `Peak ${Math.round(data[hi])}`, true, cssVar("--raises"));
    if (lo !== hi) tag(lo, `Low ${Math.round(data[lo])}`, false, cssVar("--lowers"));
    ctx.restore();
  },
};

function hourKeys(canvas) {  // keyboard: arrows move from hour to hour
  canvas.addEventListener("keydown", (e) => {
    const c = state.chart;
    if (!c) return;
    const n = c.data.labels.length;
    const act = c.getActiveElements();
    const i = act.length ? act[0].index : -1;
    const to = { ArrowRight: Math.min(n - 1, i + 1), ArrowLeft: Math.max(0, i - 1), Home: 0, End: n - 1 }[e.key];
    if (to != null) { e.preventDefault(); setActiveHour(c, to); }
  });
  canvas.addEventListener("blur", () => state.chart && setActiveHour(state.chart, null));
  // the boxes are above the plot area, where the chart itself ignores the pointer
  const pick = (e) => {
    const c = state.chart;
    if (!c) return;
    const r = canvas.getBoundingClientRect();
    const x = e.clientX - r.left, y = e.clientY - r.top;
    const a = c.chartArea;
    if (y >= a.top - BOX_H - 14 && y < a.top) {
      const pts = c.getDatasetMeta(0).data;
      let best = 0;
      pts.forEach((p, k) => { if (Math.abs(p.x - x) < Math.abs(pts[best].x - x)) best = k; });
      setActiveHour(c, best);
    }
  };
  canvas.addEventListener("pointermove", pick);
  canvas.addEventListener("pointerdown", pick);
}

function drawChart(canvas, f) {
  if (state.chart) state.chart.destroy();
  const series = seriesOf(f);
  const values = series.map((s) => s.pm25);
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const pad = Math.max(8, (hi - lo) * 0.35);
  // Time labels every 3 hours, every 6 on a narrow chart (phone); recomputed when
  // the chart is resized (phone rotated, window narrowed) so they never crowd.
  const tickLabels = (width) => {
    const step = width < 600 ? 6 : 3;
    return series.map((s, i) => {
      if (i % step !== 0) return "";
      const newDay = i === 0 || fmtDay(s.time) !== fmtDay(series[i - step].time);
      return newDay ? [fmtClock(s.time), fmtDay(s.time)] : fmtClock(s.time);
    });
  };
  let ticks = tickLabels(canvas.parentElement.clientWidth);
  const ink = cssVar("--ink");
  const points = (fn) => series.map((s, i) => fn(s, i));
  const peak = 1 + values.slice(1).indexOf(Math.max(...values.slice(1)));
  const low = 1 + values.slice(1).indexOf(Math.min(...values.slice(1)));
  const ring = (i) => i === peak || i === low;
  state.chart = new Chart(canvas, {
    type: "line",
    data: {
      labels: series.map((s) => s.time),
      datasets: [{
        data: values,
        cats: series.map((s) => s.cat),  // AQI category per point, for the boxes
        borderColor: ink, borderWidth: 2, tension: 0.25,
        pointStyle: points((s, i) => (i === 0 ? "rectRot" : "circle")),
        pointRadius: points((s, i) => (i === 0 ? 7 : ring(i) ? 6.5 : 3.5)),
        pointHoverRadius: points((s, i) => (i === 0 ? 9 : ring(i) ? 8 : 6)),
        pointBackgroundColor: points((s) => aqiInfo(s.cat).color),
        pointBorderColor: points((s, i) => (i === peak ? cssVar("--raises") : i === low ? cssVar("--lowers") : ink)),
        pointBorderWidth: points((s, i) => (ring(i) ? 3.5 : i === 0 ? 2 : 1.5)),
      }],
    },
    options: {
      maintainAspectRatio: false, animation: false,
      onResize: (chart, size) => { ticks = tickLabels(size.width); },
      interaction: { mode: "index", intersect: false },
      layout: { padding: { top: BOX_H + 30, right: 10 } },
      scales: {
        y: {
          min: Math.max(0, Math.floor((lo - pad * 1.4) / 10) * 10), max: Math.ceil((hi + pad) / 10) * 10,
          title: { display: true, text: "µg/m³", color: cssVar("--muted") },
          ticks: { color: cssVar("--muted"), maxTicksLimit: 6 }, grid: { color: cssVar("--grid") }, border: { display: false },
        },
        x: {
          ticks: { autoSkip: false, maxRotation: 0, color: cssVar("--muted"), callback: (v, i) => ticks[i] },
          grid: { display: false }, border: { color: cssVar("--line") },
        },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: cssVar("--tip-bg"), titleColor: cssVar("--tip-ink"), bodyColor: cssVar("--tip-ink"),
          footerColor: cssVar("--tip-muted"), borderColor: cssVar("--line"), borderWidth: 1,
          padding: 10, cornerRadius: 8, displayColors: false, caretPadding: 8,
          titleFont: { weight: 700 }, footerFont: { weight: 400 },
          callbacks: {
            title: (items) => fmtWhen(series[items[0].dataIndex].time),
            label: (c) => `${Math.round(c.parsed.y)} µg/m³, ${aqiInfo(series[c.dataIndex].cat).name}`,
            footer: (items) => {
              const i = items[0].dataIndex;
              const mark = i === peak ? " (highest)" : i === low ? " (lowest)" : "";
              if (i === 0) return "Measured (latest 24-hour average)";
              const d = Math.round(values[i] - values[i - 1]);
              return `Forecast: ${d > 0 ? "+" : d < 0 ? "−" : "±"}${Math.abs(d)} µg/m³ from the hour before${mark}`;
            },
          },
        },
      },
    },
    plugins: [forecastDecor, hourBoxes],
  });
}

// ---------- tabs: what to do, why this forecast ----------

function adviceTab(f) {
  const chips = el("fieldset", { class: "chips" },
    el("legend", {}, "Advice for"),
    GROUPS.map((g) => el("label", {},
      el("input", { type: "radio", name: "group", value: g.id, checked: g.id === state.group ? "" : null, onchange: () => setGroup(g.id) }),
      el("span", {}, g.label))));
  if (!f.guidance) return el("div", { class: "advice" }, chips, el("p", {}, "Health advice is not available right now."));
  const g = f.guidance;
  return el("div", { class: "advice", id: "advice" },
    chips,
    el("p", { class: "advice-text" }, g.text),
    el("p", { class: "sources" }, `For ${groupText(g.group)}, at ${g.category} levels: ${g.pm25_range}. Based on: ${g.sources.join("; ")}.`));
}

function factorRow(name, v, maxAbs, extra) {
  const dir = v > 0 ? "raises" : "lowers";
  const size = Math.abs(v).toFixed(1);
  return el("li", {
    class: "factor" + (extra ? " rest" : ""), tabindex: "0",
    "data-tip": `${cap(name)}\n${cap(dir)} the forecast by ${size} µg/m³\nAverage over the next 24 hours`,
  },
    el("div", { class: "factor-name" }, cap(name), el("span", {}, `${dir} the forecast by ${size} µg/m³`)),
    el("div", { class: "factor-bar", "aria-hidden": "true" },
      el("span", { class: "mid" }),
      el("span", { class: `fill ${dir}`, style: `width:${(Math.abs(v) / maxAbs) * 50}%` })),
    el("div", { class: `factor-val ${dir}` }, `${v > 0 ? "+" : "−"}${size}`));
}

function whyTab(f) {
  if (!f.explanation || !f.explanation.length) return el("p", {}, "No explanation available.");
  const rest = f.explanation_other_ug_m3;
  const all = [...f.explanation.map((x) => x.effect_ug_m3), rest || 0];
  const maxAbs = Math.max(...all.map(Math.abs), 0.5);
  return el("div", { class: "why" },
    el("p", { class: "hint" }, "How much each input pushed this forecast up or down, averaged over the next 24 hours."),
    el("div", { class: "why-key", "aria-hidden": "true" },
      el("span", {}), el("div", { class: "key-bar" }, el("span", { class: "lowers" }, "← Lowers"), el("span", { class: "raises" }, "Raises →"))),
    el("ul", { class: "factors" },
      f.explanation.map((x) => factorRow(x.factor, x.effect_ug_m3, maxAbs)),
      rest != null ? factorRow("all other factors combined", rest, maxAbs, true) : null));
}

function notesBlock(f) {
  if (!f.warnings || !f.warnings.length) return null;
  return el("div", { class: "notes", role: "note" },
    el("strong", {}, "Notes on this forecast"),
    el("ul", {}, f.warnings.map((w) => el("li", {}, w))));
}

function renderUnavailable(f) {
  const last = state.cities.find((c) => c.city === f.city);
  const info = last ? aqiInfo(last.aqi_category) : null;
  const nearby = state.cities.filter((c) => c.region === f.region && c.city !== f.city && hasValue(c));
  return el("div", { class: "unavailable" },
    el("h3", {}, `No forecast for ${f.city} right now`),
    el("p", {}, `${cap(f.reason)}. Forecasts are made only from recent measurements, so this city is skipped until new data arrives (usually within an hour).`),
    last && hasValue(last)
      ? el("p", {}, "Last known reading: ",
          el("span", { class: "last-known", style: bandStyle(info) }, `${Math.round(last.pm25_24h)} µg/m³, ${info.name}, measured ${fmtAgo(last.age_hours * 60)}`))
      : null,
    nearby.length
      ? [el("p", {}, `Other cities in the ${f.region}:`),
          el("div", { class: "alternatives" }, nearby.map((c) =>
            el("button", { type: "button", class: "ghost", onclick: () => selectCity(c.city, true) },
              el("span", { class: "dot", style: `background:${aqiInfo(c.aqi_category).color}` }), `${c.city} ${Math.round(c.pm25_24h)}`)))]
      : null);
}

function renderForecast(f) {
  state.f = f;
  const box = document.getElementById("detail");
  box.hidden = false;
  box.classList.toggle("is-empty", !f.available);
  if (!f.available) {
    box.style.cssText = "";
    if (state.chart) { state.chart.destroy(); state.chart = null; }
    fill(box, renderUnavailable(f));
    return;
  }
  box.style.cssText = bandStyle(aqiInfo(f.current.aqi_category));
  const canvas = el("canvas", {
    role: "img", tabindex: "0",
    "aria-label": `Chart of the next 24 hours for ${f.city}: the measured value, then one point per forecast hour. Use the left and right arrow keys to read each hour.`,
  });
  fill(box, 
    el("div", { class: "detail-head" }, readingBlock(f), summaryBlock(f)),
    el("div", { class: "hours" },
      el("div", { class: "section-top" },
        el("h3", { class: "section-h" }, "Next 24 hours, hour by hour"),
        el("div", { class: "chart-legend", "aria-hidden": "true" },
          el("span", {}, el("i", { class: "lg-diamond" }), "Measured"),
          el("span", {}, el("i", { class: "lg-dot" }), "Forecast"),
          el("span", {}, el("i", { class: "lg-ring peak" }), "Peak"),
          el("span", {}, el("i", { class: "lg-ring low" }), "Low"))),
      el("div", { class: "chart-box hours-chart" }, canvas)),
    makeTabs("Advice and explanation", [
      { id: "advice", label: "What to do", panel: adviceTab(f) },
      { id: "why", label: "Why this forecast", panel: whyTab(f) },
    ], state.tab, (id) => { state.tab = id; }),
    notesBlock(f),
  );
  drawChart(canvas, f);
  hourKeys(canvas);
  document.getElementById("model-line").textContent =
    `Model ${f.model.name}, version ${f.model.version}. Forecast made ${fmtWhen(f.generated_at_utc)}` +
    (f.model_latency_ms != null ? ` in ${Math.round(f.model_latency_ms)} ms.` : ".");
}

async function loadForecast({ quiet = false } = {}) {
  if (!state.city) return;
  const box = document.getElementById("detail");
  box.setAttribute("aria-busy", "true");
  if (quiet) {
    const a = document.getElementById("advice");
    if (a) a.classList.add("updating");
  } else {
    box.hidden = false;
    if (state.chart) { state.chart.destroy(); state.chart = null; }
    fill(box, el("div", { class: "loading" },
      el("span", { class: "spinner", "aria-hidden": "true" }), `Loading the forecast for ${state.city}…`));
  }
  try {
    renderForecast(await getJSON(`/forecast/${encodeURIComponent(state.city)}?group=${state.group}`));
    showBanner("");
  } catch (e) {
    state.f = null;
    if (state.chart) { state.chart.destroy(); state.chart = null; }
    box.style.cssText = "";
    fill(box, el("div", { class: "unavailable" },
      el("h3", {}, `Could not load the forecast for ${state.city}`),
      el("p", {}, `${reason(e)}. Use Refresh to try again in a moment.`)));
  } finally {
    box.removeAttribute("aria-busy");
  }
}

async function selectCity(city, scroll = false) {
  state.city = city;
  renderCities();
  document.getElementById("city-select").value = city;
  await loadForecast();
  if (scroll) document.getElementById("detail").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function setGroup(group) {
  state.group = group;
  // The reload redraws the details box, which replaces the focused choice:
  // put the keyboard focus back on the chosen group (a keyboard user keeps going).
  const keyboard = document.activeElement && document.activeElement.name === "group";
  await loadForecast({ quiet: true });
  if (keyboard) {
    const chosen = document.querySelector(`input[name="group"][value="${group}"]`);
    if (chosen) chosen.focus({ preventScroll: true });
  }
}

async function refreshAll() {
  try {
    await loadCities();
    showBanner("");
  } catch (e) {
    showBanner(`Could not load the city list: ${reason(e)}. Use Refresh to try again in a moment.`);
    document.getElementById("asof").textContent = "No data right now";  // not "Loading…"
    return;
  }
  await loadForecast({ quiet: Boolean(state.f && state.f.city === state.city) });
}

function fillAqiTable() {
  let low = 0;
  fill(document.getElementById("aqi-rows"), 
    ...AQI.map((a) => {
      const range = a.max === Infinity ? `above ${low}` : `${low === 0 ? 0 : low + 1}–${a.max}`;
      low = a.max;
      return el("tr", {},
        el("td", {}, el("span", { class: "swatch", style: `background:${a.color}` }), a.name),
        el("td", { class: "nowrap" }, `${range} µg/m³`),
        el("td", {}, a.advice));
    }),
  );
}

document.getElementById("city-select").addEventListener("change", (e) => selectCity(e.target.value));
document.getElementById("refresh").addEventListener("click", refreshAll);
document.addEventListener("themechange", () => {  // charts read their colours when drawn
  if (state.cities.length) renderOverview();
  if (state.f && state.f.available) renderForecast(state.f);
});
setupShell();
setupOverview();
fillAqiTable();
refreshAll();
setInterval(refreshAll, REFRESH_MS);
