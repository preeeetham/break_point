// topology.js — the main stage: live service dependency graph.
// Deterministic layered layout (no force sim), built once per topology fingerprint;
// every poll only mutates classes/attributes/text so animations never restart.
import {
  el, svg, clear, cls, svgIcon, statusTone, roleIcon,
  fmtPct, fmtMs, titleCase, reduceMotion, hash,
} from "./util.js";
import { CASCADE_MECHANISM, cascadeConfidence } from "./copy.js";

const P = "fl-topo-";
// Last-resort values if tokens.css failed to load. Mirrors docs/UI_CONTRACT.md §1 —
// used only as low-priority SVG presentation attributes / var() fallbacks.
const FALLBACK = {
  surface: "#0E1424", "surface-2": "#141C30", "surface-3": "#1B2540", border: "#1E2A44", "border-strong": "#2C3C5E",
  text: "#E8EDF7", "text-dim": "#A8B6D1", "text-mute": "#6F7F9F",
  blue: "#4D8DFF", cyan: "#22D3EE", violet: "#A78BFA",
  ok: "#2DD36F", warn: "#F5A524", danger: "#FF4D4D", critical: "#F43F5E", chaos: "#FF7A1A",
};

let TOK = null;
/** Read a design token once and cache it; falls back to the documented literal. */
function tok(name) {
  if (!TOK) {
    TOK = {};
    const cs = typeof getComputedStyle === "function" ? getComputedStyle(document.documentElement) : null;
    for (const k of Object.keys(FALLBACK)) {
      const v = cs ? String(cs.getPropertyValue(`--${k}`) || "").trim() : "";
      TOK[k] = v || FALLBACK[k];
    }
  }
  return TOK[name] || FALLBACK[name] || "currentColor";
}

// ---------------------------------------------------------------- geometry constants
const PAD = 34, ROW_PITCH = 128, COL_GAP = 44, OBS_GAP = 58;
const NODE_MIN_W = 150, NODE_H = 60, W_BOOST = 26, H_BOOST = 14;
const CHAR_W = 8.4, NAME_X = 40, NAME_PAD = 16;
const OBS_W = 170, OBS_H = 48, OBS_PITCH = 76;
const ROW_MAX = 6;            // wrap wider rows so arbitrary compose projects stay readable
const HOP_CAP = 32;           // max bulge for same-row arcs, keeps them inside the row gap
const SLOT_SPREAD = 0.68;     // fraction of a node edge used to fan out attach points

const KINDS = {
  env:        { label: "env var (hidden dependency)", color: "blue",      width: 3.0, dash: null,     halo: true },
  proxy:      { label: "proxy route",                 color: "cyan",      width: 2.2, dash: null,     halo: false },
  observed:   { label: "observed traffic (live)",     color: "cyan",      width: 2.4, dash: "7 7",    halo: false, flow: true },
  depends_on: { label: "compose depends_on",          color: "text-mute", width: 1.6, dash: "6 5",    halo: false },
  documented: { label: "documented (architecture)",   color: "text-mute", width: 1.5, dash: "1.5 5",  halo: false },
};
const KIND_ORDER = ["env", "proxy", "observed", "depends_on", "documented"];
const TONES = { ok: "check", warn: "alert", danger: "x", critical: "alert", chaos: "crosshair", neutral: "info" };

// ---------------------------------------------------------------- layout (pure, testable)
const scoreOf = (t, n) => {
  const row = (t.criticality || []).find((c) => c.service === n);
  if (row && typeof row.score === "number") return row.score;
  const s = (t.services || {})[n];
  return (s && typeof s.criticality === "number") ? s.criticality : 0;
};

function nodeSize(name, score, lo, hi, obs) {
  if (obs) return { w: OBS_W, h: OBS_H };
  const norm = hi > lo ? (score - lo) / (hi - lo) : 0;
  const textW = String(name).length * CHAR_W;
  return {
    w: Math.round(Math.max(NODE_MIN_W, NAME_X + textW + NAME_PAD) + W_BOOST * norm),
    h: Math.round(NODE_H + H_BOOST * norm),
  };
}

/** Role-bucketed rows (proxy/ui → gateway → cluster → datastore), compacted and wrapped. */
function assignRows(services, edges, main) {
  const bucket = new Map();
  const inMain = new Set(main);
  const entryish = new Set(main.filter((n) => ["proxy", "ui"].includes(services[n].role)));
  // an app that an entry-layer node depends on is the gateway tier
  const tier1 = new Set();
  for (const e of edges) {
    if (entryish.has(e.src) && inMain.has(e.dst) && services[e.dst].role === "app") tier1.add(e.dst);
  }
  for (const n of main) {
    const role = services[n].role;
    if (entryish.has(n)) bucket.set(n, 0);
    else if (role === "datastore") bucket.set(n, 3);
    else if (role === "app") bucket.set(n, tier1.has(n) ? 1 : 2);
    else bucket.set(n, 2);
  }
  // compact unused row indices so a proxy-less project still starts at row 0
  const used = [...new Set([...bucket.values()])].sort((a, b) => a - b);
  const remap = new Map(used.map((v, i) => [v, i]));
  const rows = used.map(() => []);
  for (const n of main) rows[remap.get(bucket.get(n))].push(n);
  // wrap over-wide rows into sub-rows
  const out = [];
  for (const r of rows) {
    if (r.length <= ROW_MAX) { out.push(r); continue; }
    const chunks = Math.ceil(r.length / ROW_MAX), per = Math.ceil(r.length / chunks);
    for (let i = 0; i < r.length; i += per) out.push(r.slice(i, i + per));
  }
  return out;
}

