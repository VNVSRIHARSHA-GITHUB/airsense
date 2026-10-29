// Shared helpers for both pages: API calls, AQI categories, time formatting.

const API = window.AIRSENSE_API;

// CPCB National AQI categories for 24-hour PM2.5 (ug/m3) and their official colours.
const AQI = [
  { name: "Good", max: 30, color: "#00b050", ink: "#ffffff", advice: "Minimal impact" },
  { name: "Satisfactory", max: 60, color: "#92d050", ink: "#1d2b12", advice: "Minor breathing discomfort for sensitive people" },
  { name: "Moderate", max: 90, color: "#ffeb3b", ink: "#3a3000", advice: "Breathing discomfort for people with lung or heart disease, children and older adults" },
  { name: "Poor", max: 120, color: "#ff9900", ink: "#3a1f00", advice: "Breathing discomfort for most people on prolonged exposure" },
  { name: "Very Poor", max: 250, color: "#e8272b", ink: "#ffffff", advice: "Respiratory illness on prolonged exposure" },
  { name: "Severe", max: Infinity, color: "#8b1a1a", ink: "#ffffff", advice: "Affects healthy people; serious for those with existing disease" },
];

function aqiInfo(name) {
  return AQI.find((a) => a.name === name) || { name: "No data", color: "#c9d1d9", ink: "#33404d" };
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

const IST = "Asia/Kolkata";

function fmtIST(iso, opts) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("en-IN", { timeZone: IST, ...opts });
}

function fmtTimeIST(iso) {
  return fmtIST(iso, { weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false });
}

function fmtAgo(minutes) {
  if (minutes == null) return "—";
  if (minutes < 60) return `${Math.round(minutes)} min ago`;
  const h = minutes / 60;
  if (h < 48) return `${h.toFixed(1)} h ago`;
  return `${Math.round(h / 24)} days ago`;
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "style") node.style.cssText = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    node.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return node;
}
