// Shared helpers for both pages: API calls, AQI categories, one time format,
// light/dark theme, the page menu, hover tips and tabs.

const API = window.AIRSENSE_API;

// CPCB National AQI categories for 24-hour PM2.5 (ug/m3) and their official colours.
// ink: text colour on that colour (WCAG AA); chip: a backing for small text on it.
const AQI = [
  { name: "Good", max: 30, color: "#00b050", ink: "#06240f", advice: "Minimal impact" },
  { name: "Satisfactory", max: 60, color: "#92d050", ink: "#1d2b12", advice: "Minor breathing discomfort for sensitive people" },
  { name: "Moderate", max: 90, color: "#ffeb3b", ink: "#3a3000", advice: "Breathing discomfort for people with lung or heart disease, children and older adults" },
  { name: "Poor", max: 120, color: "#ff9900", ink: "#3a1f00", advice: "Breathing discomfort for most people on prolonged exposure" },
  { name: "Very Poor", max: 250, color: "#e8272b", ink: "#ffffff", advice: "Respiratory illness on prolonged exposure" },
  { name: "Severe", max: Infinity, color: "#8b1a1a", ink: "#ffffff", advice: "Affects healthy people; serious for those with existing disease" },
];
const NO_DATA = { name: "No data", max: null, color: "#9aa7b4", ink: "#1b2530", advice: "" };

function aqiInfo(name) {
  return AQI.find((a) => a.name === name) || NO_DATA;
}

// CSS variables for one AQI band: colour, text colour, and a backing for small text.
function bandStyle(info) {
  const chip = info.ink === "#ffffff" ? "rgba(0,0,0,0.22)" : "rgba(255,255,255,0.4)";
  return `--band:${info.color};--band-ink:${info.ink};--band-chip:${chip}`;
}

async function getJSON(path) {
  let r;
  try {
    r = await fetch(API + path, { headers: { Accept: "application/json" } });
  } catch (_) {  // nothing answered at all (laptop run with the backend stopped)
    throw new Error("The AirSense service cannot be reached");
  }
  let body = null;
  try { body = await r.json(); } catch (_) { /* not JSON */ }
  if (!r.ok) {
    const detail = body && body.detail ? body.detail : `HTTP ${r.status}`;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return body;
}

// The reason from an error, without a trailing full stop, to put inside a sentence.
function reason(e) {
  return String(e && e.message ? e.message : e).replace(/[.\s]+$/, "");
}

// ---------- one time format for both pages: "Mon 2 Nov, 11:00 IST (2 h ago)" ----------

const IST = "Asia/Kolkata";
const PARTS = new Intl.DateTimeFormat("en-GB", {
  timeZone: IST, weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
});

function istParts(when) {
  const d = when instanceof Date ? when : new Date(when);
  const p = Object.fromEntries(PARTS.formatToParts(d).map((x) => [x.type, x.value]));
  return { day: `${p.weekday} ${p.day} ${p.month}`, clock: `${p.hour}:${p.minute}` };
}
const fmtDay = (when) => istParts(when).day;                 // "Mon 2 Nov"
const fmtClock = (when) => istParts(when).clock;             // "11:00"
function fmtWhen(when) {                                     // "Mon 2 Nov, 11:00 IST"
  if (when == null) return "—";
  const p = istParts(when);
  return `${p.day}, ${p.clock} IST`;
}

function fmtAgo(minutes) {
  if (minutes == null || Number.isNaN(minutes)) return "—";
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${Math.round(minutes)} min ago`;
  const h = minutes / 60;
  if (h < 48) return `${Math.round(h)} h ago`;
  return `${Math.round(h / 24)} days ago`;
}
const minutesSince = (when) => (Date.now() - new Date(when).getTime()) / 60000;
const fmtWhenAgo = (when) => (when == null ? "—" : `${fmtWhen(when)} (${fmtAgo(minutesSince(when))})`);

function fmtDuration(minutes) {
  if (minutes == null) return "—";
  if (minutes < 1) return `${Math.max(1, Math.round(minutes * 60))} s`;
  if (minutes < 90) return `${Math.round(minutes)} min`;
  return `${(minutes / 60).toFixed(1).replace(/\.0$/, "")} h`;
}

const NUM = new Intl.NumberFormat("en-US");
const fmtNum = (x) => NUM.format(Math.round(x));             // 346,101

// ---------- DOM ----------

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "style") node.style.cssText = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false || c === "") continue;
    node.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return node;
}

function svgIcon(name) {
  const paths = {
    sun: '<circle cx="12" cy="12" r="4.2"/><path d="M12 2.5v2.2M12 19.3v2.2M2.5 12h2.2M19.3 12h2.2M5.3 5.3l1.6 1.6M17.1 17.1l1.6 1.6M5.3 18.7l1.6-1.6M17.1 6.9l1.6-1.6"/>',
    moon: '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>',
    menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
    refresh: '<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/>',
    external: '<path d="M14 4h6v6M20 4l-9 9"/><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
  };
  const span = document.createElement("span");
  span.className = "icon";
  span.setAttribute("aria-hidden", "true");
  span.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${paths[name]}</svg>`;
  return span;
}

// ---------- theme (light / dark) ----------

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function currentTheme() {
  return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
}

function setupTheme() {
  const btn = document.getElementById("theme-toggle");
  if (!btn) return;
  const paint = () => {
    const dark = currentTheme() === "dark";
    btn.replaceChildren(svgIcon(dark ? "sun" : "moon"));
    btn.setAttribute("aria-label", dark ? "Switch to light theme" : "Switch to dark theme");
    btn.dataset.tip = dark ? "Light theme" : "Dark theme";
  };
  btn.addEventListener("click", () => {
    const next = currentTheme() === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("airsense-theme", next); } catch (_) { /* private window */ }
    paint();
    document.dispatchEvent(new CustomEvent("themechange"));
  });
  paint();
}

