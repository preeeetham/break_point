// Telemetry visualisation for Faultline Control Room.
// createLiveChart (streaming canvas chart) · createSparkline (KPI micro-chart) ·
// createScoreRing (SVG resilience gauge). Vanilla ES module, zero deps, no build step.
// Contract: docs/UI_CONTRACT.md §1 tokens, §2 /api/state, §3 signatures, §6 a11y.
import { el, svg, clear, cls, fmtPct, fmtMs, fmtDur, clamp, reduceMotion } from "./util.js";

// ----------------------------------------------------------------- THEME cache
// Canvas cannot read CSS custom properties, so resolve tokens once and cache them.
// tokens.css stays authoritative; FALLBACK (CSS keywords, never brand hex) only covers
// the frame before the stylesheet applies. Re-read on the `themechange` event.
const TOKENS = ["ok", "warn", "danger", "critical", "chaos", "cyan", "violet", "blue",
  "text", "text-dim", "text-mute", "grid", "border", "border-strong", "surface-2"];
const FALLBACK = {
  ok: "limegreen", warn: "orange", danger: "red", critical: "crimson", chaos: "darkorange",
  cyan: "aqua", violet: "mediumpurple", blue: "dodgerblue", text: "white",
  "text-dim": "silver", "text-mute": "gray", grid: "rgba(255,255,255,.06)",
  border: "rgba(255,255,255,.12)", "border-strong": "rgba(255,255,255,.22)",
  "surface-2": "rgba(255,255,255,.04)",
};
const THEME = {};
const themeHooks = new Set();
let themeLoaded = false;
let themeWatched = false;

function readTheme() {
  themeLoaded = true;
  if (typeof document === "undefined" || !document.documentElement || !window.getComputedStyle) return;
  const cs = getComputedStyle(document.documentElement);
  for (const n of TOKENS) THEME[n] = (cs.getPropertyValue(`--${n}`) || "").trim() || FALLBACK[n];
}

/** Token colour, resolved and cached. */
function T(name) {
  if (!themeLoaded) readTheme();
  return THEME[name] || FALLBACK[name] || FALLBACK.text;
}

function watchTheme(hook) {
  themeHooks.add(hook);
  if (themeWatched || typeof window === "undefined") return;
  themeWatched = true;
  window.addEventListener("themechange", () => {
    for (const k of Object.keys(THEME)) delete THEME[k];
    themeLoaded = false;
    for (const fn of themeHooks) { try { fn(); } catch { /* one dead host must not stop the rest */ } }
  });
}

// ------------------------------------------------------------- pure maths/maps
/** Latency axis: sqrt (power-0.5) transform. Defined at 0 (log is not) and monotonic;
 *  a 40 ms baseline sits ~12% up a 2400 ms axis where linear would bury it at ~2%. */
function latNorm(ms, ceil) {
  if (!(ceil > 0) || !Number.isFinite(ms)) return 0;
  return clamp(Math.sqrt(Math.max(0, ms) / ceil), 0, 1);
}
const NICE_MS = [25, 50, 100, 150, 200, 300, 400, 600, 800, 1200, 1600, 2400, 3200, 5000, 8000, 12000, 20000];

/** Smallest "nice" axis top that contains max. */
function latCeil(max) {
  const m = Number.isFinite(max) && max > 0 ? max : 100;
  for (const v of NICE_MS) if (m <= v) return v;
  return Math.ceil(m / 5000) * 5000;
}

/** Ticks spaced >=12% apart in *transformed* space, so sqrt crowding never stacks labels. */
function latTicks(ceil) {
  const out = [0];
  let last = 0;
  for (const v of NICE_MS) {
    if (v >= ceil) break;
    const p = latNorm(v, ceil);
    if (p - last >= 0.12) { out.push(v); last = p; }
  }
  if (1 - last < 0.1 && out.length > 1) out.pop();
  out.push(ceil);
  return out;
}

/** Success-rate colour band. Never used alone, always paired with the STATE text. */
function successBand(v) {
  if (!Number.isFinite(v)) return "text-mute";
  return v >= 0.95 ? "ok" : v >= 0.75 ? "warn" : "danger";
}
const STATES = {
  ok: { label: "STEADY", glyph: "●" }, warn: { label: "DEGRADED", glyph: "▲" },
  danger: { label: "FAILING", glyph: "✖" }, "text-mute": { label: "NO DATA", glyph: "○" },
};

/** Phase -> token, per §1 status mapping. */
function phaseTone(phase) {
  return ({ injecting: "chaos", recovering: "warn", soaking: "cyan", rollback: "violet",
    preflight: "blue", analyzing: "violet" })[String(phase || "").toLowerCase()] || "text-mute";
}

function scoreBand(score) {
  if (!Number.isFinite(score)) return "text-mute";
  return score >= 80 ? "ok" : score >= 60 ? "warn" : "danger";
}

