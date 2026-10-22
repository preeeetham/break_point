// Shared DOM + formatting helpers. No side effects on import, no dependencies.
import { ICONS } from "./icons.js";

export const SVG_NS = "http://www.w3.org/2000/svg";

/** el("div", {class:"x", onclick:fn, dataset:{k:1}}, child, "text") */
export function el(tag, props = null, ...children) {
  const node = document.createElement(tag);
  applyProps(node, props);
  append(node, children);
  return node;
}

export function svg(tag, props = null, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") node.setAttribute("class", v);
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v);
    }
  }
  append(node, children);
  return node;
}

function applyProps(node, props) {
  if (!props) return;
  for (const [k, v] of Object.entries(props)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = String(v);
    else if (k === "html") throw new Error("raw html is not allowed; build nodes instead");
    else if (k === "dataset") Object.assign(node.dataset, v);
    else if (k === "style" && typeof v === "object") {
      // setProperty is required for custom properties; Object.assign silently drops them
      for (const [prop, val] of Object.entries(v)) {
        if (prop.startsWith("--")) node.style.setProperty(prop, String(val));
        else node.style[prop] = val;
      }
    }
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (k === "disabled" || k === "checked" || k === "selected") node[k] = Boolean(v);
    else node.setAttribute(k, v === true ? "" : String(v));
  }
}

function append(node, children) {
  for (const c of children.flat(4)) {
    if (c === null || c === undefined || c === false || c === true) continue;
    node.appendChild(typeof c === "object" && c.nodeType ? c : document.createTextNode(String(c)));
  }
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export function cls(...parts) {
  return parts.flat().filter(Boolean).join(" ");
}

/** Inline SVG icon from icons.js. Returns an <svg> element. */
export function svgIcon(name, size = 16, extra = {}) {
  const d = ICONS[name] || ICONS.info;
  const node = svg("svg", {
    class: cls("icon", extra.class),
    width: size, height: size, viewBox: "0 0 24 24", fill: "none",
    stroke: "currentColor", "stroke-width": extra.weight || 1.75,
    "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true", focusable: "false",
  });
  node.appendChild(svg("g", null, ...d.map((spec) => svg(spec[0], spec[1]))));
  return node;
}

// ---------------------------------------------------------------- formatting
export function fmtPct(v, digits = 0) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `${(v * 100).toFixed(digits)}%`;
}

export function fmtMs(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  if (v >= 10000) return `${(v / 1000).toFixed(1)} s`;
  if (v >= 1000) return `${(v / 1000).toFixed(2)} s`;
  return `${Math.round(v)} ms`;
}

export function fmtDur(s) {
  if (s === null || s === undefined || Number.isNaN(s)) return "—";
  if (s < 10) return `${s.toFixed(1)}s`;
  if (s < 90) return `${Math.round(s)}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${Math.round(s % 60)}s`;
}

export function fmtNum(v, digits = 1) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return Number.isInteger(v) ? String(v) : v.toFixed(digits);
}

export function relTime(iso) {
  if (!iso) return "—";
  const t = typeof iso === "number" ? iso * 1000 : Date.parse(iso);
  if (Number.isNaN(t)) return "—";
  const diff = (Date.now() - t) / 1000;
  if (diff < 10) return "just now";
  if (diff < 60) return `${Math.round(diff)}s ago`;
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)}h ago`;
  return new Date(t).toLocaleDateString();
}

export function clockTime(iso) {
  if (!iso) return "";
  const t = typeof iso === "number" ? iso * 1000 : Date.parse(iso);
  if (Number.isNaN(t)) return "";
  return new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}

export function titleCase(s) {
  return String(s || "").replace(/[_-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

export function shortId(id) {
  return String(id || "").slice(0, 8);
}

// ---------------------------------------------------------------- semantics
/** Canonical status -> token colour name. Keep in sync with docs/UI_CONTRACT.md §1. */
export function statusTone(status) {
  const s = String(status || "").toLowerCase();
  if (["healthy", "up", "running", "recovered", "completed", "ok", "held", "true", "done", "success"].includes(s)) return "ok";
  if (["degraded", "warn", "warning", "soaking", "starting", "queued", "unknown", "skipped"].includes(s)) return "warn";
  if (["failing", "down", "exited", "aborted", "violated", "false", "error", "failed", "unhealthy", "dead"].includes(s)) return "danger";
  if (["critical", "data_loss"].includes(s)) return "critical";
  if (["injecting", "chaos", "working", "running_experiment"].includes(s)) return "chaos";
  return "neutral";
}

export const SEVERITY_ORDER = { critical: 0, high: 1, medium: 2, low: 3, info: 4 };

export function severityTone(sev) {
  const s = String(sev || "info").toLowerCase();
  return { critical: "critical", high: "danger", medium: "warn", low: "cyan", info: "neutral" }[s] || "neutral";
}

export function riskTone(level) {
  return { critical: "critical", high: "danger", medium: "warn", low: "ok" }[String(level || "").toLowerCase()] || "neutral";
}

export function gradeTone(grade) {
  return { A: "ok", B: "ok", C: "warn", D: "danger", F: "critical" }[String(grade || "").trim().toUpperCase()] || "neutral";
}

export function scoreTone(score) {
  if (score === null || score === undefined) return "neutral";
  if (score >= 80) return "ok";
  if (score >= 60) return "warn";
  return "danger";
}

/** Icon name for a service role. */
export function roleIcon(role) {
  return { datastore: "database", proxy: "globe", ui: "layout", app: "server", other: "cpu" }[role] || "server";
}

// ---------------------------------------------------------------- misc
let liveRegion = null;
/** Announce a status change once, politely, without moving focus. */
export function announce(text) {
  if (!liveRegion) {
    liveRegion = el("div", { class: "sr-only", role: "status", "aria-live": "polite", "aria-atomic": "true" });
    document.body.appendChild(liveRegion);
  }
  liveRegion.textContent = "";
  window.setTimeout(() => { liveRegion.textContent = text; }, 40);
}

export const reduceMotion = () =>
  window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

export function debounce(fn, ms = 150) {
  let t = 0;
  return (...a) => { clearTimeout(t); t = window.setTimeout(() => fn(...a), ms); };
}

export function clamp(v, lo, hi) {
  return Math.min(hi, Math.max(lo, v));
}

/** Stable JSON fingerprint so update() can skip identical re-renders. */
export function hash(obj) {
  const s = JSON.stringify(obj) || "";
  let h = 5381;
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
  return String(h);
}

export async function postJSON(url, body = {}) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  let data = null;
  try { data = await res.json(); } catch { data = null; }
  if (!res.ok) throw Object.assign(new Error((data && data.error) || `HTTP ${res.status}`), { status: res.status, data });
  return data;
}

export async function getJSON(url) {
  const res = await fetch(url, { headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}