// ---------- page menu (phone) ----------

function setupMenu() {
  const btn = document.getElementById("menu-toggle");
  const nav = document.getElementById("topnav");
  if (!btn || !nav) return;
  btn.replaceChildren(svgIcon("menu"));
  const close = () => { nav.classList.remove("open"); btn.setAttribute("aria-expanded", "false"); };
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    const open = !nav.classList.contains("open");
    nav.classList.toggle("open", open);
    btn.setAttribute("aria-expanded", String(open));
    if (open) nav.querySelector("a").focus();
  });
  document.addEventListener("click", (e) => { if (!nav.contains(e.target)) close(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
}

// ---------- hover tips: any element with data-tip (lines separated by \n) ----------

function setupTips() {
  const tip = el("div", { class: "tip", role: "tooltip", hidden: "" });
  document.body.append(tip);
  let owner = null;
  const show = (target) => {
    owner = target;
    const [head, ...rest] = target.dataset.tip.split("\n");
    tip.replaceChildren(el("strong", {}, head), ...rest.map((line) => el("span", {}, line)));
    tip.hidden = false;
    const r = target.getBoundingClientRect();
    const t = tip.getBoundingClientRect();
    let x = r.left + r.width / 2 - t.width / 2;
    x = Math.max(8, Math.min(x, window.innerWidth - t.width - 8));
    let y = r.top - t.height - 8;
    if (y < 8) y = r.bottom + 8;
    tip.style.transform = `translate(${Math.round(x + window.scrollX)}px, ${Math.round(y + window.scrollY)}px)`;
  };
  const hide = () => { owner = null; tip.hidden = true; };
  const find = (e) => (e.target.closest ? e.target.closest("[data-tip]") : null);
  document.addEventListener("pointerover", (e) => { const t = find(e); if (t && t !== owner) show(t); else if (!t) hide(); });
  document.addEventListener("focusin", (e) => { const t = find(e); if (t) show(t); });
  document.addEventListener("focusout", hide);
  window.addEventListener("scroll", hide, { passive: true });
}

// ---------- tabs ----------
// makeTabs(id, [{ id, label, panel: Node }], selected, onChange) -> element

function makeTabs(name, tabs, selected, onChange) {
  const list = el("div", { class: "tabs", role: "tablist", "aria-label": name });
  const panels = [];
  const buttons = tabs.map((t) => {
    const b = el("button", {
      type: "button", role: "tab", id: `tab-${t.id}`, "aria-controls": `panel-${t.id}`,
      "aria-selected": String(t.id === selected), tabindex: t.id === selected ? "0" : "-1",
    }, t.label, t.badge ? el("span", { class: "tab-badge" }, t.badge) : null);
    const p = el("div", { class: "tab-panel", role: "tabpanel", id: `panel-${t.id}`, "aria-labelledby": `tab-${t.id}`, tabindex: "0" }, t.panel);
    p.hidden = t.id !== selected;
    panels.push(p);
    return b;
  });
  const pick = (i, focus) => {
    buttons.forEach((b, j) => {
      b.setAttribute("aria-selected", String(i === j));
      b.tabIndex = i === j ? 0 : -1;
      panels[j].hidden = i !== j;
    });
    if (focus) buttons[i].focus();
    if (onChange) onChange(tabs[i].id);
  };
  buttons.forEach((b, i) => {
    b.addEventListener("click", () => pick(i, false));
    b.addEventListener("keydown", (e) => {
      const n = buttons.length;
      const to = { ArrowRight: (i + 1) % n, ArrowLeft: (i - 1 + n) % n, Home: 0, End: n - 1 }[e.key];
      if (to != null) { e.preventDefault(); pick(to, true); }
    });
  });
  list.append(...buttons);
  const box = el("div", { class: "tabset" }, list, ...panels);
  box.selectTab = (id) => { const i = tabs.findIndex((t) => t.id === id); if (i >= 0) pick(i, false); };
  return box;
}

function showBanner(text) {
  const b = document.getElementById("banner");
  b.textContent = text;
  b.hidden = !text;
}

function setupShell() {
  setupTheme();
  setupMenu();
  setupTips();
  const r = document.getElementById("refresh");
  if (r) r.prepend(svgIcon("refresh"));
}
