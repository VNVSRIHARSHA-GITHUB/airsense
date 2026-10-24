// Forecast page: city tiles from /cities, details from /forecast/{city}?group=...

const REFRESH_MS = 10 * 60 * 1000;
const GROUP_TEXT = {
  general: "the general public",
  sensitive: "children, older adults and people with heart or lung conditions",
  outdoor_workers: "people who work or exercise outdoors",
};

// Shown next to the city name as a hint; values are per city (mean of its own stations).
const STATE_OF = {
  Delhi: "Delhi", Lucknow: "Uttar Pradesh", Patna: "Bihar", Kanpur: "Uttar Pradesh",
  Jaipur: "Rajasthan", Chandigarh: "Chandigarh", Mumbai: "Maharashtra", Ahmedabad: "Gujarat",
  Pune: "Maharashtra", Bengaluru: "Karnataka", Chennai: "Tamil Nadu", Hyderabad: "Telangana",
  Kolkata: "West Bengal", Guwahati: "Assam",
};
const REGIONS = ["North", "West", "South", "East"];

const state = { city: null, group: "general", cities: [], chart: null };

function cityLabel(name) {
  const st = STATE_OF[name];
  return st && st !== name ? `${name} (${st})` : name;
}

function showBanner(text) {
  const b = document.getElementById("banner");
  b.textContent = text;
  b.hidden = !text;
}

// ---------- city tiles ----------

function renderCities() {
  const box = document.getElementById("cities");
  box.replaceChildren(
    ...state.cities.map((c) => {
      const info = aqiInfo(c.aqi_category);
      const stale = c.pm25_24h == null || (c.age_hours != null && c.age_hours > 4);
      return el(
        "button",
        {
          class: "city" + (stale ? " stale" : ""),
          type: "button",
          role: "listitem",
          "aria-pressed": String(c.city === state.city),
          style: `--band:${info.color}`,
          onclick: () => selectCity(c.city),
          title: c.age_hours != null ? `Measured ${c.age_hours} h ago` : "No recent measurement",
        },
        el("span", { class: "name" }, c.city),
        el(
          "span",
          { class: "val" },
          c.pm25_24h == null ? "—" : Math.round(c.pm25_24h),
          c.pm25_24h == null ? null : el("small", {}, "µg/m³"),
        ),
        el("span", { class: "cat" }, c.pm25_24h == null ? "No recent data" : info.name),
      );
    }),
  );
}

async function loadCities() {
  state.cities = await getJSON("/cities");
  const fresh = state.cities.filter((c) => c.age_hours != null);
  const newest = fresh.length ? Math.min(...fresh.map((c) => c.age_hours)) : null;
  document.getElementById("asof").textContent =
    newest == null ? "No recent measurements" : `Latest measurement ${fmtAgo(newest * 60)}`;
  if (!state.city) {
    const first = state.cities.find((c) => c.pm25_24h != null) || state.cities[0];
    state.city = first ? first.city : null;
  }
  renderCities();
  renderCitySelect();
  renderOverview();
}

// ---------- phone: city dropdown ----------

function renderCitySelect() {
  const sel = document.getElementById("city-select");
  sel.replaceChildren(
    ...REGIONS.map((region) =>
      el(
        "optgroup",
        { label: region },
        state.cities
          .filter((c) => c.region === region)
          .map((c) =>
            el("option", { value: c.city, selected: c.city === state.city ? "selected" : null }, cityLabel(c.city)),
          ),
      ),
    ),
  );
  sel.value = state.city;
}

// ---------- across India ----------