/** Ring arc geometry: clockwise sweep from 12 o'clock, driven by stroke-dashoffset. */
function ringGeometry(score, radius) {
  const circumference = 2 * Math.PI * (radius > 0 ? radius : 1);
  const pct = Number.isFinite(score) ? clamp(score, 0, 100) / 100 : 0;
  return { circumference, pct, sweepDeg: pct * 360, offset: circumference * (1 - pct) };
}

/** Contiguous runs; a gap > gapS means the probe stopped, so the line must break. */
function segments(points, gapS) {
  const out = [];
  let cur = [];
  for (const p of points || []) {
    if (!p || !Number.isFinite(p.t)) continue;
    if (cur.length && p.t - cur[cur.length - 1].t > gapS) { out.push(cur); cur = []; }
    cur.push(p);
  }
  if (cur.length) out.push(cur);
  return out;
}

/** Aggregate into <=n equal-time buckets for the off-screen summary table. */
function aggregateBuckets(points, n = 10) {
  const pts = (points || []).filter((p) => p && Number.isFinite(p.t));
  if (!pts.length) return [];
  const t0 = pts[0].t;
  const span = Math.max(pts[pts.length - 1].t - t0, 1e-6);
  const bins = Array.from({ length: n }, () => ({ t0: 0, t1: 0, sw: 0, w: 0, p95: null, n: 0 }));
  for (const p of pts) {
    const b = bins[clamp(Math.floor(((p.t - t0) / span) * n), 0, n - 1)];
    const w = Number.isFinite(p.n) && p.n > 0 ? p.n : 1;
    if (!b.n) b.t0 = p.t;
    b.t1 = p.t; b.n += w;
    if (Number.isFinite(p.success)) { b.sw += p.success * w; b.w += w; }
    if (Number.isFinite(p.p95)) b.p95 = b.p95 === null ? p.p95 : Math.max(b.p95, p.p95);
  }
  return bins.filter((b) => b.n > 0)
    .map((b) => ({ t0: b.t0, t1: b.t1, n: b.n, p95: b.p95, success: b.w > 0 ? b.sw / b.w : null }));
}

/** Mean of the last k successes vs the k before -> one trend word for the aria-label. */
function trendWord(points, k = 10) {
  const v = (points || []).filter((p) => p && Number.isFinite(p.success)).map((p) => p.success);
  if (v.length < 4) return "flat";
  const tail = v.slice(-k);
  const prev = v.slice(-2 * k, -k);
  if (!prev.length) return "flat";
  const mean = (a) => a.reduce((x, y) => x + y, 0) / a.length;
  const d = mean(tail) - mean(prev);
  return d > 0.02 ? "recovering" : d < -0.02 ? "degrading" : "flat";
}