export function computeLayout(topology, opts = {}) {
  const services = topology && topology.services ? topology.services : {};
  const names = Object.keys(services).sort();
  const edges = (topology && topology.edges ? topology.edges : [])
    .filter((e) => services[e.src] && services[e.dst] && e.src !== e.dst);
  const scores = names.map((n) => scoreOf(topology, n));
  const lo = scores.length ? Math.min(...scores) : 0, hi = scores.length ? Math.max(...scores) : 1;

  const isObs = (n) => (services[n].role || "other") === "other";
  const obsNames = opts.showObs === false ? [] : names.filter(isObs);
  const main = names.filter((n) => !isObs(n));
  const hidden = new Set(opts.showObs === false ? names.filter(isObs) : []);

  const byImportance = (a, b) => (scoreOf(topology, b) - scoreOf(topology, a)) || a.localeCompare(b);
  // centre-out: the most critical service of a row lands mid-row (shortest in-row arcs,
  // and the heavy nodes stay under the entrypoint column).
  const centreOut = (sorted) => sorted.reduce((out, n, i) => {
    if (i === 0 || i % 2 === 1) out.push(n); else out.unshift(n);
    return out;
  }, []);
  const rows = assignRows(services, edges, main).map((r) => centreOut([...r].sort(byImportance)));

  const nodes = {};
  const mk = (n, row, obs) => {
    const score = scoreOf(topology, n);
    const { w, h } = nodeSize(n, score, lo, hi, obs);
    nodes[n] = { name: n, row, w, h, x: 0, y: 0, score, obs, role: services[n].role || "other" };
    return nodes[n];
  };

  // main cluster: rows centred on x = 0, stacked top-down
  let y = 0;
  const rowTops = [], rowBottoms = [];
  rows.forEach((row, ri) => {
    row.forEach((n) => mk(n, ri, false));
    const rowH = Math.max(0, ...row.map((n) => nodes[n].h));
    const total = row.reduce((a, n) => a + nodes[n].w, 0) + COL_GAP * Math.max(0, row.length - 1);
    let x = -total / 2;
    row.forEach((n, ci) => {
      const nd = nodes[n];
      nd.x = Math.round(x + nd.w / 2); nd.y = Math.round(y + rowH / 2); nd.col = ci;
      x += nd.w + COL_GAP;
    });
    rowTops[ri] = y; rowBottoms[ri] = y + rowH;
    y += Math.max(rowH, NODE_H) + (ROW_PITCH - NODE_H);
  });
  const mainBottom = rowBottoms.length ? rowBottoms[rowBottoms.length - 1] : 0;
  const mainRight = Math.max(0, ...Object.values(nodes).map((n) => n.x + n.w / 2));

  // observability cluster: dimmed vertical stack off to the right
  let obsBox = null;
  if (obsNames.length) {
    const stack = [...obsNames].sort(byImportance);
    const colX = mainRight + OBS_GAP + OBS_W / 2;
    const stackH = (stack.length - 1) * OBS_PITCH + OBS_H;
    const top = Math.round(mainBottom / 2 - stackH / 2);
    stack.forEach((n, i) => {
      const nd = mk(n, rows.length + i, true);
      nd.x = Math.round(colX); nd.y = Math.round(top + OBS_H / 2 + i * OBS_PITCH); nd.col = 0;
    });
    obsBox = { x: colX - OBS_W / 2 - 16, y: top - 30, w: OBS_W + 32, h: stackH + 46 };
  }

  // ---- edge routing: fan attach points out along each node edge, bow parallel pairs
  const live = edges.filter((e) => !hidden.has(e.src) && !hidden.has(e.dst));
  const pairCount = new Map();
  for (const e of live) pairCount.set(`${e.src}>${e.dst}`, (pairCount.get(`${e.src}>${e.dst}`) || 0) + 1);
  const pairSeen = new Map();
  const slots = new Map();   // `${name}|${side}` -> [{other, set(x)}]
  const slot = (name, side, other, apply) => {
    const k = `${name}|${side}`;
    if (!slots.has(k)) slots.set(k, []);
    slots.get(k).push({ other, apply });
  };

  const routed = live.map((e, i) => {
    const a = nodes[e.src], b = nodes[e.dst];
    const key = `${e.src}>${e.dst}`;
    const idx = pairSeen.get(key) || 0; pairSeen.set(key, idx + 1);
    const bow = (idx - (pairCount.get(key) - 1) / 2) * 24;
    const dy = b.y - a.y;
    const r = { src: e.src, dst: e.dst, kind: KINDS[e.kind] ? e.kind : "documented", evidence: e.evidence || "", i, bow };
    if (Math.abs(dy) > 10) {
      r.mode = "flow";
      r.aSide = dy > 0 ? "bottom" : "top";
      r.bSide = dy > 0 ? "top" : "bottom";
    } else {
      r.mode = "hop";
      r.aSide = "top"; r.bSide = "top";
    }
    slot(e.src, r.aSide, b.x, (x) => { r.sx = x; });
    slot(e.dst, r.bSide, a.x, (x) => { r.ex = x; });
    return r;
  });

  for (const [k, list] of slots) {
    const nd = nodes[k.split("|")[0]];
    list.sort((p, q) => p.other - q.other);
    list.forEach((s, i) => s.apply(Math.round(nd.x + ((i + 1) / (list.length + 1) - 0.5) * SLOT_SPREAD * nd.w)));
  }

  let apexMin = Infinity;
  for (const r of routed) {
    const a = nodes[r.src], b = nodes[r.dst];
    const sy = a.y + (r.aSide === "bottom" ? a.h / 2 : -a.h / 2);
    const ey = b.y + (r.bSide === "bottom" ? b.h / 2 : -b.h / 2);
    if (r.mode === "flow") {
      // cp between the rows -> y stays monotonic, so the curve can never cross a sibling node
      const cx = (r.sx + r.ex) / 2 + r.bow, cy = sy + (ey - sy) * 0.35;
      r.d = `M${r.sx} ${sy} Q${cx.toFixed(1)} ${cy.toFixed(1)} ${r.ex} ${ey}`;
      r.dRev = `M${r.ex} ${ey} Q${cx.toFixed(1)} ${cy.toFixed(1)} ${r.sx} ${sy}`;
      r.mid = [(0.25 * r.sx + 0.5 * cx + 0.25 * r.ex), (0.25 * sy + 0.5 * cy + 0.25 * ey)];
    } else {
      // same-row hop: arc over the gap. apex of a quadratic = ¼p0 + ½cp + ¼p2, solve for cp.
      const span = Math.max(1, Math.abs((a.col || 0) - (b.col || 0)));
      const bulge = Math.min(HOP_CAP, 18 + 6 * (span - 1)) + Math.abs(r.bow) * 0.3;
      const apexY = Math.min(sy, ey) - bulge;
      const cy = 2 * apexY - 0.5 * (sy + ey);
      const cx = (r.sx + r.ex) / 2;
      r.d = `M${r.sx} ${sy} Q${cx.toFixed(1)} ${cy.toFixed(1)} ${r.ex} ${ey}`;
      r.dRev = `M${r.ex} ${ey} Q${cx.toFixed(1)} ${cy.toFixed(1)} ${r.sx} ${sy}`;
      r.mid = [cx, apexY];
      apexMin = Math.min(apexMin, apexY);
    }
  }

  // viewBox: union of node cards, the obs frame and the highest hop arc
  const xs = [], ys = [];
  for (const n of Object.values(nodes)) { xs.push(n.x - n.w / 2, n.x + n.w / 2); ys.push(n.y - n.h / 2, n.y + n.h / 2); }
  if (obsBox) { xs.push(obsBox.x, obsBox.x + obsBox.w); ys.push(obsBox.y, obsBox.y + obsBox.h); }
  if (Number.isFinite(apexMin)) ys.push(apexMin);
  if (!xs.length) { xs.push(0, 320); ys.push(0, 160); }
  const minX = Math.min(...xs) - PAD, minY = Math.min(...ys) - PAD;
  const width = Math.max(...xs) - Math.min(...xs) + PAD * 2;
  const height = Math.max(...ys) - Math.min(...ys) + PAD * 2;
  return {
    nodes, edges: routed, obsBox, rows,
    width: Math.round(width), height: Math.round(height),
    viewBox: `${Math.round(minX)} ${Math.round(minY)} ${Math.round(width)} ${Math.round(height)}`,
  };
}

// ---------------------------------------------------------------- graph helpers
function adjacency(edges) {
  const deps = new Map(), rdeps = new Map();
  for (const e of edges) {
    if (!deps.has(e.src)) deps.set(e.src, new Set());
    if (!rdeps.has(e.dst)) rdeps.set(e.dst, new Set());
    deps.get(e.src).add(e.dst); rdeps.get(e.dst).add(e.src);
  }
  const walk = (m, start) => {
    const seen = new Set(), q = [start];
    while (q.length) for (const nx of m.get(q.pop()) || []) if (!seen.has(nx)) { seen.add(nx); q.push(nx); }
    return seen;
  };
  return { deps, rdeps, subtree: (n) => walk(deps, n), blastOf: (n) => walk(rdeps, n) };
}