function renderOverview() {
  const box = document.getElementById("overview");
  const known = state.cities.filter((c) => c.pm25_24h != null).sort((a, b) => b.pm25_24h - a.pm25_24h);
  if (!known.length) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  const worst = known[0];
  const best = known[known.length - 1];
  const missing = state.cities.length - known.length;
  document.getElementById("overview-text").textContent =
    `Most polluted: ${worst.city}, ${Math.round(worst.pm25_24h)} µg/m³ (${worst.aqi_category}). ` +
    `Cleanest: ${best.city}, ${Math.round(best.pm25_24h)} µg/m³ (${best.aqi_category}).` +
    (missing ? ` No recent data for ${missing} ${missing === 1 ? "city" : "cities"}.` : "");

  const counts = AQI.map((a) => ({ ...a, n: known.filter((c) => c.aqi_category === a.name).length })).filter((a) => a.n);
  const bar = document.getElementById("overview-bar");
  bar.setAttribute("aria-label", counts.map((a) => `${a.n} ${a.name}`).join(", "));
  bar.replaceChildren(...counts.map((a) => el("span", { style: `background:${a.color};flex:${a.n}`, title: `${a.n} ${a.name}` })));
  const legend = el("div", { class: "catbar-legend" },
    counts.map((a) => el("span", {}, el("span", { class: "swatch", style: `background:${a.color}` }), `${a.n} ${a.name}`)));
  const old = box.querySelector(".catbar-legend");
  if (old) old.replaceWith(legend); else bar.after(legend);

  document.getElementById("overview-list").replaceChildren(
    ...[...known, ...state.cities.filter((c) => c.pm25_24h == null)].map((c) => {
      const info = aqiInfo(c.aqi_category);
      return el("li", {},
        el("button", { type: "button", onclick: () => selectCity(c.city, true) },
          el("span", { class: "dot", style: `--band:${info.color}` }),
          el("span", {}, c.city, el("span", { class: "rs" }, c.pm25_24h == null ? "No recent data" : info.name)),
          el("span", { class: "rv" }, c.pm25_24h == null ? "—" : Math.round(c.pm25_24h))));
    }),
  );
}

// ---------- detail ----------

function readingBlock(f) {
  const info = aqiInfo(f.current.aqi_category);
  return el(
    "div",
    { class: "reading", style: `--band:${info.color};--band-ink:${info.ink}` },
    el("p", { class: "where" }, `${f.city} now`),
    el("p", { class: "big" }, Math.round(f.current.pm25_24h), el("small", {}, "µg/m³")),
    el("p", { class: "catname" }, info.name),
    el(
      "p",
      { class: "meta" },
      `24-hour average as of ${f.current.time_ist} IST, from ${f.current.stations} stations, ` +
        `measured ${fmtAgo(f.current.age_hours * 60)}`,
    ),
  );
}

function summaryBlock(f) {
  const w = f.worst_next_24h;
  const now = aqiInfo(f.current.aqi_category);
  const worst = aqiInfo(w.aqi_category);
  const last = f.forecast[f.forecast.length - 1];
  const trend =
    last.pm25 > f.current.pm25_24h + 5 ? "rising" : last.pm25 < f.current.pm25_24h - 5 ? "falling" : "about the same";
  const headline =
    worst.name === now.name
      ? `Expected to stay ${worst.name} for the next 24 hours`
      : `Expected to reach ${worst.name} in the next 24 hours`;
  return el(
    "div",
    { class: "summary" },
    el("h3", {}, headline),
    el("p", {}, `Highest: ${Math.round(w.pm25)} µg/m³ around ${w.time_ist} IST. ` +
      `By ${last.time_ist} IST the 24-hour average is expected to be ${trend} (${Math.round(last.pm25)} µg/m³).`),
    el("p", {}, worst.advice + "."),
  );
}

function stripBlock(f) {
  const worstTime = f.worst_next_24h.time_ist;
  const cells = f.forecast.map((h) => {
    const info = aqiInfo(h.aqi_category);
    return el("div", {
      class: "cell" + (h.time_ist === worstTime ? " worst" : ""),
      style: `--band:${info.color}`,
      title: `${h.time_ist} IST: ${h.pm25} µg/m³ (${info.name})`,
      role: "img",
      "aria-label": `${h.time_ist}: ${info.name}`,
    });
  });
  const hours = f.forecast.map((h, i) =>
    el("span", {}, i % 3 === 0 ? fmtIST(h.time_utc, { hour: "2-digit", hour12: false }) + "h" : ""),
  );
  return el(
    "div",
    { class: "strip-wrap" },
    el("h4", { class: "section-h" }, "Next 24 hours, hour by hour"),
    el("div", { class: "strip" }, cells),
    el("div", { class: "strip-hours", "aria-hidden": "true" }, hours),
  );
}

// Background bands for the AQI categories behind the line chart.
const aqiBands = {
  id: "aqiBands",
  beforeDraw(chart) {
    const { ctx, chartArea: a, scales: { y } } = chart;
    let low = 0;
    for (const band of AQI) {
      const top = Math.min(band.max, y.max);
      if (top > y.min && low < y.max) {
        const y1 = y.getPixelForValue(Math.max(low, y.min));
        const y2 = y.getPixelForValue(top);
        ctx.save();
        ctx.fillStyle = band.color + "33";
        ctx.fillRect(a.left, y2, a.right - a.left, y1 - y2);
        ctx.restore();
      }
      low = band.max;
    }
  },
};