function clockStr(t) {
  if (!Number.isFinite(t)) return "—";
  const d = new Date(t * 1000);
  const p = (x) => String(x).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

// --------------------------------------------------------------- scoped styles
// Lives inside the host subtree (no writes outside hostEl) and only uses §1 tokens.
const CSS = `
.flc{display:flex;flex-direction:column;gap:var(--s2,8px);min-width:0}
.flc-top{display:flex;align-items:center;gap:var(--s3,12px);flex-wrap:wrap;font:600 11px/1 var(--font-sans,system-ui);letter-spacing:.08em;text-transform:uppercase}
.flc-state{display:inline-flex;align-items:center;gap:var(--s1,4px);color:var(--text-dim)}
.flc-state b{font-size:12px}
.flc-meta{color:var(--text-mute);font-weight:500;letter-spacing:.04em;text-transform:none;font-family:var(--font-mono,monospace)}
.flc-btn{margin-left:auto;display:inline-flex;align-items:center;gap:var(--s1,4px);min-height:28px;padding:0 var(--s3,12px);border:1px solid var(--border-strong);border-radius:var(--r1,6px);background:var(--surface-2);color:var(--text-dim);font:600 11px/1 var(--font-sans,system-ui);letter-spacing:.06em;cursor:pointer}
.flc-btn:hover{color:var(--text);border-color:var(--cyan)}
.flc-btn:focus-visible,.flc-cv:focus-visible{outline:2px solid var(--cyan);outline-offset:2px}
.flc-btn[aria-pressed="true"]{color:var(--warn);border-color:var(--warn)}
.flc-wrap{position:relative;width:100%;min-width:0;border:1px solid var(--border);border-radius:var(--r2,10px);background:var(--surface);overflow:hidden}
.flc-cv,.flc-ov{position:absolute;inset:0;display:block;width:100%;height:100%}
.flc-empty{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:var(--s1,4px);color:var(--text-mute);font:500 12px/1.5 var(--font-sans,system-ui);text-align:center;pointer-events:none}
.flc-empty i{font-style:normal;font-size:11px;opacity:.8}
.flc-tip{position:absolute;z-index:2;min-width:118px;padding:var(--s2,8px);border:1px solid var(--border-strong);border-radius:var(--r1,6px);background:var(--surface-2);box-shadow:var(--shadow-2);color:var(--text);font:500 11px/1.5 var(--font-mono,monospace);pointer-events:none;display:none}
.flc-tip[data-on="1"]{display:block}
.flc-tip dt{display:inline;color:var(--text-mute)}
.flc-tip dd{display:inline;margin:0 0 0 var(--s1,4px)}
.flc-tip div+div{margin-top:2px}
.flc-sr{position:absolute;width:1px;height:1px;margin:-1px;padding:0;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0}
.flc-ring{position:relative;display:inline-flex;align-items:center;justify-content:center}
.flc-ring svg{display:block;overflow:visible}
.flc-ring-mid{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;pointer-events:none}
.flc-ring-num{font:700 clamp(28px,3vw,44px)/1 var(--font-mono,monospace);color:var(--text);font-variant-numeric:tabular-nums}
.flc-ring-grade{font:600 11px/1 var(--font-sans,system-ui);letter-spacing:.14em;text-transform:uppercase;color:var(--text-mute)}
.flc-ring[data-dim="1"] .flc-ring-num{color:var(--text-mute)}
.flc-spark{display:block}
`;
const mountStyle = (host) => host.appendChild(el("style", { text: CSS }));

// ================================================================= LIVE CHART
const PAD = { l: 46, r: 56, t: 16, b: 20 };
const AXIS_FONT = '10px ui-monospace, SFMono-Regular, Menlo, monospace';
const TAG_FONT = '600 9px ui-sans-serif, system-ui, sans-serif';

export function createLiveChart(hostEl, opts = {}) {
  const windowS = opts.windowS || 300;    // rolling buffer (contract: 60-300 s)
  const gapS = opts.gapS || 5;            // never connect across a gap wider than this
  const height = opts.height || 220;

  // --- chrome -----------------------------------------------------------------
  clear(hostEl);
  hostEl.classList.add("flc");
  mountStyle(hostEl);
  const glyph = el("span", { "aria-hidden": "true", text: STATES["text-mute"].glyph });
  const stateText = el("b", { text: STATES["text-mute"].label });
  const stateBox = el("span", { class: "flc-state" }, glyph, stateText);
  const meta = el("span", { class: "flc-meta", text: "waiting for telemetry" });
  const pauseBtn = el("button", { type: "button", class: "flc-btn", "aria-pressed": "false",
    title: "Pause live updates (p)", onclick: () => setPaused(!paused) },
    el("span", { "aria-hidden": "true", text: "⏸" }), el("span", { text: "Pause" }));
  const base = el("canvas", { class: "flc-cv", tabindex: "0", role: "img",
    "aria-label": "API probe telemetry: waiting for data." });
  const over = el("canvas", { class: "flc-ov", "aria-hidden": "true" });
  const empty = el("div", { class: "flc-empty" }, el("span", { text: "waiting for telemetry" }),
    el("i", { text: "arm the probe to start sampling the API" }));
  const tip = el("div", { class: "flc-tip", role: "presentation" });
  const wrap = el("div", { class: "flc-wrap", style: { height: `${height}px` } }, base, over, empty, tip);
  const table = el("div", { class: "flc-sr" });
  hostEl.append(el("div", { class: "flc-top" }, stateBox, meta, pauseBtn), wrap, table);

  const bctx = base.getContext ? base.getContext("2d") : null;
  const octx = over.getContext ? over.getContext("2d") : null;
  let pts = [], phases = [], baseline = null, run = null;
  let paused = false, hoverIdx = -1, keyHover = false;
  let W = 0, H = height, raf = 0, geo = null, lastAria = "", lastTableSig = "";

  // --- sizing (high-DPI + ResizeObserver) -------------------------------------
  function fit() {
    const dpr = clamp(window.devicePixelRatio || 1, 1, 3);
    const r = wrap.getBoundingClientRect ? wrap.getBoundingClientRect() : { width: 0, height: 0 };
    W = Math.max(0, Math.round(r.width || 0));
    H = Math.max(0, Math.round(r.height || height));
    for (const [c, cx] of [[base, bctx], [over, octx]]) {
      c.width = Math.max(1, Math.round(W * dpr));
      c.height = Math.max(1, Math.round(H * dpr));
      if (cx) cx.setTransform(dpr, 0, 0, dpr, 0, 0);   // a resize resets the transform
    }
  }
  const onWinResize = () => { fit(); draw(); };
  const ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver(onWinResize) : null;
  if (ro) ro.observe(wrap); else window.addEventListener("resize", onWinResize);
  const repaint = () => { draw(); };
  watchTheme(repaint);

  // --- geometry ---------------------------------------------------------------
  function layout() {
    const t = (baseline && baseline.thresholds) || {};
    const num = (v) => (Number.isFinite(v) ? v : null);
    const th = {
      abortErr: num(t.abort_error_rate_above), abortP95: num(t.abort_p95_ms_above),
      recOk: num(t.recovery_success_rate_at_least), recP95: num(t.recovery_p95_ms_below),
      basP95: baseline && baseline.api ? num(baseline.api.p95_ms) : null,
    };
    const plotW = Math.max(1, W - PAD.l - PAD.r), plotH = Math.max(1, H - PAD.t - PAD.b);
    let tEnd = pts.length ? pts[pts.length - 1].t : Date.now() / 1000;
    for (const ph of phases) if (Number.isFinite(ph && ph.to)) tEnd = Math.max(tEnd, ph.to);
    const tStart = tEnd - windowS;
    // Axis top spans the data *and* every reference line, so no dashed line falls off-canvas.
    let maxMs = 100;
    for (const p of pts) if (Number.isFinite(p.p95)) maxMs = Math.max(maxMs, p.p95);
    for (const v of [th.abortP95, th.recP95, th.basP95 && th.basP95 * 3]) if (v) maxMs = Math.max(maxMs, v);
    const ceil = latCeil(maxMs * 1.05);
    return { th, tStart, tEnd, ceil, plotW, plotH,
      x: (tt) => PAD.l + clamp((tt - tStart) / windowS, 0, 1) * plotW,
      ySuc: (v) => PAD.t + (1 - clamp(v, 0, 1)) * plotH,
      yLat: (ms) => PAD.t + (1 - latNorm(ms, ceil)) * plotH };
  }

  // --- base render ------------------------------------------------------------
  function draw() {
    if (!bctx) return;
    const hasData = pts.length >= 2;
    empty.style.display = hasData ? "none" : "";
    if (W < 80 || H < 60) { geo = null; return; }   // zero-size host: nothing to draw
    geo = layout();
    const c = bctx;
    c.clearRect(0, 0, W, H);
    c.lineJoin = "round"; c.lineCap = "round";
    drawPhases(c, geo);
    drawGrid(c, geo, hasData);
    if (hasData) { drawRefs(c, geo); drawSeries(c, geo); drawCrossings(c, geo); drawLead(c, geo); }
    drawAxes(c, geo, hasData);
    syncA11y(geo, hasData);
    drawOverlay();
  }

  function drawPhases(c, g) {
    for (const ph of phases) {
      if (!ph || !Number.isFinite(ph.from)) continue;
      const to = Number.isFinite(ph.to) ? ph.to : g.tEnd;
      if (to < g.tStart || ph.from > g.tEnd) continue;
      const x0 = g.x(ph.from), w = Math.max(1.5, g.x(to) - x0), col = T(phaseTone(ph.phase));
      c.fillStyle = col;
      c.globalAlpha = 0.11; c.fillRect(x0, PAD.t, w, g.plotH);
      c.globalAlpha = 0.55; c.fillRect(x0, PAD.t, 1.25, g.plotH);   // band leading edge
      if (w > 46) {
        c.globalAlpha = 0.95; c.font = TAG_FONT; c.textAlign = "left"; c.textBaseline = "top";
        c.save(); c.beginPath(); c.rect(x0, PAD.t, w - 3, 14); c.clip();
        c.fillText(String(ph.phase || "").toUpperCase(), x0 + 4, PAD.t + 3);
        c.restore();
      }
      c.globalAlpha = 1;
    }
  }

  function hLine(c, g, y) {
    const yy = Math.round(clamp(y, PAD.t, PAD.t + g.plotH)) + 0.5;   // crisp 1px line
    c.beginPath(); c.moveTo(PAD.l, yy); c.lineTo(PAD.l + g.plotW, yy); c.stroke();
    return yy;
  }

  function drawGrid(c, g, hasData) {
    c.strokeStyle = T("grid"); c.lineWidth = 1; c.setLineDash([]);
    for (const v of [0, 0.25, 0.5, 0.75, 1]) hLine(c, g, g.ySuc(v));
    if (!hasData) return;
    const step = windowS >= 240 ? 60 : 30;
    for (let t = Math.ceil(g.tStart / step) * step; t <= g.tEnd; t += step) {
      const x = Math.round(g.x(t)) + 0.5;
      c.beginPath(); c.moveTo(x, PAD.t); c.lineTo(x, PAD.t + g.plotH); c.stroke();
    }
  }

  function ref(c, g, y, color, label, right) {
    if (!Number.isFinite(y)) return;
    c.save();
    c.strokeStyle = color; c.fillStyle = color; c.globalAlpha = 0.75;
    c.setLineDash([4, 4]); c.lineWidth = 1;
    const yy = hLine(c, g, y);
    c.setLineDash([]); c.font = TAG_FONT; c.textBaseline = "bottom";
    c.textAlign = right ? "right" : "left";
    c.fillText(label, right ? PAD.l + g.plotW - 3 : PAD.l + 4, yy - 2);
    c.restore();
  }

  function drawRefs(c, g) {
    const { th } = g;
    if (th.abortErr !== null) ref(c, g, g.ySuc(1 - th.abortErr), T("danger"), `abort success<${fmtPct(1 - th.abortErr)}`, false);
    if (th.recOk !== null) ref(c, g, g.ySuc(th.recOk), T("ok"), `recovered ≥${fmtPct(th.recOk)}`, false);
    if (th.abortP95 !== null) ref(c, g, g.yLat(th.abortP95), T("danger"), `abort p95>${fmtMs(th.abortP95)}`, true);
    if (th.recP95 !== null) ref(c, g, g.yLat(th.recP95), T("warn"), `recover p95<${fmtMs(th.recP95)}`, true);
    if (th.basP95 !== null) ref(c, g, g.yLat(th.basP95), T("cyan"), `baseline p95 ${fmtMs(th.basP95)}`, true);
  }

  /** Gradient with one stop per sample, so history keeps its own band instead of one flat fill. */
  function bandPaint(c, seg, g) {
    const x0 = g.x(seg[0].t), x1 = g.x(seg[seg.length - 1].t);
    if (!(x1 - x0 > 0.5)) return T(successBand(seg[seg.length - 1].success));
    const grd = c.createLinearGradient(x0, 0, x1, 0);
    for (const p of seg) grd.addColorStop(clamp((g.x(p.t) - x0) / (x1 - x0), 0, 1), T(successBand(p.success)));
    return grd;
  }

  function drawSeries(c, g) {
    const bottom = PAD.t + g.plotH;
    for (const seg of segments(pts, gapS)) {
      const ok = seg.filter((p) => Number.isFinite(p.success));
      if (ok.length) {
        const paint = bandPaint(c, ok, g);
        c.beginPath(); c.moveTo(g.x(ok[0].t), bottom);
        for (const p of ok) c.lineTo(g.x(p.t), g.ySuc(p.success));
        c.lineTo(g.x(ok[ok.length - 1].t), bottom); c.closePath();
        c.globalAlpha = 0.24; c.fillStyle = paint; c.fill(); c.globalAlpha = 1;
        c.beginPath();
        ok.forEach((p, i) => (i ? c.lineTo(g.x(p.t), g.ySuc(p.success)) : c.moveTo(g.x(p.t), g.ySuc(p.success))));
        c.strokeStyle = paint; c.lineWidth = 1.75; c.stroke();
      }
      const lat = seg.filter((p) => Number.isFinite(p.p95));
      if (lat.length > 1) {
        c.beginPath();
        lat.forEach((p, i) => (i ? c.lineTo(g.x(p.t), g.yLat(p.p95)) : c.moveTo(g.x(p.t), g.yLat(p.p95))));
        c.strokeStyle = T("cyan"); c.globalAlpha = 0.95; c.lineWidth = 1.4; c.stroke();
        c.globalAlpha = 1;
      }
    }
  }

  /** Ring the first sample of each threshold breach; capped so a long outage stays readable. */
  function drawCrossings(c, g) {
    const { th } = g;
    if (th.abortErr === null && th.abortP95 === null) return;
    c.strokeStyle = T("danger"); c.lineWidth = 1.5;
    let was = false, drawn = 0;
    for (const p of pts) {
      const bad = (th.abortErr !== null && Number.isFinite(p.success) && p.success < 1 - th.abortErr)
        || (th.abortP95 !== null && Number.isFinite(p.p95) && p.p95 > th.abortP95);
      if (bad && !was && drawn < 24) {
        const y = Number.isFinite(p.success) ? g.ySuc(p.success) : g.yLat(p.p95);
        c.beginPath(); c.arc(g.x(p.t), y, 4, 0, Math.PI * 2); c.stroke();
        drawn++;
      }
      was = bad;
    }
  }

  function dot(c, x, y, r, color, alpha) {
    c.fillStyle = color; c.globalAlpha = alpha;
    c.beginPath(); c.arc(x, y, r, 0, Math.PI * 2); c.fill();
    c.globalAlpha = 1;
  }

  function drawLead(c, g) {
    const last = pts[pts.length - 1];
    if (!last) return;
    const x = g.x(last.t);
    if (Number.isFinite(last.success)) {
      const col = T(successBand(last.success));
      dot(c, x, g.ySuc(last.success), 6.5, col, 0.25);
      dot(c, x, g.ySuc(last.success), 3, col, 1);
    }
    if (Number.isFinite(last.p95)) dot(c, x, g.yLat(last.p95), 2.5, T("cyan"), 1);
  }

  function drawAxes(c, g, hasData) {
    c.font = AXIS_FONT; c.fillStyle = T("text-mute");
    c.textBaseline = "middle"; c.textAlign = "right";
    for (const v of [0, 0.5, 1]) c.fillText(fmtPct(v), PAD.l - 6, g.ySuc(v));
    if (!hasData) return;
    c.textAlign = "left";
    for (const ms of latTicks(g.ceil)) c.fillText(String(ms), PAD.l + g.plotW + 6, g.yLat(ms));
    c.textBaseline = "top";
    c.fillText(clockStr(g.tStart), PAD.l, PAD.t + g.plotH + 4);
    c.textAlign = "right"; c.fillText(clockStr(g.tEnd), PAD.l + g.plotW, PAD.t + g.plotH + 4);
    c.textAlign = "center"; c.fillStyle = T("cyan");
    c.fillText("p95 ms · √ scale", PAD.l + g.plotW / 2, PAD.t + g.plotH + 4);
  }

  // --- overlay: crosshair + tooltip + live scan glow --------------------------
  function drawOverlay() {
    if (!octx || !geo) return;
    const g = geo, c = octx;
    c.clearRect(0, 0, W, H);
    const last = pts[pts.length - 1];
    if (last && !paused && !reduceMotion()) {
      // Pulsing glow trailing the leading edge so the chart reads as live.
      const xe = g.x(last.t), x0 = Math.max(PAD.l, xe - 64);
      if (xe > x0) {
        const grd = c.createLinearGradient(x0, 0, xe, 0);
        grd.addColorStop(0, T("surface-2")); grd.addColorStop(1, T("cyan"));
        c.globalAlpha = 0.07 + 0.06 * (0.5 + 0.5 * Math.sin(Date.now() / 620));
        c.fillStyle = grd; c.fillRect(x0, PAD.t, xe - x0, g.plotH);
        c.globalAlpha = 1;
      }
    }
    const p = pts[hoverIdx];
    if (!p) { tip.dataset.on = "0"; return; }
    const x = g.x(p.t);
    c.save();
    c.strokeStyle = T("border-strong"); c.setLineDash([3, 3]); c.lineWidth = 1;
    c.beginPath(); c.moveTo(Math.round(x) + 0.5, PAD.t); c.lineTo(Math.round(x) + 0.5, PAD.t + g.plotH); c.stroke();
    c.setLineDash([]);
    if (Number.isFinite(p.success)) dot(c, x, g.ySuc(p.success), 3.5, T(successBand(p.success)), 1);
    if (Number.isFinite(p.p95)) dot(c, x, g.yLat(p.p95), 3, T("cyan"), 1);
    c.restore();
    clear(tip);
    const row = (k, v) => el("div", null, el("dt", { text: k }), el("dd", { text: v }));
    tip.append(row("time", clockStr(p.t)), row("success", fmtPct(p.success, 1)),
      row("p95", fmtMs(p.p95)), row("samples", Number.isFinite(p.n) ? String(p.n) : "—"));
    tip.dataset.on = "1";
    tip.style.left = `${clamp(x + 12, 4, Math.max(4, W - (tip.offsetWidth || 130) - 4))}px`;
    tip.style.top = `${PAD.t + 4}px`;
  }

  // Only the overlay animates; the base canvas redraws once per poll.
  function tick() { raf = 0; drawOverlay(); schedule(); }
  function schedule() {
    if (raf || typeof requestAnimationFrame === "undefined") return;
    if (paused || reduceMotion() || pts.length < 2) return;
    raf = requestAnimationFrame(tick);
  }

  // --- a11y: state text, aria-label, off-screen summary table -----------------
  // The text state must match what is painted: under 2 samples the chart shows the
  // waiting overlay, so the label and aria-label stay in the waiting state too.
  function syncA11y(g, hasData) {
    const last = pts[pts.length - 1];
    const band = hasData ? successBand(last.success) : "text-mute";
    const st = STATES[band];
    stateText.textContent = st.label;
    glyph.textContent = st.glyph;
    stateBox.style.color = `var(--${band === "text-mute" ? "text-mute" : band})`;
    const phase = run && run.phase && run.phase !== "idle" ? String(run.phase) : null;
    const span = hasData ? fmtDur(Math.max(0, g.tEnd - pts[0].t)) : "0s";
    meta.textContent = hasData
      ? `${fmtPct(last.success, 1)} · p95 ${fmtMs(last.p95)} · ${span}${phase ? ` · ${phase}` : ""}${paused ? " · PAUSED" : ""}`
      : `waiting for telemetry${pts.length ? ` · ${pts.length} sample` : ""}`;
    const aria = hasData
      ? `API probe, last ${span}: success ${fmtPct(last.success, 1)}, p95 ${fmtMs(last.p95)}, state ${st.label}, trend ${trendWord(pts)}${phase ? `, phase ${phase}` : ""}. Arrow keys inspect samples.`
      : `API probe telemetry: waiting for data, ${pts.length} sample${pts.length === 1 ? "" : "s"} so far.`;
    if (aria !== lastAria) { base.setAttribute("aria-label", aria); lastAria = aria; }
    syncTable();
  }

  function syncTable() {
    const buckets = aggregateBuckets(pts, 10);
    const sig = buckets.map((b) => `${Math.round(b.t1)}:${b.success}:${b.p95}`).join("|");
    if (sig === lastTableSig) return;            // skip churn for screen readers
    lastTableSig = sig;
    clear(table);
    if (!buckets.length) return;
    const head = ["time", "success", "p95", "samples"].map((t) => el("th", { scope: "col", text: t }));
    table.appendChild(el("table", null,
      el("caption", { text: "API probe telemetry, last 10 aggregated buckets" }),
      el("thead", null, el("tr", null, ...head)),
      el("tbody", null, ...buckets.map((b) => el("tr", null,
        el("th", { scope: "row", text: clockStr(b.t1) }), el("td", { text: fmtPct(b.success, 1) }),
        el("td", { text: fmtMs(b.p95) }), el("td", { text: String(b.n) }))))));
  }

  // --- interaction ------------------------------------------------------------
  function idxAtX(px) {
    if (!geo || !pts.length) return -1;
    let best = -1, bd = Infinity;
    for (let i = 0; i < pts.length; i++) {
      const d = Math.abs(geo.x(pts[i].t) - px);
      if (d < bd) { bd = d; best = i; }
    }
    return bd <= 28 ? best : -1;
  }
  const onMove = (ev) => {
    const r = over.getBoundingClientRect();
    keyHover = false;
    hoverIdx = idxAtX(ev.clientX - r.left);
    drawOverlay();
  };
  const onLeave = () => { if (!keyHover) { hoverIdx = -1; drawOverlay(); } };
  const onBlur = () => { if (keyHover) { keyHover = false; hoverIdx = -1; drawOverlay(); } };
  const onKey = (ev) => {
    const k = ev.key;
    if (k === "p" || k === "P") { setPaused(!paused); ev.preventDefault(); return; }
    if (!pts.length) return;
    if (k === "ArrowLeft" || k === "ArrowRight") {
      keyHover = true;
      const cur = hoverIdx < 0 ? pts.length - 1 : hoverIdx;
      hoverIdx = clamp(cur + (k === "ArrowRight" ? 1 : -1), 0, pts.length - 1);
    } else if (k === "Home") { keyHover = true; hoverIdx = 0; }
    else if (k === "End") { keyHover = true; hoverIdx = pts.length - 1; }
    else if (k === "Escape") { keyHover = false; hoverIdx = -1; }
    else return;
    ev.preventDefault();
    drawOverlay();
  };
  over.addEventListener("pointermove", onMove);
  over.addEventListener("pointerleave", onLeave);
  base.addEventListener("keydown", onKey);
  base.addEventListener("blur", onBlur);

  function setPaused(v) {
    paused = Boolean(v);
    pauseBtn.setAttribute("aria-pressed", paused ? "true" : "false");
    pauseBtn.firstChild.textContent = paused ? "▶" : "⏸";
    pauseBtn.lastChild.textContent = paused ? "Resume" : "Pause";
    pauseBtn.title = paused ? "Resume live updates (p)" : "Pause live updates (p)";
    if (!paused) draw();
    else { if (geo) syncA11y(geo, pts.length >= 2); drawOverlay(); }
    schedule();
  }

  fit();
  draw();
  schedule();

  return {
    /** Called every poll; diffs into the live canvases, never rebuilds the subtree. */
    update(telemetry, runState, baselineState) {
      const t = telemetry || {};
      run = runState || null;
      if (baselineState !== undefined) baseline = baselineState || null;
      if (paused) return;                        // hold the frozen frame, drop this tick
      pts = Array.isArray(t.points) ? t.points : [];
      phases = Array.isArray(t.phases) ? t.phases : [];
      if (hoverIdx >= pts.length) hoverIdx = pts.length - 1;
      draw();
      schedule();
    },
    setPaused,
    destroy() {
      if (raf && typeof cancelAnimationFrame !== "undefined") cancelAnimationFrame(raf);
      raf = 0;
      if (ro) ro.disconnect(); else window.removeEventListener("resize", onWinResize);
      over.removeEventListener("pointermove", onMove);
      over.removeEventListener("pointerleave", onLeave);
      base.removeEventListener("keydown", onKey);
      base.removeEventListener("blur", onBlur);
      themeHooks.delete(repaint);
      clear(hostEl);
      hostEl.classList.remove("flc");
    },
  };
}

// ================================================================== SPARKLINE
export function createSparkline(hostEl, opts = {}) {
  const w = opts.width || 60, h = opts.height || 18, tone = opts.tone || "cyan";
  clear(hostEl);
  const cv = el("canvas", { class: "flc-spark", "aria-hidden": "true",
    style: { width: `${w}px`, height: `${h}px` } });
  hostEl.appendChild(cv);
  const ctx = cv.getContext ? cv.getContext("2d") : null;
  let vals = [];

  const dpr = clamp(window.devicePixelRatio || 1, 1, 3);
  cv.width = Math.max(1, Math.round(w * dpr));
  cv.height = Math.max(1, Math.round(h * dpr));
  if (ctx) ctx.setTransform(dpr, 0, 0, dpr, 0, 0);   // crisp at 1x and 2x

  function draw() {
    if (!ctx) return;
    ctx.clearRect(0, 0, w, h);
    const n = vals.length, pad = 2.5;
    if (!n) return;
    let lo = Math.min(...vals), hi = Math.max(...vals);
    if (hi - lo < 1e-9) { lo -= 1; hi += 1; }        // flat series -> centre line
    const sx = (i) => (n === 1 ? w / 2 : pad + (i / (n - 1)) * (w - 2 * pad));
    const sy = (v) => h - pad - ((v - lo) / (hi - lo)) * (h - 2 * pad);
    ctx.strokeStyle = T(tone);
    ctx.lineWidth = opts.weight || 1.4;
    ctx.lineJoin = "round"; ctx.lineCap = "round";
    ctx.beginPath();
    ctx.moveTo(sx(0), sy(vals[0]));
    // Quadratic through segment midpoints: smooth without overshooting the samples.
    for (let i = 1; i < n; i++) {
      ctx.quadraticCurveTo(sx(i - 1), sy(vals[i - 1]),
        (sx(i - 1) + sx(i)) / 2, (sy(vals[i - 1]) + sy(vals[i])) / 2);
    }
    ctx.lineTo(sx(n - 1), sy(vals[n - 1]));
    ctx.stroke();
    ctx.fillStyle = T(tone);
    ctx.beginPath(); ctx.arc(sx(n - 1), sy(vals[n - 1]), 1.9, 0, Math.PI * 2); ctx.fill();
  }
  watchTheme(draw);

  return {
    update(values) {
      vals = (Array.isArray(values) ? values : []).filter((v) => Number.isFinite(v));
      draw();
    },
    destroy() { themeHooks.delete(draw); clear(hostEl); },
  };
}

// ================================================================= SCORE RING
export function createScoreRing(hostEl, opts = {}) {
  const size = opts.size || 136, stroke = opts.stroke || 11;
  const r = (size - stroke) / 2 - 1;                  // inset so the round cap never clips
  const { circumference } = ringGeometry(0, r);
  clear(hostEl);
  const track = svg("circle", { cx: size / 2, cy: size / 2, r, fill: "none",
    "stroke-width": stroke, style: "stroke: var(--border-strong)" });
  const arc = svg("circle", { cx: size / 2, cy: size / 2, r, fill: "none", "stroke-width": stroke,
    "stroke-linecap": "round", transform: `rotate(-90 ${size / 2} ${size / 2})`,  // sweep from 12 o'clock
    "stroke-dasharray": circumference.toFixed(2), "stroke-dashoffset": circumference.toFixed(2) });
  const num = el("div", { class: "flc-ring-num", text: "—" });
  const grade = el("div", { class: "flc-ring-grade", text: "not tested" });
  const wrap = el("div", { class: cls("flc-ring", opts.class), role: "img", "data-dim": "1",
    "aria-label": "Resilience score not tested", style: { width: `${size}px`, height: `${size}px` } },
    svg("svg", { width: size, height: size, viewBox: `0 0 ${size} ${size}`, "aria-hidden": "true" }, track, arc),
    el("div", { class: "flc-ring-mid" }, num, grade));
  mountStyle(hostEl);
  hostEl.appendChild(wrap);

  let prev = null, lastGrade = null;
  function paint(score, gradeStr) {
    const g = ringGeometry(score, r);
    const dim = !Number.isFinite(score);
    const band = scoreBand(score);
    arc.style.stroke = `var(--${dim ? "border-strong" : band})`;
    arc.style.transition = reduceMotion() ? "none" : "stroke-dashoffset var(--t-slow, 420ms) var(--ease, ease)";
    arc.setAttribute("stroke-dashoffset", g.offset.toFixed(2));
    num.textContent = dim ? "—" : String(Math.round(score));
    grade.textContent = dim ? "not tested" : `grade ${String(gradeStr || "—").toUpperCase()}`;
    grade.style.color = `var(--${dim ? "text-mute" : band})`;
    wrap.dataset.dim = dim ? "1" : "0";
    wrap.setAttribute("aria-label", dim ? "Resilience score not tested"
      : `Resilience score ${Math.round(score)} out of 100, grade ${String(gradeStr || "unknown").toUpperCase()}`);
  }
  const repaint = () => paint(prev, lastGrade);
  watchTheme(repaint);

  return {
    update(score, gradeStr) {
      const s = Number.isFinite(score) ? clamp(score, 0, 100) : null;
      lastGrade = gradeStr === undefined ? lastGrade : gradeStr;
      if (s !== null && prev === null && !reduceMotion() && typeof requestAnimationFrame !== "undefined") {
        paint(0, lastGrade);                      // park at 0 so the first value animates in
        requestAnimationFrame(() => paint(s, lastGrade));
      } else {
        paint(s, lastGrade);
      }
      prev = s;
    },
    destroy() { themeHooks.delete(repaint); clear(hostEl); },
  };
}

// Pure helpers, exported for unit tests only, not part of the §3 contract.
export const __internals = {
  THEME, latNorm, latCeil, latTicks, successBand, phaseTone, scoreBand,
  ringGeometry, segments, aggregateBuckets, trendWord, clockStr, STATES, PAD,
};