/** Health tone + glyph for one service. Never colour alone (§6). */
function toneFor(name, services, run) {
  const svcs = services || {};
  const s = svcs[name] || {};
  const h = (run && run.health && run.health[name]) || {};
  const active = run && run.phase && run.phase !== "idle";
  const status = String(h.status || s.state || "").toLowerCase();
  const dockerHealth = String(s.health || "").toLowerCase();
  const codes = [h.alive, h.ready].filter((v) => typeof v === "number");
  let tone = "warn", why = "unknown";
  if (["exited", "dead", "removing", "paused"].includes(status)) { tone = "danger"; why = status; }
  else if (dockerHealth === "unhealthy") { tone = "danger"; why = "unhealthy"; }
  else if (codes.some((c) => c >= 500)) { tone = "danger"; why = `HTTP ${Math.max(...codes)}`; }
  else if (["restarting", "created", "starting"].includes(status)) { tone = "warn"; why = status; }
  else if (codes.some((c) => c >= 400)) { tone = "warn"; why = `HTTP ${Math.max(...codes)}`; }
  else if (dockerHealth === "healthy") { tone = "ok"; why = "healthy"; }
  else if (status === "running") { tone = "ok"; why = s.health ? "healthy" : "running"; }
  else if (status) { tone = statusTone(status) === "neutral" ? "warn" : statusTone(status); why = status; }
  if (active && run.target_service === name) return { tone: "chaos", why: `chaos target · ${why}`, glyph: "crosshair" };
  return { tone, why, glyph: TONES[tone] || "info" };
}

// ---------------------------------------------------------------- injected stylesheet
function injectStyle() {
  if (document.getElementById(`${P}style`)) return;
  const t = (k) => `var(--${k}, ${FALLBACK[k]})`;
  const kindCss = KIND_ORDER.map((k) => {
    const d = KINDS[k];
    return `.${P}e--${k} .${P}edge{stroke:${t(d.color)};stroke-width:${d.width};${d.dash ? `stroke-dasharray:${d.dash};` : ""}}`;
  }).join("");
  const toneCss = ["ok", "warn", "danger", "critical", "chaos"].map((k) =>
    `.${P}n--${k} .${P}shape{stroke:${t(k)}}.${P}n--${k} .${P}glyph{color:${t(k)}}`).join("");
  const css = `
.${P}root{position:relative;width:100%;height:100%;min-height:240px;overflow:hidden;
  display:flex;flex-direction:column}
.${P}svg{display:block;width:100%;flex:1 1 auto;min-height:0}
.${P}node{cursor:pointer;transition:opacity var(--t,220ms) var(--ease,ease)}
.${P}node:focus{outline:none}
.${P}node:focus-visible .${P}shape{stroke:${t("cyan")};stroke-width:3}
.${P}shape{fill:${t("surface-2")};stroke:${t("border-strong")};stroke-width:1.6}
.${P}name{font-family:var(--font-mono,ui-monospace,monospace);font-size:14px;font-weight:600;fill:${t("text")}}
.${P}sub{font-family:var(--font-sans,system-ui);font-size:9.5px;fill:${t("text-mute")};letter-spacing:.04em}
.${P}badge-bg{fill:${t("surface-3")};stroke:${t("border")};stroke-width:1}
.${P}badge-tx{font-family:var(--font-mono,monospace);font-size:11px;font-weight:700;fill:${t("text-dim")}}
.${P}role-icon{color:${t("text-dim")}}
.${P}halo{fill:none;stroke:none;stroke-width:10;opacity:0}
.${P}edge{fill:none;stroke-linecap:round}
.${P}edge-halo{fill:none;stroke-width:9;opacity:0;stroke-linecap:round}
.${P}e--env .${P}edge-halo{stroke:${t("blue")};opacity:.16}
.${P}pulse{fill:none;stroke:${t("danger")};stroke-width:4;stroke-linecap:round;opacity:0}
.${P}obs-frame{fill:none;stroke:${t("border")};stroke-width:1.2;stroke-dasharray:4 5}
.${P}obs-label{font-family:var(--font-sans,system-ui);font-size:10px;fill:${t("text-mute")};letter-spacing:.08em}
.${P}n--obs{opacity:.52}.${P}n--obs .${P}name{font-size:12px}
${kindCss}${toneCss}
.${P}n--chaos .${P}shape{stroke-width:3.6}
.${P}n--chaos .${P}halo{stroke:${t("chaos")};opacity:.3;animation:${P}pulse 1.9s ease-in-out infinite}
.${P}n--observed .${P}shape{fill:color-mix(in srgb, ${t("danger")} 16%, ${t("surface-2")});stroke:${t("danger")};stroke-width:2.6}
.${P}n--expected .${P}shape{stroke:${t("warn")};stroke-width:2.2;stroke-dasharray:7 5}
.${P}n--surprise .${P}shape{stroke:${t("critical")};stroke-width:3.2}
.${P}n--surprise .${P}halo{stroke:${t("critical")};opacity:.34}
.${P}mark{font-family:var(--font-sans,system-ui);font-size:9.5px;font-weight:800;letter-spacing:.09em;
  fill:${t("critical")};opacity:0;paint-order:stroke;stroke:${t("surface")};stroke-width:3.5;stroke-linejoin:round}
.${P}n--surprise .${P}mark{opacity:1}
.${P}e.is-blast .${P}edge{stroke:${t("danger")};stroke-width:2.6;stroke-dasharray:none}
.${P}e.is-blast .${P}pulse{opacity:.95;stroke-dasharray:18 var(--len,400);animation:${P}travel 1.15s linear infinite}
.${P}e--observed .${P}edge{animation:${P}flow 1.6s linear infinite}
.${P}e.is-cascade .${P}edge{stroke:${t("danger")};stroke-width:3;stroke-dasharray:none}
.${P}e.is-cascade .${P}pulse{opacity:.95;stroke-dasharray:18 var(--len,400);animation:${P}travel 1.15s linear infinite}
.${P}e.is-cascade--soft .${P}edge{stroke-width:2.4;stroke-dasharray:7 6;opacity:.8}
.${P}e.is-cascade--soft .${P}pulse{opacity:.5;stroke-width:3;stroke-dasharray:5 9;animation:${P}trickle 1.5s linear infinite}
.${P}cbadge{cursor:help}
.${P}cbadge:focus{outline:none}
.${P}cbadge rect{fill:${t("surface-3")};stroke:${t("danger")};stroke-width:1.4}
.${P}cbadge--soft rect{stroke-dasharray:3 2.5}
.${P}cbadge:focus-visible rect{stroke:${t("cyan")};stroke-width:2.4}
.${P}cbadge text{font-family:var(--font-mono,monospace);font-size:10.5px;font-weight:700;fill:${t("text")}}
.${P}cmark{font-family:var(--font-sans,system-ui);font-size:9px;font-weight:800;letter-spacing:.08em;
  fill:${t("warn")};opacity:0;paint-order:stroke;stroke:${t("surface")};stroke-width:3.5;stroke-linejoin:round}
.${P}cmark.is-on{opacity:1}
.${P}replay{position:absolute;left:8px;top:8px;z-index:4;display:flex;align-items:center;gap:8px;max-width:calc(100% - 140px);
  min-height:28px;padding:3px 4px 3px 10px;border:1px dashed ${t("danger")};border-radius:var(--r1,6px);
  background:${t("surface-2")};color:${t("text-dim")};font:11px var(--font-sans,system-ui)}
.${P}replay[hidden]{display:none}
.${P}replay span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.${P}replay b{color:${t("text")};font-family:var(--font-mono,monospace);font-weight:600}
.${P}replay button{flex:none;min-height:22px;padding:2px 8px;cursor:pointer;border:1px solid ${t("border-strong")};
  border-radius:var(--r1,6px);background:${t("surface-3")};color:${t("text-dim")};font:11px var(--font-sans,system-ui)}
.${P}replay button:focus-visible{outline:2px solid ${t("cyan")};outline-offset:2px}
.${P}legend .${P}lg-cascade[hidden]{display:none}
.${P}dim{opacity:.25}
.${P}node.is-selected .${P}shape{stroke-width:3.4}
.${P}node.is-selected .${P}name{fill:${t("text")}}
.${P}e.is-rel .${P}edge{stroke-width:3.2}
@keyframes ${P}flow{to{stroke-dashoffset:-28}}
@keyframes ${P}travel{from{stroke-dashoffset:18}to{stroke-dashoffset:calc(-1 * var(--len,400))}}
@keyframes ${P}trickle{from{stroke-dashoffset:0}to{stroke-dashoffset:-28}}
@keyframes ${P}pulse{0%,100%{stroke-width:9;opacity:.22}50%{stroke-width:20;opacity:.5}}
.${P}root .tooltip{position:absolute;left:var(--x,0);top:var(--y,0);transform:none;z-index:5;pointer-events:none;
  max-width:280px;padding:var(--s3,12px);border:1px solid ${t("border-strong")};border-radius:var(--r2,10px);
  background:${t("surface-3")};box-shadow:var(--shadow-2,0 8px 30px rgba(0,0,0,.5));
  font:12px/1.45 var(--font-sans,system-ui);color:${t("text-dim")};opacity:0;transition:opacity 120ms}
.${P}root .tooltip.is-on{opacity:1}
.${P}root .tooltip b{color:${t("text")};font-family:var(--font-mono,monospace);font-size:13px}
.${P}root .tooltip dl{display:grid;grid-template-columns:auto 1fr;gap:2px var(--s2,8px);margin:6px 0 0}
.${P}root .tooltip dt{color:${t("text-mute")}}.${P}root .tooltip dd{margin:0;color:${t("text-dim")}}
.${P}legend{flex:0 0 auto;display:flex;flex-wrap:wrap;gap:3px 14px;margin:2px 6px 6px;
  padding:5px 9px;border:1px solid ${t("border")};border-radius:var(--r1,6px);background:${t("surface")};
  font:10.5px/1.5 var(--font-sans,system-ui);color:${t("text-mute")}}
.${P}legend>span{flex:0 0 auto;display:inline-flex;align-items:center;gap:5px;white-space:nowrap}
.${P}legend span span{white-space:nowrap}
.${P}legend i{display:inline-block;width:16px;height:0;border-top-width:2px;border-top-style:solid}
.${P}legend em{display:inline-block;width:9px;height:9px;border-radius:3px;border:2px solid currentColor;font-style:normal}
.${P}toggle{position:absolute;right:8px;top:8px;z-index:4;min-height:28px;padding:4px 9px;cursor:pointer;
  border:1px solid ${t("border-strong")};border-radius:var(--r1,6px);background:${t("surface-2")};
  color:${t("text-dim")};font:11px var(--font-sans,system-ui)}
.${P}toggle[aria-pressed="false"]{color:${t("text-mute")}}
.${P}toggle:focus-visible{outline:2px solid ${t("cyan")};outline-offset:2px}
.${P}alt{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.${P}empty{display:grid;place-items:center;height:100%;color:${t("text-mute")};font:13px var(--font-sans,system-ui)}
@media (prefers-reduced-motion: reduce){
  .${P}e--observed .${P}edge,.${P}e.is-blast .${P}pulse,.${P}e.is-cascade .${P}pulse,.${P}n--chaos .${P}halo{animation:none!important}
  .${P}e.is-blast .${P}pulse,.${P}e.is-cascade .${P}pulse{opacity:0}
}`;
  // inserted first in <head> so app.css (later in document order) can still override
  const node = el("style", { id: `${P}style` });
  node.textContent = css;
  document.head.insertBefore(node, document.head.firstChild);
}