function drawChart(canvas, f) {
  if (state.chart) state.chart.destroy();
  const values = f.forecast.map((h) => h.pm25);
  const lo = Math.min(...values, f.current.pm25_24h);
  const hi = Math.max(...values, f.current.pm25_24h);
  const pad = Math.max(10, (hi - lo) * 0.6);
  state.chart = new Chart(canvas, {
    type: "line",
    data: {
      labels: ["now", ...f.forecast.map((h) => fmtIST(h.time_utc, { hour: "2-digit", minute: "2-digit", hour12: false }))],
      datasets: [
        {
          label: "24-hour average PM2.5 (µg/m³)",
          data: [f.current.pm25_24h, ...values],
          borderColor: "#1d2733",
          borderWidth: 2,
          pointRadius: 3,
          pointBackgroundColor: [aqiInfo(f.current.aqi_category).color, ...f.forecast.map((h) => aqiInfo(h.aqi_category).color)],
          pointBorderColor: "#1d2733",
          tension: 0.25,
        },
      ],
    },
    options: {
      maintainAspectRatio: false,
      animation: false,
      scales: {
        y: { min: Math.max(0, Math.floor(lo - pad)), max: Math.ceil(hi + pad), title: { display: true, text: "µg/m³" } },
        x: { ticks: { maxRotation: 0, autoSkipPadding: 12 } },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (c) => {
              const cat = c.dataIndex === 0 ? f.current.aqi_category : f.forecast[c.dataIndex - 1].aqi_category;
              return ` ${c.parsed.y} µg/m³ (${cat})`;
            },
          },
        },
      },
    },
    plugins: [aqiBands],
  });
}

function factorsBlock(f) {
  if (!f.explanation.length) return el("p", {}, "No explanation available.");
  const maxAbs = Math.max(...f.explanation.map((x) => Math.abs(x.effect_ug_m3)), 1);
  return el(
    "ul",
    { class: "factors" },
    f.explanation.map((x) => {
      const width = (Math.abs(x.effect_ug_m3) / maxAbs) * 50;
      const sign = x.effect_ug_m3 > 0 ? "+" : "−";
      return el(
        "li",
        {},
        el("div", {}, x.factor.charAt(0).toUpperCase() + x.factor.slice(1),
          el("div", { class: "factor-num" }, `${x.direction} the forecast by ${sign}${Math.abs(x.effect_ug_m3).toFixed(1)} µg/m³`)),
        el("div", { class: "factor-bar", "aria-hidden": "true" },
          el("span", { class: "mid" }),
          el("span", { class: `fill ${x.direction}`, style: `width:${width}%` })),
      );
    }),
  );
}

function adviceBlock(f) {
  if (!f.guidance) return el("p", {}, "Health advice is not available right now.");
  const g = f.guidance;
  return el(
    "div",
    { class: "advice" },
    el("p", {}, g.text),
    el("p", { class: "sources" }, `For ${GROUP_TEXT[g.group]}, at ${g.category} levels (${g.pm25_range}). Based on: ${g.sources.join("; ")}.`),
  );
}

function notesBlock(f) {
  if (!f.warnings || !f.warnings.length) return null;
  return el(
    "div",
    { class: "notes" },
    el("strong", {}, "Notes on this forecast"),
    el("ul", {}, f.warnings.map((w) => el("li", {}, w))),
  );
}

function renderUnavailable(f) {
  const last = state.cities.find((c) => c.city === f.city);
  const info = last ? aqiInfo(last.aqi_category) : null;
  const nearby = state.cities.filter((c) => c.region === f.region && c.city !== f.city && c.pm25_24h != null);
  return el(
    "div",
    { class: "unavailable" },
    el("h3", {}, `No forecast for ${f.city} right now`),
    el("p", {}, `Reason: ${f.reason}. Forecasts are made only from recent measurements, so this city is skipped until new data arrives (usually within an hour).`),
    last && last.pm25_24h != null
      ? [
          el("p", {}, "Last known reading:"),
          el("span", { class: "last-known", style: `--band:${info.color};--band-ink:${info.ink}` },
            `${Math.round(last.pm25_24h)} µg/m³, ${info.name}, measured ${fmtAgo(last.age_hours * 60)}`),
          el("p", {}, `${info.name} means: ${info.advice.toLowerCase()}.`),
        ]
      : el("p", {}, "There is no recent measurement for this city."),
    nearby.length
      ? [
          el("p", {}, `Other cities in the ${f.region}:`),
          el("div", { class: "alternatives" },
            nearby.map((c) =>
              el("button", { type: "button", onclick: () => selectCity(c.city, true) },
                el("span", { class: "dot", style: `--band:${aqiInfo(c.aqi_category).color}` }),
                `${c.city} ${Math.round(c.pm25_24h)}`))),
        ]
      : null,
  );
}

function renderForecast(f) {
  const box = document.getElementById("detail");
  box.hidden = false;
  if (!f.available) {
    box.replaceChildren(renderUnavailable(f));
    return;
  }
  const canvas = el("canvas", { role: "img", "aria-label": `Line chart of the forecast for ${f.city}` });
  box.replaceChildren(
    el("div", { class: "detail-head" }, readingBlock(f), summaryBlock(f)),
    stripBlock(f),
    el("div", { class: "chart-box" }, canvas),
    el(
      "div",
      { class: "columns" },
      el("section", {}, el("h4", {}, `What to do: advice for ${GROUP_TEXT[f.guidance ? f.guidance.group : state.group]}`), adviceBlock(f)),
      el("section", {}, el("h4", {}, "Why this forecast"), factorsBlock(f)),
    ),
    notesBlock(f),
  );
  drawChart(canvas, f);
  document.getElementById("model-line").textContent =
    `Model ${f.model.name} version ${f.model.version}. Generated ${fmtTimeIST(f.generated_at_utc)} IST` +
    (f.model_latency_ms != null ? ` in ${Math.round(f.model_latency_ms)} ms.` : ".");
}

async function loadForecast() {
  if (!state.city) return;
  const box = document.getElementById("detail");
  box.setAttribute("aria-busy", "true");
  box.hidden = false;
  if (state.chart) { state.chart.destroy(); state.chart = null; }
  box.replaceChildren(
    el("div", { class: "loading" },
      el("span", { class: "spinner", "aria-hidden": "true" }),
      `Loading the forecast for ${state.city}…`),
  );
  try {
    const f = await getJSON(`/forecast/${encodeURIComponent(state.city)}?group=${state.group}`);
    renderForecast(f);
    showBanner("");
  } catch (e) {
    box.hidden = false;
    box.replaceChildren(
      el("div", { class: "unavailable" },
        el("h3", {}, `Could not load the forecast for ${state.city}`),
        el("p", {}, `${e.message}. Use Refresh to try again.`)),
    );
  } finally {
    box.removeAttribute("aria-busy");
  }
}

async function selectCity(city, scroll = false) {
  state.city = city;
  renderCities();
  document.getElementById("city-select").value = city;
  await loadForecast();
  if (scroll) document.getElementById("detail").scrollIntoView({ block: "start" });
}

async function refreshAll() {
  try {
    await loadCities();
    showBanner("");
  } catch (e) {
    showBanner(`The forecast service at ${API} cannot be reached (${e.message}). Check that the backend is running, then use Refresh.`);
    return;
  }
  await loadForecast();
}

function fillAqiTable() {
  let low = 0;
  document.getElementById("aqi-rows").replaceChildren(
    ...AQI.map((a) => {
      const range = a.max === Infinity ? `above ${low}` : `${low === 0 ? 0 : low + 1}–${a.max}`;
      low = a.max;
      return el("tr", {},
        el("td", {}, el("span", { class: "swatch", style: `background:${a.color}` }), a.name),
        el("td", {}, `${range} µg/m³`),
        el("td", {}, a.advice));
    }),
  );
}

function setGroup(group) {
  state.group = group;
  document.getElementById("group-select").value = group; // keep phone and desktop controls in step
  document.querySelectorAll('input[name="group"]').forEach((r) => (r.checked = r.value === group));
  loadForecast();
}

document.querySelectorAll('input[name="group"]').forEach((r) =>
  r.addEventListener("change", (e) => setGroup(e.target.value)),
);
document.getElementById("group-select").addEventListener("change", (e) => setGroup(e.target.value));
document.getElementById("city-select").addEventListener("change", (e) => selectCity(e.target.value));
document.getElementById("refresh").addEventListener("click", refreshAll);
fillAqiTable();
refreshAll();
setInterval(refreshAll, REFRESH_MS);