// ---------------------------------------------------------------- node shapes
function shapePath(role, w, h) {
  const x = -w / 2, y = -h / 2, r = 10;
  if (role === "datastore") {                       // cylinder silhouette
    const ry = 9;
    return `M${x} ${y + ry} a${w / 2} ${ry} 0 0 1 ${w} 0 v${h - 2 * ry} a${w / 2} ${ry} 0 0 1 ${-w} 0 z`;
  }
  if (role === "proxy") {                           // hexagon (pointed left/right)
    const c = Math.min(18, w / 5);
    return `M${x + c} ${y} H${x + w - c} L${x + w} 0 L${x + w - c} ${y + h} H${x + c} L${x} 0 Z`;
  }
  return `M${x + r} ${y} h${w - 2 * r} a${r} ${r} 0 0 1 ${r} ${r} v${h - 2 * r} a${r} ${r} 0 0 1 ${-r} ${r} h${-(w - 2 * r)} a${r} ${r} 0 0 1 ${-r} ${-r} v${-(h - 2 * r)} a${r} ${r} 0 0 1 ${r} ${-r} z`;
}

function marker(kind) {
  const color = tok(KINDS[kind] ? KINDS[kind].color : "text-mute");
  return svg("marker", {
    id: `${P}arrow-${kind}`, viewBox: "0 0 10 10", refX: 8.5, refY: 5,
    markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse", markerUnits: "userSpaceOnUse",
  }, svg("path", { d: "M0 1 L9 5 L0 9 z", fill: color, class: `${P}arrow ${P}arrow--${kind}` }));
}

// ---------------------------------------------------------------- factory
export function createTopology(hostEl, { onSelect, onReplayClear } = {}) {
  injectStyle();
  const root = el("div", { class: `${P}root topology` });
  const tip = el("div", { class: "tooltip", "aria-hidden": "true" });
  const legend = el("div", { class: `${P}legend legend`, "aria-hidden": "true" });
  const alt = el("ul", { class: `${P}alt sr-only` });
  const toggle = el("button", {
    type: "button", class: `${P}toggle`, "aria-pressed": "true",
    title: "Show or hide observability services (not chaos-relevant)",
  }, "Observability");
  // shown while the map replays a FINISHED experiment's cascade (app.js calls showCascade on row expand)
  const replayLabel = el("span");
  const replayBar = el("div", { class: `${P}replay`, hidden: true },
    replayLabel,
    el("button", {
      type: "button", title: "Stop showing this finished experiment's cascade on the map",
      onclick: () => { replay = null; if (typeof onReplayClear === "function") onReplayClear(); refresh(); },
    }, "Clear"));
  clear(hostEl).append(root);
  root.append(toggle, replayBar, legend, tip, alt);

  let svgRoot = null, nodeLayer = null, edgeLayer = null, badgeLayer = null, defs = null;
  let L = null, graph = null, topo = null, fp = "";
  let showObs = true, selected = null, hovered = null, order = [], lastAria = "", lastAlt = "";
  const nodeEls = new Map(), edgeEls = [];
  // cascade attribution: `replay` is a finished experiment the operator picked, `view` what is painted now
  let replay = null, view = null, cHops = new Map(), cUp = new Map(), legendCascade = [];
  const pairRep = new Map();   // "src>dst" -> the one edge element that carries that pair's cascade

  toggle.addEventListener("click", () => {
    showObs = !showObs;
    toggle.setAttribute("aria-pressed", String(showObs));
    fp = "";                                   // force one rebuild; user-initiated, so cheap
    if (topo) rebuild(topo);
  });

  // ------------------------------------------------------------ build (once per fingerprint)
  function rebuild(topology) {
    L = computeLayout(topology, { showObs });
    graph = adjacency(L.edges);
    nodeEls.clear(); edgeEls.length = 0;
    if (svgRoot) svgRoot.remove();

    svgRoot = svg("svg", {
      class: `${P}svg`, viewBox: L.viewBox, preserveAspectRatio: "xMidYMid meet",
      role: "img", "aria-label": "Service dependency graph",
    });
    defs = svg("defs", null, ...KIND_ORDER.map(marker), svg("marker", {
      id: `${P}arrow-blast`, viewBox: "0 0 10 10", refX: 8.5, refY: 5, markerWidth: 8, markerHeight: 8,
      orient: "auto-start-reverse", markerUnits: "userSpaceOnUse",
    }, svg("path", { d: "M0 1 L9 5 L0 9 z", fill: tok("danger") })));
    edgeLayer = svg("g", { class: `${P}edges` });
    badgeLayer = svg("g", { class: `${P}cbadges` });
    nodeLayer = svg("g", { class: `${P}nodes` });
    const frame = svg("g", null);
    if (L.obsBox) {
      frame.append(
        svg("rect", { class: `${P}obs-frame`, x: L.obsBox.x, y: L.obsBox.y, width: L.obsBox.w, height: L.obsBox.h, rx: 12, stroke: tok("border") }),
        svg("text", { class: `${P}obs-label`, x: L.obsBox.x + 14, y: L.obsBox.y + 19, fill: tok("text-mute") }, "OBSERVABILITY"),
      );
    }
    svgRoot.append(defs, frame, edgeLayer, badgeLayer, nodeLayer);
    // click on empty canvas clears the selection
    svgRoot.addEventListener("click", (ev) => {
      if (!(ev.target.closest && ev.target.closest(`.${P}node`))) pick(null);
    });
    root.insertBefore(svgRoot, toggle);

    for (const e of L.edges) buildEdge(e);
    for (const nd of Object.values(L.nodes)) buildNode(nd, topology);
    // parallel edges (same pair, different evidence) share one cascade: the strongest evidence carries it
    pairRep.clear();
    const rank = (k) => KIND_ORDER.indexOf(k);
    for (const ref of [...edgeEls].sort((a, b) => rank(a.e.kind) - rank(b.e.kind))) {
      const k = `${ref.e.src}>${ref.e.dst}`;
      if (!pairRep.has(k)) pairRep.set(k, ref);
    }

    order = Object.values(L.nodes).sort((a, b) => a.row - b.row || a.x - b.x);
    buildLegend();
  }

  function buildEdge(e) {
    const g = svg("g", { class: cls(`${P}e`, `${P}e--${e.kind}`), "data-src": e.src, "data-dst": e.dst, "data-kind": e.kind });
    const d = KINDS[e.kind];
    const halo = d.halo ? svg("path", { class: `${P}edge-halo`, d: e.d, stroke: tok(d.color) }) : null;
    const path = svg("path", {
      class: cls(`${P}edge`, `edge`, `edge--${e.kind}`), d: e.d,
      stroke: tok(d.color), "stroke-width": d.width, "stroke-dasharray": d.dash || null, fill: "none",
      "marker-end": `url(#${P}arrow-${e.kind})`,
    });
    const pulse = svg("path", { class: `${P}pulse`, d: e.dRev, stroke: tok("danger"), fill: "none" });
    const title = svg("title");   // hover text for a cascade edge; empty (no tooltip) otherwise
    g.append(title);
    if (halo) g.append(halo);
    g.append(path, pulse);
    edgeLayer.append(g);
    if (typeof pulse.getTotalLength === "function") {
      try { pulse.style.setProperty("--len", String(Math.round(pulse.getTotalLength()))); } catch { /* jsdom */ }
    }
    // midpoint badge for a cascade edge: text (lag / ≈ / ?) so confidence is never colour or dash alone
    const bRect = svg("rect", { x: -16, y: -9, width: 32, height: 18, rx: 8 });
    const bText = svg("text", { x: 0, y: 3.8, "text-anchor": "middle" });
    const badge = svg("g", {
      class: `${P}cbadge`, transform: `translate(${e.mid[0].toFixed(1)} ${e.mid[1].toFixed(1)})`,
      tabindex: "0", role: "img",
    }, bRect, bText);
    badge.style.display = "none";
    badgeLayer.append(badge);
    const ref = { e, g, path, pulse, title, badge, bRect, bText, c: null };
    badge.addEventListener("mouseenter", () => showEdgeTip(ref));
    badge.addEventListener("focus", () => showEdgeTip(ref));
    badge.addEventListener("mouseleave", hideTip);
    badge.addEventListener("blur", hideTip);
    edgeEls.push(ref);
  }

  function buildNode(nd, topology) {
    const s = topology.services[nd.name] || {};
    const g = svg("g", {
      class: cls(`${P}node`, "node", `${P}n--role-${nd.role}`, nd.obs && `${P}n--obs`),
      transform: `translate(${nd.x} ${nd.y})`, tabindex: "0", role: "button",
      "data-name": nd.name, "aria-label": nd.name,
    });
    const halo = svg("path", { class: `${P}halo`, d: shapePath(nd.role, nd.w + 8, nd.h + 8), fill: "none" });
    const shape = svg("path", { class: cls(`${P}shape`, "node__shape"), d: shapePath(nd.role, nd.w, nd.h), fill: tok("surface-2"), stroke: tok("border-strong") });
    const icon = svg("g", { class: `${P}role-icon`, transform: `translate(${-nd.w / 2 + 12} ${nd.obs ? -8 : -10})`, color: tok("text-dim") });
    icon.append(svgIcon(roleIcon(nd.role), nd.obs ? 16 : 20));
    const name = svg("text", {
      class: `${P}name`, x: -nd.w / 2 + NAME_X, y: nd.obs ? 5 : -2,
      "dominant-baseline": "middle", fill: tok("text"),
    }, nd.name);
    // drop trailing sub-line parts rather than letting them run under the corner glyph
    const parts = [titleCase(nd.role), s.stateful ? "stateful" : null, s.mem_limit_mb ? `${s.mem_limit_mb}MB` : null].filter(Boolean);
    const room = Math.floor((nd.w - NAME_X - 24) / 6.2);   // 6.2px per upper-case char at 9.5px
    while (parts.length > 1 && parts.join(" · ").length > room) parts.pop();
    const sub = nd.obs ? null
      : svg("text", { class: `${P}sub`, x: -nd.w / 2 + NAME_X, y: 16, fill: tok("text-mute") }, parts.join(" · ").toUpperCase());
    const badgeY = -nd.h / 2 + 4;
    const badge = svg("g", { class: `${P}badge` },
      svg("rect", { class: `${P}badge-bg`, x: nd.w / 2 - 44, y: badgeY, width: 36, height: 17, rx: 5, fill: tok("surface-3"), stroke: tok("border") }),
      svg("text", { class: `${P}badge-tx`, x: nd.w / 2 - 26, y: badgeY + 12, "text-anchor": "middle", fill: tok("text-dim") }, nd.score.toFixed(1)),
    );
    const glyph = svg("g", { class: `${P}glyph`, transform: `translate(${nd.w / 2 - 21} ${nd.h / 2 - 20})`, color: tok("text-mute") });
    glyph.append(svgIcon("info", 14));
    // observability services are never chaos targets, so they carry no blast marker
    const mark = nd.obs ? null : svg("text", {
      class: `${P}mark`, x: 0, y: -nd.h / 2 - 7, "text-anchor": "middle", fill: tok("critical"),
    }, "⚠ UNEXPECTED");
    // cascade markers under the card: "no graph path" / "health check green"
    const cmark = nd.obs ? null : svg("text", {
      class: `${P}cmark`, x: 0, y: nd.h / 2 + 14, "text-anchor": "middle", fill: tok("warn"),
    });
    g.append(...[halo, shape, icon, name, sub, badge, glyph, mark, cmark].filter(Boolean));
    nodeLayer.append(g);

    g.addEventListener("click", () => pick(nd.name));
    g.addEventListener("keydown", (ev) => onKey(ev, nd));
    g.addEventListener("mouseenter", () => { hovered = nd.name; showTip(nd); paintFocus(); });
    g.addEventListener("mouseleave", () => { hovered = null; hideTip(); paintFocus(); });
    g.addEventListener("focus", () => { hovered = nd.name; showTip(nd); paintFocus(); });
    g.addEventListener("blur", () => { if (hovered === nd.name) { hovered = null; hideTip(); paintFocus(); } });
    nodeEls.set(nd.name, { nd, g, shape, glyph, badge, name, cmark });
  }

  function buildLegend() {
    clear(legend);
    const kinds = new Set(L.edges.map((e) => e.kind));
    for (const k of KIND_ORDER) {
      if (!kinds.has(k)) continue;
      const d = KINDS[k];
      legend.append(el("span", null,
        el("i", { style: { borderTopColor: tok(d.color), borderTopStyle: d.dash ? "dashed" : "solid", borderTopWidth: `${Math.max(2, d.width)}px` } }),
        KINDS[k].label));
    }
    for (const [t, label] of [["ok", "healthy"], ["warn", "degraded / unknown"], ["danger", "down"], ["chaos", "chaos target"], ["critical", "unexpected blast"]]) {
      legend.append(el("span", { style: { color: tok(t) } }, el("em"), el("span", { style: { color: tok("text-mute") } }, label)));
    }
    // only shown while a cascade is on the map
    legendCascade = [["solid", "cascade, order proven (+lag)"], ["dashed", "cascade, direction or path uncertain (≈ / ?)"]]
      .map(([style, label]) => el("span", { class: `${P}lg-cascade`, hidden: true },
        el("i", { style: { borderTopColor: tok("danger"), borderTopStyle: style, borderTopWidth: "3px" } }), label));
    legend.append(...legendCascade);
  }

  // ------------------------------------------------------------ per-tick paint
  /** What the map paints: the live run, else a finished experiment's cascade picked from the table. */
  function currentView(state) {
    const run = state.run || {};
    if (run.phase && run.phase !== "idle") {
      const b = run.blast || {};
      return { run, target: run.target_service, expected: b.expected || [], observed: b.observed || [],
               edges: b.cascade_edges || [], hops: b.cascade_hops || [], replay: null };
    }
    if (!replay) return null;
    return { run: { phase: "replay", target_service: replay.target, health: {} }, target: replay.target,
             expected: replay.expected || [], observed: replay.observed || [],
             edges: replay.edges || [], hops: replay.hops || [], replay };
  }

  function applyLive(state) {
    const services = topo.services || {};
    view = currentView(state);
    const active = Boolean(view);
    const run = active ? view.run : (state.run || {});
    const expected = new Set(active ? view.expected : []);
    const observed = new Set(active ? view.observed : []);
    const target = active ? view.target : null;
    const cEdges = active ? view.edges.filter((c) => c && c.src && c.dst) : [];
    cHops = new Map((active ? view.hops : []).filter((h) => h && h.service).map((h) => [h.service, h]));
    cUp = new Map(cEdges.map((c) => [c.src, c]));
    const cascade = cEdges.length > 0 || cHops.size > 0;
    // an attributed hop failed even when its health check stayed green (masked), so it is in the blast
    for (const name of cHops.keys()) observed.add(name);
    let down = 0;
    const tones = new Map();

    for (const [name, ref] of nodeEls) {
      const { tone, why, glyph } = toneFor(name, services, run);
      tones.set(name, { tone, why });
      if (tone === "danger") down++;
      const hop = cHops.get(name);
      const isObs = observed.has(name), isExp = expected.has(name) && !observed.has(name);
      const surprise = isObs && name !== target && (hop ? Boolean(hop.beyond_hypothesis) : !expected.has(name));
      const g = ref.g;
      setClasses(g, {
        [`${P}n--ok`]: tone === "ok", [`${P}n--warn`]: tone === "warn",
        [`${P}n--danger`]: tone === "danger", [`${P}n--critical`]: tone === "critical",
        [`${P}n--chaos`]: tone === "chaos",
        [`${P}n--observed`]: isObs && name !== target, [`${P}n--expected`]: isExp,
        [`${P}n--surprise`]: surprise, "is-selected": selected === name,
      });
      // glyph (colour is always paired with a shape, §6)
      const want = tone === "chaos" ? "crosshair" : glyph;
      if (ref.glyphName !== want) {
        clear(ref.glyph).append(svgIcon(want, 14));
        ref.glyph.setAttribute("color", tok(tone === "neutral" ? "text-mute" : tone));
        ref.glyphName = want;
      }
      // a surprise victim is the headline finding: paint it last so its marker is never covered
      if (ref.surpriseWas !== surprise) { if (surprise) nodeLayer.appendChild(g); ref.surpriseWas = surprise; }
      if (ref.cmark) {
        const marks = [hop && hop.path_confidence === "no_graph_path" ? "⊘ NO GRAPH PATH" : null,
                       hop && hop.masked_by_health_check ? "◐ HEALTH CHECK GREEN" : null].filter(Boolean).join("  ·  ");
        if (ref.cmarkWas !== marks) {
          ref.cmark.textContent = marks; ref.cmark.classList.toggle("is-on", Boolean(marks)); ref.cmarkWas = marks;
        }
      }
      const label = `${name}: ${titleCase(ref.nd.role)}, ${why}, criticality ${ref.nd.score.toFixed(1)}`
        + (surprise ? ", affected but not predicted" : isObs && name !== target ? ", affected by the current failure"
          : isExp ? ", predicted to be affected" : "")
        + (hop && hop.path_confidence !== "origin" ? `, cascade: ${hopWords(hop)}` : "");
      if (g.getAttribute("aria-label") !== label) g.setAttribute("aria-label", label);
      if (ref.toneWas !== tone) { ref.shape.setAttribute("stroke", tok(tone === "neutral" ? "border-strong" : tone)); ref.toneWas = tone; }
    }

    // With cascade data only the attributed path pulses (dependency -> dependent); without it, the
    // original rule: every edge between two affected services.
    const still = reduceMotion();
    const carried = new Map();
    for (const c of cEdges) {
      const ref = pairRep.get(`${c.src}>${c.dst}`);
      if (ref && !carried.has(ref)) carried.set(ref, c);
    }
    for (const ref of edgeEls) {
      const { e, g, path, pulse } = ref;
      const c = carried.get(ref) || null;
      const blast = !cascade && active && observed.has(e.src) && observed.has(e.dst);
      g.classList.toggle("is-blast", blast);
      g.classList.toggle("is-cascade", Boolean(c));
      g.classList.toggle("is-cascade--soft", Boolean(c) && c.confidence !== "ordered");
      pulse.style.display = (blast || c) && !still ? "" : "none";
      // a cascade edge's arrow points the way the failure went (at the dependent), so the direction
      // survives reduced motion; every other edge keeps its "depends on" arrow
      const end = c ? null : blast ? `url(#${P}arrow-blast)` : `url(#${P}arrow-${e.kind})`;
      const start = c ? `url(#${P}arrow-blast)` : null;
      if (path.getAttribute("marker-end") !== end) { if (end) path.setAttribute("marker-end", end); else path.removeAttribute("marker-end"); }
      if (path.getAttribute("marker-start") !== start) { if (start) path.setAttribute("marker-start", start); else path.removeAttribute("marker-start"); }
      paintCascadeEdge(ref, c);
    }
    for (const span of legendCascade) span.hidden = !cascade;

    const rp = active ? view.replay : null;
    replayBar.hidden = !rp;
    if (rp && replayBar.dataset.id !== rp.id) {
      clear(replayLabel).append("Cascade of finished experiment ", el("b", { text: rp.label || rp.id }),
        rp.note ? ` · ${rp.note}` : "");
      replayBar.dataset.id = rp.id;
    }

    // aria summary + off-screen text equivalent
    const n = nodeEls.size, m = L.edges.length;
    const aria = `Service dependency graph: ${n} services, ${m} dependencies, ${down} down.`
      + (rp ? ` Showing the cascade of finished experiment ${rp.label || rp.id} on ${target}.`
        : target ? ` Current chaos target: ${target}, phase ${run.phase}.` : " No experiment running.")
      + (cascade ? ` Cascade path: ${cEdges.length} attributed hop${cEdges.length === 1 ? "" : "s"}.` : "");
    if (aria !== lastAria) { svgRoot.setAttribute("aria-label", aria); lastAria = aria; }
    const lines = order.map((nd) => {
      const t = tones.get(nd.name) || {};
      const deps = [...(graph.deps.get(nd.name) || [])].sort();
      const rev = [...(graph.rdeps.get(nd.name) || [])].sort();
      return `${nd.name}: ${nd.role}, ${t.why}, criticality ${nd.score.toFixed(1)}. `
        + `Depends on ${deps.length ? deps.join(", ") : "nothing"}. `
        + `Required by ${rev.length ? rev.join(", ") : "nothing"}.`;
    });
    for (const h of cHops.values()) if (h.path_confidence !== "origin") lines.push(`Cascade: ${h.service}, ${hopWords(h)}.`);
    const sig = lines.join("|");
    if (sig !== lastAlt) { clear(alt).append(...lines.map((t) => el("li", { text: t }))); lastAlt = sig; }
  }

  const cap = (t) => (t ? t[0].toUpperCase() + t.slice(1) : t);
  const mechLabel = (m) => CASCADE_MECHANISM[m] || m || "failed";
  const num = (v) => (typeof v === "number" ? v : null);

  /** One hop in words: mechanism, the dependency it failed through, confidence, masked / beyond tags. */
  function hopWords(hop) {
    const up = cUp.get(hop.service);
    const upstream = hop.upstream || (up && up.dst) || null;
    const lag = num(hop.lag_s) ?? (up ? num(up.lag_s) : null);
    return [
      mechLabel(hop.mechanism),
      upstream && !["ordered", "origin"].includes(hop.path_confidence) ? `via ${upstream}` : null,
      cascadeConfidence(hop.path_confidence, upstream, lag),
      hop.masked_by_health_check ? "health check stayed green" : null,
      hop.beyond_hypothesis ? "beyond the hypothesis" : null,
    ].filter(Boolean).join(", ");
  }

  /** Cascade edge in words. src depends on dst, so the failure travelled dst -> src. */
  function edgeWords(c, hop) {
    return [
      `${c.dst} → ${c.src}`,
      cap(mechLabel(c.mechanism)),
      cap(cascadeConfidence(c.confidence, c.dst, num(c.lag_s))),
      hop && hop.masked_by_health_check ? `${c.src}'s own health check stayed green` : null,
      hop && hop.beyond_hypothesis ? `${c.src} was not in the hypothesis` : null,
    ].filter(Boolean);
  }

  function paintCascadeEdge(ref, c) {
    const hop = c ? cHops.get(c.src) || null : null;
    const sig = c ? JSON.stringify([c.mechanism, c.confidence, c.lag_s, hop && hop.masked_by_health_check,
                                    hop && hop.beyond_hypothesis]) : "";
    if (ref.cWas === sig) return;
    ref.cWas = sig;
    ref.c = c ? { c, hop } : null;
    ref.badge.style.display = c ? "" : "none";
    if (!c) { ref.title.textContent = ""; ref.badge.removeAttribute("aria-label"); return; }
    edgeLayer.appendChild(ref.g);   // above the edges it crosses
    const words = edgeWords(c, hop);
    ref.title.textContent = words.join("\n");
    ref.badge.setAttribute("aria-label", `Failure travelled ${words.join(". ")}`);
    const lag = num(c.lag_s);
    const glyph = c.confidence === "ordered" ? (lag !== null ? `+${lag.toFixed(1)}s` : "→")
      : c.confidence === "same_interval" ? "≈" : "?";
    const text = glyph + (hop && hop.masked_by_health_check ? " ◐" : "");
    const w = Math.max(24, Math.round(12 + text.length * 6.6));
    ref.bText.textContent = text;
    ref.bRect.setAttribute("x", String(-w / 2));
    ref.bRect.setAttribute("width", String(w));
    ref.badge.classList.toggle(`${P}cbadge--soft`, c.confidence !== "ordered");
  }

  function setClasses(node, map) {
    for (const [k, on] of Object.entries(map)) node.classList.toggle(k, Boolean(on));
  }

  /** Dim everything unrelated to the hovered/selected node to ~25%. */
  function paintFocus() {
    const focus = hovered || selected;
    if (!focus || !nodeEls.has(focus)) {
      for (const { g } of nodeEls.values()) g.classList.remove(`${P}dim`);
      for (const { g, badge } of edgeEls) { g.classList.remove(`${P}dim`); g.classList.remove("is-rel"); badge.classList.remove(`${P}dim`); }
      return;
    }
    const sub = graph.subtree(focus), up = graph.blastOf(focus);
    const keep = new Set([focus, ...sub, ...up]);
    for (const [name, { g }] of nodeEls) g.classList.toggle(`${P}dim`, !keep.has(name));
    for (const { e, g, badge } of edgeEls) {
      const rel = keep.has(e.src) && keep.has(e.dst);
      g.classList.toggle(`${P}dim`, !rel);
      badge.classList.toggle(`${P}dim`, !rel);
      g.classList.toggle("is-rel", rel && (e.src === focus || e.dst === focus));
    }
  }

  function pick(name) {
    selected = name && nodeEls.has(name) ? name : null;
    for (const [n, { g }] of nodeEls) g.classList.toggle("is-selected", n === selected);
    paintFocus();
    if (typeof onSelect === "function") onSelect(selected);
  }

  function onKey(ev, nd) {
    const k = ev.key;
    if (k === "Enter" || k === " " || k === "Spacebar") { ev.preventDefault(); pick(nd.name); return; }
    const dirs = { ArrowLeft: "l", ArrowRight: "r", ArrowUp: "u", ArrowDown: "d" };
    if (!dirs[k]) return;
    ev.preventDefault();
    const dir = dirs[k];
    let best = null;
    for (const o of order) {
      if (o.name === nd.name) continue;
      const sameRow = o.row === nd.row;
      const ok = dir === "l" ? (sameRow && o.x < nd.x) : dir === "r" ? (sameRow && o.x > nd.x)
        : dir === "u" ? o.row < nd.row : o.row > nd.row;
      if (!ok) continue;
      const cost = (dir === "l" || dir === "r") ? Math.abs(o.x - nd.x)
        : Math.abs(o.row - nd.row) * 1000 + Math.abs(o.x - nd.x);
      if (!best || cost < best.cost) best = { o, cost };
    }
    if (best) nodeEls.get(best.o.name).g.focus();
  }

  // ------------------------------------------------------------ tooltip
  function showTip(nd) {
    if (!topo || !nodeEls.has(nd.name)) return;
    const s = (topo.services || {})[nd.name] || {};
    const run = (lastState && lastState.run) || {};
    const { why } = toneFor(nd.name, topo.services, run);
    const crit = (topo.criticality || []).find((c) => c.service === nd.name) || {};
    const deps = [...(graph.deps.get(nd.name) || [])].sort();
    const breaks = (crit.transitive_dependents && crit.transitive_dependents.length
      ? crit.transitive_dependents : [...graph.blastOf(nd.name)]).slice().sort();
    const exps = ((lastState && lastState.experiments) || []).filter((x) => x.target_service === nd.name);
    const worst = exps.length ? Math.min(...exps.map((x) => (typeof x.during_success_rate === "number" ? x.during_success_rate : 1))) : null;
    const slowP95 = exps.length ? Math.max(...exps.map((x) => x.during_p95_ms || 0)) : 0;
    const row = (k, v) => [el("dt", { text: k }), el("dd", { text: v })];
    clear(tip).append(
      el("b", { text: nd.name }),
      el("dl", null,
        row("role", `${titleCase(nd.role)}${s.stateful ? " · stateful" : ""}${s.entrypoint ? " · entrypoint" : ""}`),
        row("container", s.container || "—"),
        row("health", why),
        row("criticality", `${nd.score.toFixed(1)}${crit.fan_in != null ? `  ·  fan-in ${crit.fan_in}` : ""}`),
        row("needs", deps.length ? deps.join(", ") : "nothing"),
        row("breaks with it", breaks.length ? breaks.join(", ") : "nothing"),
        s.chaos_relevant === false ? row("chaos", "excluded (observability)") : [],
        exps.length ? row("experiments", `${exps.length} · worst ${fmtPct(worst)}${slowP95 ? ` · p95 ${fmtMs(slowP95)}` : ""}`) : [],
        cHops.has(nd.name) ? row("cascade", hopWords(cHops.get(nd.name))) : [],
      ),
      (crit.reasons || []).length ? el("div", { style: { marginTop: "6px", color: tok("text-mute") }, text: crit.reasons.join(" · ") }) : null,
    );
    placeTip(nodeEls.get(nd.name).g.getBoundingClientRect());
  }

  function showEdgeTip(ref) {
    if (!ref.c) return;
    const [head, ...rest] = edgeWords(ref.c.c, ref.c.hop);
    clear(tip).append(el("b", { text: `Failure travelled ${head}` }), ...rest.map((t) => el("div", { text: t })));
    placeTip(ref.badge.getBoundingClientRect());
  }

  /** Position the tooltip next to a screen rect, clamped inside the host. */
  function placeTip(r) {
    const box = root.getBoundingClientRect();
    const tw = 280, th = tip.offsetHeight || 170;
    let x = r.right - box.left + 12, y = r.top - box.top - 6;
    if (x + tw > box.width) x = r.left - box.left - tw - 12;
    tip.style.setProperty("--x", `${Math.max(6, Math.round(x))}px`);
    tip.style.setProperty("--y", `${Math.max(6, Math.round(Math.min(y, box.height - th - 6)))}px`);
    tip.classList.add("is-on");
  }
  const hideTip = () => tip.classList.remove("is-on");

  // ------------------------------------------------------------ public API
  let lastState = null;
  function refresh() {
    if (topo && svgRoot && lastState) { applyLive(lastState); paintFocus(); }
  }
  return {
    update(state) {
      lastState = state || {};
      const t = lastState.topology;
      if (!t || !t.services || !Object.keys(t.services).length) {
        if (svgRoot) { svgRoot.remove(); svgRoot = null; }
        fp = ""; nodeEls.clear(); edgeEls.length = 0; clear(legend); clear(alt);
        if (!root.querySelector(`.${P}empty`)) root.append(el("div", { class: `${P}empty`, text: "No topology yet — run `faultline topology`." }));
        return;
      }
      const empty = root.querySelector(`.${P}empty`);
      if (empty) empty.remove();
      topo = t;
      const next = hash([
        Object.keys(t.services).sort().map((n) => [n, t.services[n].role, scoreOf(t, n)]),
        (t.edges || []).map((e) => [e.src, e.dst, e.kind]).sort(),
        showObs,
      ]);
      if (next !== fp) { fp = next; rebuild(t); if (selected && !nodeEls.has(selected)) selected = null; }
      applyLive(lastState);
      paintFocus();
    },
    select(name) {
      selected = name && nodeEls.has(name) ? name : null;
      for (const [n, { g }] of nodeEls) g.classList.toggle("is-selected", n === selected);
      paintFocus();
      if (selected) showTip(nodeEls.get(selected).nd); else hideTip();
    },
    destroy() {
      if (svgRoot) svgRoot.remove();
      nodeEls.clear(); edgeEls.length = 0;
      clear(hostEl);
      svgRoot = null; L = null; graph = null; topo = null; lastState = null;
    },
    /** Show a FINISHED experiment's cascade while nothing is live (a live run always wins), or clear it
     *  with null. view: {id, label, note, target, expected[], observed[], edges[CascadeEdge], hops[CascadeHop]} */
    showCascade(v) {
      replay = v && ((v.edges || []).length || (v.hops || []).length) ? v : null;
      refresh();
    },
  };
}
