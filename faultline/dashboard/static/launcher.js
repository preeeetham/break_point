// launcher.js — the injection console: choose what to break, with what parameters, and launch it.
// Contract: docs/UI_CONTRACT.md §2 (payloads), §3 (export), §4 (classes), §5 (explain), §6 (a11y).
// Owns its subtree inside hostEl, writes nowhere else. update() is idempotent and never clobbers a
// control the operator has touched (see TOUCHED below).
//
// PRESENTATION: this module renders onto app.css's own vocabulary — .panel/.panel__head/.panel__body,
// .fieldset/.fieldset__legend, .optgrid/.card/.card__input, .params/.param, .btn*, .chip/.pill/.badge/
// .dot/.bar/.stat, .hypothesis, .explain__btn/.explain__body and [data-tone]. The scoped <style> at the
// bottom is injected unconditionally and is limited to the handful of things app.css does not define
// (the hold-to-launch progress, the mode segmented control, the toggle switch, the candidate rows).
import {
  el, clear, cls, svgIcon, statusTone, roleIcon, fmtPct, fmtDur, titleCase, clamp, announce,
} from "./util.js";

// ----------------------------------------------------------------- explain copy (§5)
const EXPLAIN = {
  console: { title: "What is this panel?",
    body: "Chaos engineering means deliberately breaking one piece of a running system to find out how it really behaves under failure. This console picks the victim, the kind of failure, and how long it lasts — then runs it against a sandboxed Eclipse Ditto IoT platform while probes measure the damage." },
  mode: { title: "AI planner vs manual",
    body: "In AI PLANNER the system ranks every service/failure pair by how much we would learn, and an LLM (or a deterministic rule set when no model is reachable) picks the next experiment and writes a falsifiable hypothesis. MANUAL hands every choice to you — same safety rails either way." },
  plan: { title: "Reading the ranked plan",
    body: "The planner scores every legal service/failure pair: how critical the service is, how little we have tested it, how much the result would teach us. #1 is the pick unless the LLM argues for another of the top five. 'Deterministic' means no model was reachable and the ranking decided on its own." },
  target: { title: "Which service gets broken",
    body: "One card per container in the sandbox, sorted by criticality: how many other services transitively depend on it. MongoDB leads because everything stores state there. Locked cards are refused by policy — a full outage of the only datastore is off the table unless you explicitly override." },
  failure: { title: "The ten failure modes",
    body: "Each card is one real-world failure: a crash, a hung process, a slow or lossy network, an overloaded database, a full disk, a traffic spike. 'Outage' takes the service away completely; 'degrade-only' leaves it running but unhealthy. Greyed-out cards say exactly why they cannot run here." },
  params: { title: "Blast radius dials",
    body: "Every dial is clamped to a range the safety policy allows — you cannot ask for a longer or harsher fault than the code permits. The line underneath spells out the worst case in seconds so nothing is a surprise." },
  hypothesis: { title: "Why a hypothesis first",
    body: "A chaos experiment is only an experiment if it can fail. Before launching we commit to a falsifiable prediction: how bad the error rate gets, who else is dragged down, and how fast it heals. Afterwards the report says HELD or VIOLATED — a violated hypothesis is the interesting result." },
  safety: { title: "The abort rails",
    body: "These limits are enforced by code, never by the model: the run aborts itself the moment the error rate, latency, or collateral damage crosses a line, and an independent watchdog force-restores everything even if the main loop hangs. STOP is always one click away." },
  launch: { title: "Launching safely",
    body: "Press and hold — a single stray click cannot start an experiment. Once it runs, this area becomes a live status block with the phase, the countdown to the hard cap, and one big ABORT." },
  actions: { title: "Supporting actions",
    body: "Rebuild map re-reads the compose file and live Docker state. Scout weaknesses statically predicts where the deployment is weak without breaking anything — every result is labelled [predicted, unconfirmed]. Capture baseline measures what 'normal' looks like so the abort thresholds are calibrated. Heal sandbox removes leftovers after a messy run. Arm starts the synthetic probe traffic that all measurements come from." },
};

const SECTION_ICON = {
  console: "crosshair", mode: "brain", plan: "brain", target: "server", failure: "skull",
  params: "gauge", hypothesis: "flask", safety: "shield", launch: "zap", actions: "wrench",
};

const HOLD_MS = 1200;
const PHASES = ["preflight", "injecting", "rollback", "recovering", "soaking", "analyzing"];
const ACTION_SPECS = [
  { kind: "topology", icon: "grid", label: "Rebuild map", body: () => ({}) },
  { kind: "scout", icon: "search", label: "Scout weaknesses", body: (s) => ({ llm: s.llm }) },
  { kind: "baseline", icon: "gauge", label: "Capture baseline", body: () => ({ duration: 60 }) },
  { kind: "analyze", icon: "brain", label: "Run analysis", body: (s) => ({ llm: s.llm }) },
  { kind: "heal", icon: "wrench", label: "Heal sandbox", confirm: true, body: (s) => ({ restart: s.healRestart }) },
  { kind: "arm", icon: "shield", label: "Arm probes", body: () => ({}) },
  { kind: "disarm", icon: "stop", label: "Disarm", body: () => ({}) },
  { kind: "cancel", icon: "x", label: "Cancel job", body: () => ({}) },
];

const reduceMotion = () => !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
const sig = (v) => { try { return JSON.stringify(v); } catch { return String(v); } };
/** Seconds the way the project writes them in prose ("30 s", "90 s") — fmtDur is for live KPIs. */
const fmtSecs = (s) => (Number.isFinite(Number(s)) ? `${Math.round(Number(s))} s` : "— s");
const list = (a) => {
  const x = (a || []).filter(Boolean);
  if (!x.length) return "nothing else";
  if (x.length === 1) return x[0];
  return `${x.slice(0, -1).join(", ")} and ${x[x.length - 1]}`;
};

// ----------------------------------------------------------------- app.css component builders
// Thin wrappers so every status surface in this panel is one of app.css's tone components and the
// [data-tone] attribute is the only colour channel we ever touch.
const chip = (text, tone, icon) => el("span", { class: "chip", "data-tone": tone || "neutral" },
  icon ? el("span", { class: "chip__glyph" }, svgIcon(icon, 12)) : el("span", { class: "chip__glyph" }),
  el("span", { class: "chip__label", text }));

const pill = (text, tone, icon) => el("span", { class: "pill", "data-tone": tone || "neutral" },
  icon ? svgIcon(icon, 14) : null, el("span", { class: "nowrap", text }));

const badge = (text, tone) => el("span", { class: cls("badge", !tone && "badge--plain"), "data-tone": tone || null }, text);

const dot = (tone, pulse) => el("span", {
  class: cls("dot", pulse && "dot--pulse"), "data-tone": tone || "neutral", "aria-hidden": "true",
});

function meter(v, tone) {
  const node = el("div", { class: "bar", "data-tone": tone || "cyan" }, el("span", { class: "bar__fill" }));
  node.style.setProperty("--v", String(clamp(Number(v) || 0, 0, 1)));
  return node;
}

const stat = (label, value, tone, sub) => el("div", { class: "stat", "data-tone": tone || null },
  el("span", { class: "stat__label", text: label }),
  el("b", { class: "stat__value truncate", text: value }),
  sub ? el("span", { class: "stat__sub truncate", text: sub }) : null);

const banner = (tone, icon, ...kids) => el("div", { class: "banner", "data-tone": tone || "neutral" },
  svgIcon(icon || "info", 15), ...kids);

const hint = (...kids) => el("p", { class: "fieldset__hint" }, ...kids);

const emptyState = (icon, title, body) => el("div", { class: "empty" },
  el("div", { class: "empty__icon" }, svgIcon(icon, 20)),
  el("p", { class: "empty__title", text: title }),
  el("p", { class: "empty__hint", text: body }));

const errorBox = (text) => el("p", { class: "launcher__error row", role: "alert" },
  svgIcon("x", 14), el("span", { text }));

const sevOf = (score) => (score >= 8 ? "critical" : score >= 5 ? "high" : score >= 2 ? "medium" : "low");

// ----------------------------------------------------------------- normalisation
// The backend has shipped two spellings of the catalog (contract §2 vs control.py). Accept both and
// derive anything missing, so a field rename degrades to a visible explanation, never a blank card.
function flatThresholds(...sources) {
  const out = {};
  for (const src of sources) {
    if (!src || typeof src !== "object") continue;
    for (const [k, v] of Object.entries(src)) {
      if (v && typeof v === "object" && !Array.isArray(v)) Object.assign(out, v);
      else out[k] = v;
    }
  }
  return out;
}

function normFailures(catalog) {
  const images = (catalog && catalog.images) || {};
  const svcRows = (catalog && catalog.services) || [];
  const protectedMap = normProtected(catalog);
  const raw = (catalog && catalog.failures) || [];
  return raw.map((f) => {
    const key = f.key || f.name;
    const params = (f.params || []).map((p) => ({
      name: p.name,
      type: p.type || (typeof p.default === "boolean" ? "bool" : "number"),
      default: p.default,
      lo: p.lo === null || p.lo === undefined ? null : Number(p.lo),
      hi: p.hi === null || p.hi === undefined ? null : Number(p.hi),
      unit: p.unit || "",
      desc: p.desc || "",
    }));
    // applies_to: explicit list, else the per-service "applicable" lists, else role matching.
    let appliesTo = Array.isArray(f.applies_to) ? f.applies_to.slice() : null;
    if (!appliesTo && svcRows.length) {
      appliesTo = svcRows.filter((s) => (s.applicable || []).includes(key)).map((s) => s.name);
      for (const s of svcRows) if (s.blocked && s.blocked[key] && !appliesTo.includes(s.name)) appliesTo.push(s.name);
    }
    const missing = (f.requires_images || []).filter((img) => images[img] === false);
    const available = f.available !== undefined ? !!f.available : missing.length === 0;
    let blockedOn = Array.isArray(f.policy_blocked_on) ? f.policy_blocked_on.slice() : null;
    if (!blockedOn) {
      blockedOn = Object.entries(protectedMap)
        .filter(([, p]) => !(p.allowed_failure_types || []).includes(key))
        .map(([name]) => name);
    }
    return {
      key,
      title: f.title || titleCase(key),
      category: f.slide_category || f.category || "",
      description: f.description || "",
      degradeOnly: !!f.degrade_only,
      recoveryS: Number(f.expected_recovery_s ?? 20),
      severity: Number(f.severity_hint ?? (f.degrade_only ? 0.3 : 1.0)),
      roles: f.roles || [],
      requiresImages: f.requires_images || [],
      available,
      unavailableReason: f.unavailable_reason
        || (missing.length ? `helper image missing: ${missing.join(", ")} — run \`faultline setup\`` : null),
      appliesTo: appliesTo ? new Set(appliesTo) : null,
      blockedOn: new Set(blockedOn),
      params,
    };
  });
}

function normProtected(catalog) {
  if (catalog && catalog.protected && typeof catalog.protected === "object") return catalog.protected;
  const out = {};
  for (const s of (catalog && catalog.services) || []) {
    if (s.protected) out[s.name] = { reason: s.protected_reason || "protected by policy", allowed_failure_types: s.protected_allowed || [] };
  }
  return out;
}

function normServices(state, catalog) {
  const topo = (state && state.topology) || null;
  const rows = (catalog && catalog.services) || [];
  const byName = new Map(rows.map((s) => [s.name, s]));
  const crit = new Map(((topo && topo.criticality) || []).map((c) => [c.service, c]));
  const health = ((state && state.run) || {}).health || {};
  const src = (topo && topo.services) || {};
  const names = Object.keys(src).length ? Object.keys(src) : rows.map((s) => s.name);
  const out = [];
  for (const name of names) {
    const node = src[name] || byName.get(name) || {};
    const extra = byName.get(name) || {};
    const relevant = node.chaos_relevant !== undefined ? node.chaos_relevant
      : (extra.chaos_relevant !== undefined ? extra.chaos_relevant : true);
    if (!relevant) continue;
    const live = health[name] || {};
    out.push({
      name,
      role: node.role || extra.role || "other",
      stateful: !!(node.stateful ?? extra.stateful),
      criticality: Number(node.criticality ?? extra.criticality ?? (crit.get(name) || {}).score ?? 0),
      health: live.status || node.health || node.state || extra.health || extra.state || "unknown",
      container: node.container || extra.container || "",
      dependents: (extra.direct_dependents && extra.direct_dependents.length)
        ? extra.direct_dependents
        : dependentsOf(topo, name),
      transitive: extra.transitive_dependents || (crit.get(name) || {}).transitive_dependents || [],
      blocked: extra.blocked || {},
      applicable: extra.applicable || null,
    });
  }
  out.sort((a, b) => (b.criticality - a.criticality) || a.name.localeCompare(b.name));
  return out;
}

/** Direct dependents from the edge list (src depends on dst), used when the catalog is absent. */
function dependentsOf(topo, name) {
  const edges = (topo && topo.edges) || [];
  const seen = new Set();
  for (const e of edges) if (e.dst === name && e.src !== name) seen.add(e.src);
  return [...seen].sort();
}

/** Why this (service, failure) pair cannot run — null when it can. */
function blockReason(svc, ft, allowProtected, protectedMap) {
  if (!svc || !ft) return null;
  if (svc.blocked && svc.blocked[ft.key] && !(allowProtected && ft.blockedOn.has(svc.name))) return svc.blocked[ft.key];
  if (ft.appliesTo && !ft.appliesTo.has(svc.name)) {
    return `${ft.title} does not apply to ${svc.name} (a ${svc.role} service)`;
  }
  if (!ft.available) return ft.unavailableReason || "the tooling this failure needs is not installed";
  if (ft.blockedOn.has(svc.name) && !allowProtected) {
    const p = protectedMap[svc.name] || {};
    const allowed = (p.allowed_failure_types || []).join(", ");
    return `${svc.name} is protected: ${p.reason || "policy blocks this failure type"}${allowed ? ` — allowed here: ${allowed}` : ""}`;
  }
  return null;
}

// ----------------------------------------------------------------- the module
export function createLauncher(hostEl, handlers = {}) {
  const { onLaunch, onPlan, onStop, onJob } = handlers;

  // ---- local UI state. TOUCHED is the dirty-state ledger: any key in it is owned by the operator
  // and update() must never write to it again.
  const S = {
    mode: "auto",
    target: null,
    failure: null,
    params: {},
    allowProtected: false,
    llm: true,
    healRestart: false,
    launchPending: false,
    planPending: false,
    jobPending: null,
    launchError: null,
    actionError: null,
    confirmAction: null,
    holdPct: 0,
    confirmArmed: false,
    dirty: {},                 // operator-set param values, keyed per failure type
    explain: new Set(),
  };
  const TOUCHED = new Set();
  let D = { state: {}, catalog: null, plan: null };
  let holdRaf = 0, holdStart = 0, confirmTimer = 0, lastPhase = "idle";
  const SIGS = {};
  const SECTIONS = {};

  // ---- skeleton: .panel > .panel__head + .explain__body + form.panel__body + .panel__foot
  hostEl.classList.add("panel", "fl-launch-root");
  clear(hostEl);
  hostEl.appendChild(el("style", { text: SCOPED_CSS }));

  const head = el("div", { class: "panel__head" },
    el("div", { class: "panel__title" },
      el("span", { class: "panel__icon" }, svgIcon(SECTION_ICON.console, 15)),
      el("span", { text: "Injection console" })));
  const headActions = el("div", { class: "panel__actions" });
  head.appendChild(headActions);

  const consoleExplain = explainBodyFor("console");
  headActions.appendChild(explainButtonFor("console", "Injection console"));
  SECTIONS.console = { explainBody: consoleExplain, btn: headActions.lastChild };

  const form = el("form", { class: "panel__body launcher fl-launch-form", novalidate: "" });
  form.addEventListener("submit", (e) => { e.preventDefault(); });

  const statusBox = el("div", { class: "row row--wrap fl-launch-status" });

  const modeSec = section("mode", "Mode");
  const planSec = section("plan", "AI planner", { extra: "fl-launch-plan" });
  const targetSec = section("target", "Target service", { legend: true });
  const failureSec = section("failure", "Failure type", { legend: true });
  const paramSec = section("params", "Parameters", { legend: true });
  const hypSec = section("hypothesis", "Hypothesis");
  const safetySec = section("safety", "Safety rails");
  const actionSec = section("actions", "Operations");

  form.append(statusBox, modeSec.root, planSec.root, targetSec.root, failureSec.root,
    paramSec.root, hypSec.root, safetySec.root, actionSec.root);

  // The launch trigger lives in the panel foot so it — and ABORT — are reachable without scrolling.
  const launchTitle = el("span", { class: "micro", text: "Launch" });
  const launchHead = el("div", { class: "row fl-launch-foothead" },
    svgIcon(SECTION_ICON.launch, 13), launchTitle, el("span", { class: "spacer" }),
    explainButtonFor("launch", "Launch"));
  const launchExplain = explainBodyFor("launch");
  SECTIONS.launch = { explainBody: launchExplain, btn: launchHead.lastChild };
  const launchBox = el("div", { class: "fl-launch-launch" });
  const foot = el("div", { class: "panel__foot fl-launch-foot" }, launchHead, launchExplain, launchBox);

  hostEl.append(head, consoleExplain, form, foot);

  // ---- explain disclosure (§5). app.css opens these with data-open="true" (0fr -> 1fr), so the
  // toggle never touches [hidden] and the bodies stay in the DOM — which also lets app.js's global
  // "Explain" button drive them through aria-controls without this module re-rendering.
  function explainBodyFor(key) {
    const copy = EXPLAIN[key] || {};
    return el("div", { class: "explain__body", id: `fl-ex-${key}`, "data-open": "false" },
      el("div", { class: "explain__inner" },
        el("strong", { text: copy.title || "What is this?" }),
        el("p", { text: copy.body || "" })));
  }

  function explainButtonFor(key, titleText) {
    return el("button", {
      type: "button", class: "explain__btn", "aria-expanded": "false",
      "aria-controls": `fl-ex-${key}`, title: (EXPLAIN[key] || {}).title || "What is this?",
      "aria-label": `Explain: ${titleText}`,
      onclick: () => setExplain(key, (SECTIONS[key].explainBody.dataset.open !== "true")),
    }, svgIcon("info", 15));
  }

  function setExplain(key, open) {
    const s = SECTIONS[key];
    if (!s) return;
    s.explainBody.dataset.open = String(open);
    s.btn.setAttribute("aria-expanded", String(open));
    if (open) S.explain.add(key); else S.explain.delete(key);
  }

  /** One collapsible section: legend (+ actions slot + explain), disclosure body, content body. */
  function section(key, titleText, { legend = false, extra = null } = {}) {
    const titleEl = el(legend ? "legend" : "h3", { class: "fieldset__legend" },
      svgIcon(SECTION_ICON[key] || "info", 13), el("span", { class: "truncate", text: titleText }));
    const actions = el("span", { class: "fl-launch-subacts" });
    const btn = explainButtonFor(key, titleText);
    titleEl.append(el("span", { class: "spacer" }), actions, btn);
    const body = el("div", { class: "fl-launch-secbody" });
    const explain = explainBodyFor(key);
    const root = el(legend ? "fieldset" : "section", { class: cls("fieldset", "fl-launch-sec", extra) },
      titleEl, explain, body);
    SECTIONS[key] = { root, title: titleEl, actions, body, explainBody: explain, btn };
    return SECTIONS[key];
  }

  function setTouched(key) { TOUCHED.add(key); }

  // ---- derived view model, recomputed per render (cheap: tens of entries)
  function derive() {
    const state = D.state || {};
    const catalog = D.catalog;
    const failures = normFailures(catalog);
    const protectedMap = normProtected(catalog);
    const services = normServices(state, catalog);
    const th = flatThresholds((catalog || {}).thresholds, ((state.baseline || {}).thresholds));
    const run = state.run || {};
    const job = state.job || null;
    const session = state.session || {};
    const busy = !!(job && job.status === "running");
    const running = !!(run.phase && run.phase !== "idle");
    const plan = D.plan || null;
    const pick = (plan && plan.pick) || null;

    // adopt backend/planner values only for untouched controls
    if (!TOUCHED.has("mode") && state.run && state.run.planned_by === "operator") S.mode = "manual";
    if (S.mode === "auto" && pick) {
      if (!TOUCHED.has("target")) S.target = pick.target_service || S.target;
      if (!TOUCHED.has("failure")) S.failure = pick.failure_type || S.failure;
    }
    if (!TOUCHED.has("llm")) {
      const l = state.llm || {};
      if (l.available === false || l.enabled === false) S.llm = false;
      else if (l.available) S.llm = true;
    }
    const eligible = services.filter((s) => failures.some((f) => !blockReason(s, f, S.allowProtected, protectedMap)));
    if (!TOUCHED.has("target") && !S.target && eligible.length) S.target = eligible[0].name;
    const svc = services.find((s) => s.name === S.target) || null;
    const okFailures = failures.filter((f) => !blockReason(svc, f, S.allowProtected, protectedMap));
    if (!TOUCHED.has("failure") && (!S.failure || !failures.some((f) => f.key === S.failure)) && okFailures.length) {
      S.failure = okFailures[0].key;
    }
    const ft = failures.find((f) => f.key === S.failure) || null;

    // params: per-param dirty tracking
    const params = {};
    for (const p of (ft ? ft.params : [])) {
      const k = `param:${ft.key}:${p.name}`;
      if (TOUCHED.has(k) && S.dirty[k] !== undefined) {
        params[p.name] = S.dirty[k];
      } else {
        const fromPick = S.mode === "auto" && pick && pick.failure_type === ft.key ? (pick.parameters || {})[p.name] : undefined;
        const v = fromPick !== undefined ? fromPick : p.default;
        params[p.name] = p.type === "bool" ? !!v : clampParam(p, Number(v));
      }
    }
    S.params = params;

    const block = blockReason(svc, ft, S.allowProtected, protectedMap);
    const reasons = [];
    if (!state.topology && !services.length) reasons.push("no service map yet — press Rebuild map");
    if (state.stop_requested) reasons.push("STOP is latched — clear it in the header before launching");
    if (busy) reasons.push(`a ${job.kind} job is already running`);
    if (!session.armed) reasons.push("probes are disarmed — Arm probes so the run can be measured");
    if (S.mode === "auto" && !pick) reasons.push("the planner has no pick yet — press Re-plan");
    if (!svc) reasons.push("pick a target service");
    if (!ft) reasons.push("pick a failure type");
    if (block) reasons.push(block);

    return { state, catalog, failures, protectedMap, services, th, run, job, session, busy, running, plan, pick, svc, ft, block, reasons, eligible };
  }

  function clampParam(p, v) {
    if (!Number.isFinite(v)) v = Number(p.default) || 0;
    return clamp(v, p.lo ?? -Infinity, p.hi ?? Infinity);
  }

  // ---- render --------------------------------------------------------------
  function render() {
    const V = derive();
    renderStatus(V);
    renderMode(V);
    renderPlan(V);
    renderTargets(V);
    renderFailures(V);
    renderParams(V);
    renderHypothesis(V);
    renderSafety(V);
    renderLaunch(V);
    renderActions(V);
  }

  function renderStatus(V) {
    const s = sig([V.session.armed, V.state.stop_requested, (V.job || {}).kind, (V.job || {}).status]);
    if (SIGS.status === s) return; SIGS.status = s;
    clear(statusBox);
    // native append() would print a null as the text "null"
    statusBox.append(...[
      pill(V.session.armed ? "ARMED" : "DISARMED", V.session.armed ? "ok" : "warn",
        V.session.armed ? "shield" : "alert"),
      V.state.stop_requested ? pill("STOP LATCHED", "danger", "stop") : null,
      V.job && V.job.status === "running" ? pill(`${V.job.kind} running`, "chaos", "refresh") : null,
    ].filter(Boolean));
  }

  function renderMode(V) {
    const s = sig([S.mode, S.llm, (V.state.llm || {}).available, (V.state.llm || {}).model]);
    if (SIGS.mode === s) return; SIGS.mode = s;
    clear(modeSec.body);
    const seg = el("div", { class: "fl-launch-seg", role: "radiogroup", "aria-label": "Planning mode" });
    for (const [val, label, icon] of [["auto", "AI planner", "brain"], ["manual", "Manual", "crosshair"]]) {
      const input = el("input", {
        type: "radio", name: "fl-mode", value: val, class: "card__input", checked: S.mode === val,
        onchange: () => { setTouched("mode"); S.mode = val; announce(`${label} mode`); render(); },
      });
      seg.append(el("label", { class: "fl-launch-seg__btn" }, input, svgIcon(icon, 14), el("span", { text: label })));
    }
    const l = V.state.llm || {};
    const llmOk = l.available !== false;
    const llmToggle = switchEl({
      label: llmOk ? "Use LLM" : "LLM offline",
      icon: "brain",
      checked: S.llm && llmOk,
      disabled: !llmOk,
      title: llmOk ? `${l.provider || "llm"} / ${l.model || "?"}` : (l.error || "no model reachable"),
      onchange: (e) => { setTouched("llm"); S.llm = e.target.checked; render(); },
    });
    modeSec.body.append(seg, el("div", { class: "row row--wrap" },
      llmToggle, el("span", { class: "spacer" }),
      el("span", { class: "micro mono dim truncate", text: `${l.provider || "—"} / ${l.model || "—"}` })));
  }

  /** The one toggle app.css has no component for: a real checkbox hidden behind a pill track. */
  function switchEl({ label, icon, checked, disabled, title, onchange, small, extraClass, describedBy }) {
    const input = el("input", {
      type: "checkbox", class: "card__input", checked: !!checked, disabled: !!disabled,
      "aria-describedby": describedBy || null, onchange,
    });
    return el("label", {
      class: cls("fl-launch-switch", small && "fl-launch-switch--sm", disabled && "is-off", extraClass),
      title: title || null,
    }, input,
      el("span", { class: "fl-launch-switch__track", "aria-hidden": "true" },
        el("span", { class: "fl-launch-switch__knob" })),
      icon ? svgIcon(icon, 14) : null,
      el("span", { class: "fl-launch-switch__label", text: label }));
  }

  function renderPlan(V) {
    const s = sig([S.mode, S.planPending, V.plan && V.plan.candidates && V.plan.candidates.length,
      V.pick && V.pick.target_service, V.pick && V.pick.failure_type, V.pick && V.pick.planned_by,
      V.plan && V.plan.llm, S.target, S.failure]);
    if (SIGS.plan === s) return; SIGS.plan = s;
    planSec.root.hidden = S.mode !== "auto";
    clear(planSec.body); clear(planSec.actions);
    if (S.mode !== "auto") return;
    planSec.actions.append(el("button", {
      type: "button", class: "btn btn--ghost btn--sm", disabled: S.planPending, onclick: doPlan,
    }, svgIcon("refresh", 13, { class: "btn__icon" }), S.planPending ? "Planning…" : "Re-plan"));

    const plan = V.plan;
    if (!plan) {
      planSec.body.append(emptyState("brain", "No plan yet",
        "Re-plan ranks every service/failure pair by what it would teach us and picks the next experiment."));
      return;
    }
    const cands = plan.candidates || [];
    if (!cands.length) {
      planSec.body.append(banner("warn", "alert", el("span", {
        text: plan.note || "No candidate passes the current policy. Switch to manual or override protection.",
      })));
    }
    const top = cands[0] || null;
    const maxP = Math.max(1e-6, ...cands.map((c) => Number(c.priority) || 0));
    const listEl = el("ol", { class: "fl-launch-cands" });
    for (const c of cands.slice(0, 6)) {
      const isTop = top && c === top;
      const isPick = V.pick && c.service === V.pick.target_service && c.failure_type === V.pick.failure_type;
      const chosen = !!(isPick || isTop);
      const picked = S.target === c.service && S.failure === c.failure_type;
      listEl.append(el("li", { class: cls("fl-launch-cand", chosen && "is-top") },
        el("span", { class: "fl-launch-cand__rank num", text: `#${c.rank || cands.indexOf(c) + 1}` }),
        el("span", { class: "fl-launch-cand__head" },
          chip(c.service, chosen ? "violet" : "cyan"),
          el("span", { class: "fl-launch-cand__name truncate", text: titleCase(c.failure_type) }),
          isPick ? badge("pick", "violet") : null,
          c.times_tested ? badge(`${c.times_tested}×`) : null),
        el("span", { class: "fl-launch-cand__act" },
          el("button", {
            type: "button", class: cls("btn", "btn--ghost", "btn--sm"), disabled: picked,
            title: `Target ${c.service} with ${titleCase(c.failure_type)}`,
            "aria-label": `Pick ${c.failure_type} on ${c.service}`,
            onclick: () => {
              setTouched("target"); setTouched("failure");
              S.target = c.service; S.failure = c.failure_type; S.launchError = null;
              announce(`Picked ${c.failure_type} on ${c.service}`);
              SIGS.plan = null; render();
            },
          }, picked ? svgIcon("check", 13, { class: "btn__icon" }) : null, picked ? "Set" : "Pick")),
        el("span", { class: "fl-launch-cand__bar" },
          meter((Number(c.priority) || 0) / maxP, chosen ? "violet" : "cyan")),
        el("span", { class: "micro dim fl-launch-cand__why" },
          el("span", { class: "num", text: `${Number(c.priority || 0).toFixed(2)}` }),
          ` · ${(c.reasons || []).join(" · ") || "no reasons given"}`)));
    }
    if (cands.length) planSec.body.append(listEl);

    const pick = V.pick;
    const llm = plan.llm || {};
    const by = (pick && pick.planned_by) || (llm.used ? "llm" : "deterministic");
    planSec.body.append(el("div", { class: "row row--wrap" },
      chip(by === "llm" ? "Chosen by the LLM" : "Chosen by deterministic rules",
        by === "llm" ? "violet" : "neutral", by === "llm" ? "brain" : "grid"),
      el("span", { class: "spacer" }),
      el("span", { class: "micro mono dim truncate", text: `${llm.provider || "—"} / ${llm.model || "—"}` })));
    if (by !== "llm") {
      planSec.body.append(banner("warn", "alert", el("span", {
        text: llm.available === false
          ? `The model was unavailable${llm.error ? ` (${llm.error})` : ""}, so the deterministic ranking decided. Same safety rails, no creativity.`
          : "The LLM was not used for this pick — the deterministic ranking decided.",
      })));
    }
    if (pick && pick.rationale) {
      planSec.body.append(hint(el("b", { class: "dim", text: "Rationale " }), pick.rationale));
    }
    for (const n of (pick && pick.planner_notes) || []) {
      planSec.body.append(hint(el("span", { class: "mute micro", text: "· " }), n));
    }
  }

  function renderTargets(V) {
    const s = sig([S.failure, S.target, S.allowProtected,
      V.services.map((x) => [x.name, x.health, x.criticality, Object.keys(x.blocked || {}).length])]);
    if (SIGS.target === s) return; SIGS.target = s;
    withFocus(targetSec.body, () => {
      clear(targetSec.body);
      if (!V.services.length) {
        targetSec.body.append(emptyState("server", "No chaos-relevant services known",
          "Press Rebuild map to read the compose file and live Docker state."));
        return;
      }
      const grid = el("div", { class: "optgrid", role: "radiogroup", "aria-label": "Target service" });
      for (const svc of V.services) {
        const why = blockReason(svc, V.ft, S.allowProtected, V.protectedMap);
        const locked = !!why;
        const tone = statusTone(svc.health);
        const id = `fl-t-${svc.name}`;
        const input = el("input", {
          type: "radio", name: "fl-target", value: svc.name, class: "card__input", id,
          checked: S.target === svc.name, disabled: locked,
          onchange: () => { setTouched("target"); S.target = svc.name; S.launchError = null; announce(`Target ${svc.name}`); render(); },
        });
        grid.append(el("label", {
          class: cls("card", locked && "card--blocked"),
          for: id, title: why || `${svc.role} · criticality ${svc.criticality}`,
        }, input,
          el("span", { class: "card__icon" }, svgIcon(roleIcon(svc.role), 15)),
          el("span", { class: "card__name", text: svc.name }),
          locked
            ? el("span", { class: "card__sub fl-launch-why-sub", text: why })
            : el("span", { class: "card__sub" }, dot(tone, tone === "chaos"),
              ` ${svc.role}${svc.stateful ? " · stateful" : ""} · ${svc.health}`),
          locked
            ? el("span", { class: "card__lock" }, svgIcon("lock", 13))
            : el("span", {
              class: "card__badge badge", "data-sev": sevOf(Number(svc.criticality)),
              title: `criticality ${Number(svc.criticality).toFixed(1)}`,
            }, Number(svc.criticality).toFixed(1))));
      }
      targetSec.body.append(grid);
      const anyLocked = V.services.some((x) => blockReason(x, V.ft, false, V.protectedMap) && V.ft && V.ft.blockedOn.has(x.name));
      targetSec.body.append(switchEl({
        label: S.allowProtected ? "OVERRIDE ACTIVE — protected services are targetable" : "Override policy protection",
        icon: S.allowProtected ? "alert" : "lock",
        checked: S.allowProtected,
        describedBy: "fl-ov-help",
        extraClass: cls("fl-launch-override", S.allowProtected && "is-hot"),
        onchange: (e) => {
          setTouched("allow_protected"); S.allowProtected = e.target.checked;
          announce(S.allowProtected ? "Protection override ON — protected services are now targetable" : "Protection override off");
          render();
        },
      }));
      targetSec.body.append(el("p", { class: "fieldset__hint", id: "fl-ov-help" },
        anyLocked || Object.keys(V.protectedMap).length
          ? `Off by default. ${Object.keys(V.protectedMap).join(", ") || "Protected services"} may only be degraded, never taken fully offline — turning this on lets a full outage through.`
          : "Off by default. Lets a full outage through on services the policy only allows to be degraded."));
    });
  }

  function renderFailures(V) {
    const s = sig([S.target, S.failure, S.allowProtected, V.failures.map((f) => [f.key, f.available])]);
    if (SIGS.failure === s) return; SIGS.failure = s;
    withFocus(failureSec.body, () => {
      clear(failureSec.body);
      if (!V.failures.length) {
        failureSec.body.append(emptyState("skull", "No failure catalog",
          "The failure catalog has not loaded yet — it ships with the backend."));
        return;
      }
      const grid = el("div", { class: "optgrid optgrid--wide", role: "radiogroup", "aria-label": "Failure type" });
      for (const f of V.failures) {
        const why = blockReason(V.svc, f, S.allowProtected, V.protectedMap);
        const locked = !!why;
        const id = `fl-f-${f.key}`;
        const input = el("input", {
          type: "radio", name: "fl-failure", value: f.key, class: "card__input", id,
          checked: S.failure === f.key, disabled: locked,
          onchange: () => { setTouched("failure"); S.failure = f.key; S.launchError = null; announce(`Failure ${f.title}`); render(); },
        });
        grid.append(el("label", {
          class: cls("card", locked && "card--blocked"), for: id, title: why || f.description,
        }, input,
          el("span", { class: "card__icon" }, svgIcon(f.degradeOnly ? "trend" : "skull", 15)),
          el("span", { class: "card__name", text: f.title }),
          el("span", { class: "card__sub", text: plainDesc(f) }),
          el("span", { class: "card__sub fl-launch-cardmeta" },
            badge(f.degradeOnly ? "degrade-only" : "outage", f.degradeOnly ? "warn" : "danger"),
            f.category ? badge(f.category) : null,
            badge(`recovers ≈ ${fmtSecs(f.recoveryS)}`)),
          locked ? el("span", { class: "card__sub fl-launch-why-sub", text: why }) : null,
          locked ? el("span", { class: "card__lock" }, svgIcon("lock", 13)) : null));
      }
      failureSec.body.append(grid);
    });
  }

  function renderParams(V) {
    const schemaSig = sig([V.ft && V.ft.key, (V.ft || {}).params]);
    if (SIGS.paramSchema !== schemaSig) {
      SIGS.paramSchema = schemaSig;
      clear(paramSec.body);
      if (!V.ft) {
        paramSec.body.append(emptyState("gauge", "No dials yet", "Pick a failure type to see its blast-radius dials."));
        SIGS.paramImpl = null;
        return;
      }
      const params = el("div", { class: "params" });
      for (const p of V.ft.params) buildParam(params, p, V);
      paramSec.body.append(params,
        el("div", { class: "banner fl-launch-impl", id: "fl-impl", "data-tone": "chaos" }));
      SIGS.paramImpl = null;
    }
    if (!V.ft) return;
    // in-place value sync (never touches a control the operator is holding)
    for (const p of V.ft.params) {
      const row = paramSec.body.querySelector(`[data-param="${p.name}"]`);
      if (!row) continue;
      const v = S.params[p.name];
      if (p.type === "bool") {
        const cb = row.querySelector("input[type=checkbox]");
        if (cb && cb !== document.activeElement && cb.checked !== !!v) cb.checked = !!v;
        const flag = row.querySelector(".fl-launch-val");
        if (flag) flag.textContent = v ? "on" : "off";
      } else {
        for (const inp of row.querySelectorAll("input")) {
          if (inp === document.activeElement) continue;
          if (String(inp.value) !== String(v)) inp.value = String(v);
        }
        setReadout(row, p, v);
      }
    }
    const implSig = sig([V.ft.key, S.params, S.target, V.th.max_experiment_seconds, V.th.watchdog_grace_seconds]);
    if (SIGS.paramImpl !== implSig) {
      SIGS.paramImpl = implSig;
      paintImplications(V);
    }
  }

  /** Writes the numeric readout and the WebKit slider fill in one place. */
  function setReadout(row, p, v) {
    const val = row.querySelector(".fl-launch-val");
    if (val && val.textContent !== String(v)) val.textContent = String(v);
    const range = row.querySelector("input[type=range]");
    if (range) {
      const lo = Number(p.lo ?? 0), hi = Number(p.hi ?? 100);
      const pct = hi > lo ? ((Number(v) - lo) / (hi - lo)) * 100 : 0;
      range.style.setProperty("--fill", `${clamp(pct, 0, 100).toFixed(1)}%`);
    }
  }

  function buildParam(host, p, V) {
    const row = el("div", { class: cls("param", p.type === "bool" && "param--bool"), dataset: { param: p.name } });
    const id = `fl-p-${p.name}`;
    const helpId = `${id}-help`;
    const niceName = titleCase(p.name.replace(/_s$/, " (seconds)").replace(/_ms$/, " (ms)").replace(/_mb$/, " (MB)"));
    const label = el("label", { class: "param__label", for: id, text: niceName });
    const help = el("p", { class: "param__desc", id: helpId, text: p.desc || (p.type === "bool" ? "" : `allowed ${p.lo ?? "—"}–${p.hi ?? "—"}${p.unit ? ` ${p.unit}` : ""}`) });
    const err = el("p", { class: "param__err", role: "alert", hidden: true });

    if (p.type === "bool") {
      const toggle = switchEl({
        label: "", small: true, describedBy: helpId, checked: !!S.params[p.name],
        onchange: (e) => {
          const k = `param:${V.ft.key}:${p.name}`;
          setTouched(k); S.dirty[k] = S.params[p.name] = e.target.checked; render();
        },
      });
      toggle.querySelector("input").id = id;
      toggle.querySelector(".fl-launch-switch__label").append(
        el("span", { class: "fl-launch-val", text: S.params[p.name] ? "on" : "off" }));
      row.append(label, el("span", { class: "param__out" }, toggle), help, err);
      host.append(row);
      return;
    }

    const v = S.params[p.name];
    const step = p.unit === "ms" ? 50 : (p.hi ?? 100) - (p.lo ?? 0) > 50 ? 5 : 1;
    const commit = (raw, from) => {
      const parsed = Number(raw);
      setTouched(`param:${V.ft.key}:${p.name}`);
      if (!Number.isFinite(parsed)) {
        err.hidden = false; err.textContent = `${p.name} must be a number`;
        return;
      }
      const cl = clampParam(p, parsed);
      if (cl !== parsed) {
        err.hidden = false;
        err.textContent = `Clamped to the policy range ${p.lo}–${p.hi}${p.unit ? ` ${p.unit}` : ""} (you asked for ${parsed}).`;
      } else { err.hidden = true; err.textContent = ""; }
      S.dirty[`param:${V.ft.key}:${p.name}`] = S.params[p.name] = cl;
      setReadout(row, p, cl);
      for (const inp of row.querySelectorAll("input")) {
        if (inp !== document.activeElement && String(inp.value) !== String(cl)) inp.value = String(cl);
      }
      if (from === "num") render(); else refreshImplications();
    };
    const range = el("input", {
      type: "range", id, class: "param__range", min: p.lo ?? 0, max: p.hi ?? 100, step, value: v,
      "aria-describedby": helpId,
      oninput: (e) => commit(e.target.value, "range"),
      onchange: () => render(),
    });
    const num = el("input", {
      type: "number", class: "fl-launch-num mono", min: p.lo ?? 0, max: p.hi ?? 100, step, value: v,
      "aria-label": `${p.name} exact value${p.unit ? ` in ${p.unit}` : ""}`, "aria-describedby": helpId,
      onchange: (e) => commit(e.target.value, "num"),
    });
    row.append(label,
      el("span", { class: "param__out fl-launch-readout" },
        el("span", { class: "fl-launch-val", text: String(v) }),
        p.unit ? el("span", { class: "param__unit", text: ` ${p.unit}` }) : null),
      el("div", { class: "fl-launch-prange" }, range, num),
      help, err);
    host.append(row);
    setReadout(row, p, v);
  }

  function paintImplications(V) {
    const node = paramSec.body.querySelector("#fl-impl");
    if (node) { clear(node); node.append(svgIcon("alert", 15), el("span", { text: implications(V) })); }
  }

  function refreshImplications() {
    const V = derive();
    paintImplications(V);
    const h = hypSec.body.querySelector(".hypothesis__text");
    if (h) h.textContent = hypothesisText(V);
  }

  function implications(V) {
    const ft = V.ft; if (!ft) return "";
    const d = Number(S.params.duration_s ?? 0) || 0;
    const maxS = Number(V.th.max_experiment_seconds ?? 90);
    const grace = Number(V.th.watchdog_grace_seconds ?? 15);
    const held = Math.min(d, maxS);
    const bits = [];
    bits.push(ft.degradeOnly
      ? `Worst case the API is degraded (not gone) for up to ${fmtSecs(held)}`
      : `Worst case the API is unavailable for up to ${fmtSecs(held)}`);
    bits.push(`rollback runs at ${fmtSecs(held)}`);
    bits.push(`the watchdog force-restores at ${fmtSecs(held + grace)}`);
    bits.push(`hard cap ${fmtSecs(maxS)}`);
    const extra = { network_delay: () => `every call out of ${S.target} gains ${S.params.delay_ms} ms ± ${S.params.jitter_ms} ms`,
      network_loss: () => `${S.params.loss_percent}% of outbound packets are dropped`,
      cpu_stress: () => `${S.params.workers} busy-loop worker(s) compete with the service for CPU`,
      disk_pressure: () => `${S.params.size_mb} MB is allocated inside the container's writable layer`,
      db_flood: () => `${S.params.writers} parallel mongosh insert loop(s) compete with Ditto's own writes`,
      traffic_spike: () => `${S.params.rps} req/s on top of the probe traffic`,
      container_crash: () => (S.params.block_restart
        ? "Docker auto-restart is disabled, so it stays down for the whole window"
        : "Docker restarts it immediately, so this measures detection + cold start + cluster rejoin"),
    }[ft.key];
    const tail = extra ? extra() : null;
    return `${bits.join("; ")}.${tail ? ` ${tail[0].toUpperCase()}${tail.slice(1)}.` : ""}`;
  }

  function hypothesisText(V) {
    if (S.mode === "auto" && V.pick && V.pick.hypothesis && V.pick.hypothesis.statement) return V.pick.hypothesis.statement;
    const { svc, ft } = V;
    if (!svc || !ft) return "Pick a target and a failure type to see the prediction this experiment will be judged against.";
    const d = Number(S.params.duration_s ?? ft.recoveryS);
    const verb = ft.degradeOnly ? `degrading \`${svc.name}\` with ${ft.title.toLowerCase()}` : `taking away \`${svc.name}\``;
    const affected = (svc.dependents && svc.dependents.length ? svc.dependents : svc.transitive).slice(0, 4);
    return `${verb[0].toUpperCase()}${verb.slice(1)} for ${fmtSecs(d)} will push the error rate to at most `
      + `${fmtPct(clamp(ft.severity, 0, 1))}, affect ${list(affected)}, and recover within ${fmtSecs(ft.recoveryS)} without intervention.`;
  }

  function renderHypothesis(V) {
    const s = sig([S.mode, S.target, S.failure, S.params, V.pick && V.pick.hypothesis]);
    if (SIGS.hyp === s) return; SIGS.hyp = s;
    clear(hypSec.body);
    const h = V.pick && V.pick.hypothesis;
    const fromPlanner = S.mode === "auto" && V.pick;
    const meta = el("div", { class: "hypothesis__meta" });
    let note = null;
    if (fromPlanner && h) {
      meta.append(
        badge(`≤ ${fmtPct(clamp(Number(h.expected_max_error_rate ?? 0), 0, 1))} errors`, "warn"),
        badge(`recovery ≤ ${fmtDur(Number(h.expected_recovery_s ?? 0))}`, "ok"));
      note = `Expected to drag down ${list(h.expected_affected)}.`;
    } else {
      meta.append(badge("falsifiable on purpose", "violet"));
      note = "The report says HELD or VIOLATED when the run finishes — a violated hypothesis is the interesting result.";
    }
    hypSec.body.append(el("div", { class: "hypothesis" },
      el("div", { class: "hypothesis__label" }, svgIcon("flask", 13),
        el("span", { text: fromPlanner ? "Planner prediction" : "Composed locally" })),
      el("p", { class: "hypothesis__text", text: hypothesisText(V) }),
      meta,
      el("p", { class: "micro dim fl-launch-hypnote", text: note })));
  }

  function renderSafety(V) {
    const s = sig([V.th, !!V.state.baseline, S.jobPending, (V.job || {}).kind]);
    if (SIGS.safety === s) return; SIGS.safety = s;
    clear(safetySec.body); clear(safetySec.actions);
    safetySec.actions.append(badge(V.state.baseline ? "calibrated" : "uncalibrated",
      V.state.baseline ? "ok" : "warn"));
    const th = V.th;
    const er = Number(th.abort_error_rate_above ?? th.probe_error_rate_above ?? 0.3);
    const n = Number(th.trailing_samples ?? 10);
    const p95 = Number(th.abort_p95_ms_above ?? th.p95_latency_ms_floor ?? 3000);
    const factor = th.p95_latency_factor_of_baseline;
    const basep95 = th.baseline_p95_ms;
    const hf = Number(th.non_target_health_failures_in_a_row ?? 3);
    const maxS = Number(th.max_experiment_seconds ?? 90);
    const grace = Number(th.watchdog_grace_seconds ?? 15);
    const recRate = th.recovery_success_rate_at_least ?? th.success_rate_at_least;
    const recChecks = th.recovery_consecutive_checks ?? th.consecutive_checks;
    const lines = [
      `Aborts if the error rate exceeds ${fmtPct(er)} over the last ${n} synthetic requests.`,
      `Aborts if p95 latency passes ${Math.round(p95)} ms${factor && basep95 ? ` (or ${factor}× the ${Math.round(basep95)} ms baseline, whichever is higher)` : ""}.`,
      `Aborts if a service outside the blast radius fails ${hf} health checks in a row.`,
      `Hard cap ${fmtSecs(maxS)} per experiment; an independent watchdog force-restores at ${fmtSecs(maxS + grace)} even if the main loop hangs.`,
    ];
    if (recRate && recChecks) lines.push(`Counts as recovered after ${recChecks} consecutive checks at or above ${fmtPct(Number(recRate))} success.`);
    const ul = el("ul", { class: "fl-launch-rails" });
    for (const t of lines) ul.append(el("li", null, svgIcon("shield", 13), el("span", { text: t })));
    safetySec.body.append(ul);
    if (!V.state.baseline) {
      const pending = S.jobPending === "baseline" || (V.job && V.job.kind === "baseline" && V.job.status === "running");
      safetySec.body.append(banner("warn", "alert",
        el("span", { text: "No baseline captured. The thresholds above are uncalibrated floors, not this system's normal — a slow day could read as an incident." }),
        el("span", { class: "spacer" }),
        el("button", { type: "button", class: "btn btn--sm", disabled: pending || V.busy, onclick: () => runJob("baseline") },
          svgIcon("gauge", 13, { class: "btn__icon" }), pending ? "Capturing…" : "Capture baseline (60 s)")));
    } else {
      const api = V.state.baseline.api || {};
      safetySec.body.append(el("div", { class: "fl-launch-stats" },
        stat("Baseline success", fmtPct(Number(api.success_rate ?? 0)), "ok"),
        stat("Baseline p95", `${Math.round(Number(api.p95_ms ?? 0))} ms`, "cyan"),
        stat("Samples", String(api.sample_count ?? 0), "neutral")));
    }
  }

  function renderLaunch(V) {
    const run = V.run;
    const s = sig([V.running, run.phase, Math.round(run.elapsed_s || 0), Math.round(run.deadline_in_s || 0),
      run.success_rate, V.reasons, S.launchPending, S.launchError, S.confirmArmed, S.mode, S.target, S.failure]);
    if (SIGS.launch === s) return; SIGS.launch = s;
    clear(launchBox);
    launchTitle.textContent = V.running ? "Live experiment" : "Launch";

    if (V.running) {
      const ribbon = el("div", { class: "row row--wrap fl-launch-phases" });
      let seen = false;
      for (const p of PHASES) {
        const on = p === run.phase;
        if (on) seen = true;
        ribbon.append(on
          ? el("span", { class: "chip", "data-tone": "chaos" },
            el("span", { class: "chip__glyph" }, dot("chaos", true)),
            el("span", { class: "chip__label", text: p.toUpperCase() }))
          : badge(p.toUpperCase(), !seen ? "ok" : null));
      }
      const abort = (run.safety || {}).abort_reason;
      // native append() prints a null arg as the text "null" (el()'s own append skips it,
      // but this is a bare Element.append call), so the no-abort case must be filtered out
      launchBox.append(...[
        ribbon,
        el("div", { class: "fl-launch-stats" },
          stat("Target", run.target_service || "—", "chaos", titleCase(run.failure_type || "")),
          stat("Elapsed", fmtDur(Number(run.elapsed_s || 0)), "cyan"),
          stat("Hard cap in", fmtDur(Number(run.deadline_in_s ?? 0)), Number(run.deadline_in_s ?? 99) < 5 ? "danger" : "chaos"),
          stat("Success rate", fmtPct(Number(run.success_rate ?? 0)), Number(run.success_rate ?? 1) < 0.9 ? "danger" : "ok"),
          stat("Samples", String(run.sample_count ?? 0), "neutral")),
        abort ? banner("danger", "alert", el("span", { text: `Abort fired: ${abort}` })) : null,
        el("button", {
          type: "button", class: "btn btn--launch btn--abort",
          onclick: () => { announce("Abort requested"); safe(onStop && onStop(), "abort"); },
        }, svgIcon("stop", 18), el("span", { text: "ABORT" })),
      ].filter(Boolean));
      if (run.phase !== lastPhase) { lastPhase = run.phase; announce(`Phase ${run.phase}`); }
      return;
    }
    lastPhase = "idle";

    const blocked = V.reasons.length > 0 || S.launchPending;
    const label = `${S.mode === "auto" ? "AI" : "Manual"}: ${titleCase(S.failure || "no failure")} on ${S.target || "no target"}`;
    const btn = el("button", {
      type: "button", class: cls("btn", "btn--launch", S.confirmArmed && "is-armed"),
      disabled: blocked, "aria-describedby": "fl-launch-why",
    }, el("span", { class: "fl-launch-ring", "aria-hidden": "true" }), svgIcon("zap", 18),
      el("span", { class: "fl-launch-btn-text", text: S.launchPending ? "LAUNCHING…" : (S.confirmArmed ? "CONFIRM LAUNCH" : (reduceMotion() ? "LAUNCH" : "HOLD TO LAUNCH")) }));
    btn.style.setProperty("--fl-hold", "0%");
    wireHold(btn, V);
    launchBox.append(btn, el("p", { class: "micro mono dim fl-launch-btn-sub truncate", text: label }));

    if (V.reasons.length) {
      const why = banner("warn", "alert", el("span", { text: `Cannot launch: ${V.reasons[0]}` }));
      why.id = "fl-launch-why";
      if (V.reasons.length > 1) {
        why.append(el("span", { class: "micro dim fl-launch-more", text: `+${V.reasons.length - 1} more: ${V.reasons.slice(1).join("; ")}` }));
      }
      const extras = el("span", { class: "btn-row fl-launch-whyacts" });
      if (!V.session.armed) {
        extras.append(el("button", { type: "button", class: "btn btn--sm", disabled: V.busy, onclick: () => runJob("arm") },
          svgIcon("shield", 13, { class: "btn__icon" }), "Arm probes"));
      }
      if (!V.services.length) {
        extras.append(el("button", { type: "button", class: "btn btn--sm", disabled: V.busy, onclick: () => runJob("topology") },
          svgIcon("grid", 13, { class: "btn__icon" }), "Rebuild map"));
      }
      if (extras.childNodes.length) why.append(extras);
      launchBox.append(why);
    } else {
      launchBox.append(el("p", { class: "fieldset__hint row fl-launch-ok", id: "fl-launch-why" },
        svgIcon("check", 13), el("span", {
          text: reduceMotion() ? "Press LAUNCH, then confirm." : "Hold for 1.2 s — or focus it and hold Enter.",
        })));
    }
    if (S.launchError) launchBox.append(errorBox(S.launchError));
  }

  function wireHold(btn, V) {
    const text = () => btn.querySelector(".fl-launch-btn-text");
    const setPct = (p) => { S.holdPct = p; btn.style.setProperty("--fl-hold", `${p}%`); };
    const cancel = () => { if (holdRaf) cancelAnimationFrame(holdRaf); holdRaf = 0; setPct(0); };
    const fire = () => { cancel(); doLaunch(V); };
    const start = () => {
      if (btn.disabled || S.launchPending) return;
      if (reduceMotion()) {
        if (!S.confirmArmed) {
          S.confirmArmed = true;
          announce("Launch armed — press again within 5 seconds to confirm");
          if (text()) text().textContent = "CONFIRM LAUNCH";
          btn.classList.add("is-armed");
          clearTimeout(confirmTimer);
          confirmTimer = window.setTimeout(() => {
            S.confirmArmed = false; btn.classList.remove("is-armed");
            if (text()) text().textContent = "LAUNCH";
          }, 5000);
          return;
        }
        clearTimeout(confirmTimer); S.confirmArmed = false; fire(); return;
      }
      holdStart = performance.now();
      const step = () => {
        const p = Math.min(100, ((performance.now() - holdStart) / HOLD_MS) * 100);
        setPct(p);
        if (p >= 100) fire(); else holdRaf = requestAnimationFrame(step);
      };
      holdRaf = requestAnimationFrame(step);
    };
    btn.addEventListener("pointerdown", (e) => { e.preventDefault(); btn.focus(); start(); });
    btn.addEventListener("pointerup", cancel);
    btn.addEventListener("pointerleave", cancel);
    btn.addEventListener("pointercancel", cancel);
    btn.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" && e.key !== " ") return;
      e.preventDefault();
      if (e.repeat) return;
      start();
    });
    btn.addEventListener("keyup", (e) => { if (e.key === "Enter" || e.key === " ") cancel(); });
    btn.addEventListener("blur", cancel);
  }

  function renderActions(V) {
    const s = sig([V.busy, (V.job || {}).kind, (V.job || {}).status, S.jobPending, S.confirmAction,
      S.healRestart, S.actionError, V.session.armed, V.session.error, (V.job || {}).error]);
    if (SIGS.actions === s) return; SIGS.actions = s;
    clear(actionSec.body);
    const row = el("div", { class: "fl-launch-actions btn-row" });
    let confirmRow = null;
    for (const spec of ACTION_SPECS) {
      if (spec.kind === "arm" && V.session.armed) continue;
      if (spec.kind === "disarm" && !V.session.armed) continue;
      const active = (V.job && V.job.kind === spec.kind && V.job.status === "running") || S.jobPending === spec.kind;
      const disabled = active || (spec.kind === "cancel" ? !V.busy : V.busy);
      const reason = active ? "running…" : (spec.kind === "cancel" && !V.busy ? "nothing to cancel" : (V.busy ? "another job is running" : ""));
      if (S.confirmAction === spec.kind) {
        confirmRow = el("div", { class: "fl-launch-confirm banner", "data-tone": "danger" },
          svgIcon("alert", 15),
          el("span", { text: S.healRestart ? "Restart every container in the sandbox?" : "Remove chaos leftovers now?" }),
          switchEl({
            label: "restart containers", small: true, checked: S.healRestart,
            onchange: (e) => { S.healRestart = e.target.checked; SIGS.actions = null; render(); },
          }),
          el("span", { class: "btn-row fl-launch-confirmacts" },
            el("button", { type: "button", class: "btn btn--sm btn--danger", onclick: () => { S.confirmAction = null; runJob("heal"); } }, "Yes, heal"),
            el("button", { type: "button", class: "btn btn--sm btn--ghost", onclick: () => { S.confirmAction = null; SIGS.actions = null; render(); } }, "Cancel")));
        continue;
      }
      row.append(el("button", {
        type: "button", class: cls("btn", "btn--sm", spec.kind === "cancel" ? "btn--danger" : "btn--ghost"),
        disabled, title: reason || spec.label,
        "aria-label": reason ? `${spec.label} — ${reason}` : spec.label,
        onclick: () => { if (spec.confirm) { S.confirmAction = spec.kind; SIGS.actions = null; render(); } else runJob(spec.kind); },
      }, svgIcon(active ? "refresh" : spec.icon, 13, { class: "btn__icon" }), active ? `${spec.label}…` : spec.label));
    }
    actionSec.body.append(row);
    if (confirmRow) actionSec.body.append(confirmRow);
    if (S.actionError) actionSec.body.append(errorBox(S.actionError));
    if (V.job && V.job.error) actionSec.body.append(errorBox(`${V.job.kind}: ${V.job.error}`));
    if (V.session.error) actionSec.body.append(banner("warn", "alert", el("span", { text: String(V.session.error) })));
  }

  // ---- actions -------------------------------------------------------------
  function payload(V) {
    return {
      mode: S.mode === "auto" ? "auto" : "manual",
      target: S.target,
      failure: S.failure,
      params: { ...S.params },
      experiments: 1,
      llm: !!S.llm,
      allow_protected: !!S.allowProtected,
      analyze: true,
    };
  }

  function doLaunch(V) {
    const now = derive();
    if (now.reasons.length) { S.launchError = `Blocked: ${now.reasons[0]}`; SIGS.launch = null; render(); return; }
    const body = payload(now);
    S.launchPending = true; S.launchError = null; SIGS.launch = null; render();
    announce(`Launching ${body.failure} on ${body.target}`);
    Promise.resolve(onLaunch && onLaunch(body)).then(() => {
      S.launchPending = false; SIGS.launch = null; render();
    }).catch((err) => {
      S.launchPending = false;
      S.launchError = (err && (err.message || err.error)) || "launch failed";
      announce(`Launch failed: ${S.launchError}`);
      SIGS.launch = null; render();
    });
  }

  function doPlan() {
    S.planPending = true; SIGS.plan = null; render();
    Promise.resolve(onPlan && onPlan({ llm: !!S.llm })).then((p) => {
      S.planPending = false;
      if (p) D.plan = p;
      // a fresh plan may re-seed untouched controls
      SIGS.plan = null; SIGS.target = null; SIGS.failure = null; SIGS.hyp = null; SIGS.launch = null;
      announce("New plan ready");
      render();
    }).catch((err) => {
      S.planPending = false;
      S.actionError = `Re-plan failed: ${(err && err.message) || err}`;
      SIGS.plan = null; SIGS.actions = null; render();
    });
  }

  function runJob(kind) {
    const spec = ACTION_SPECS.find((a) => a.kind === kind) || { body: () => ({}) };
    S.jobPending = kind; S.actionError = null; SIGS.actions = null; SIGS.safety = null; SIGS.launch = null; render();
    announce(`${titleCase(kind)} requested`);
    Promise.resolve(onJob && onJob(kind, spec.body(S))).then(() => {
      S.jobPending = null; SIGS.actions = null; SIGS.safety = null; SIGS.launch = null; render();
    }).catch((err) => {
      S.jobPending = null;
      S.actionError = `${titleCase(kind)} failed: ${(err && (err.message || err.error)) || err}`;
      SIGS.actions = null; render();
    });
  }

  function safe(p, what) {
    Promise.resolve(p).catch((err) => {
      S.actionError = `${what} failed: ${(err && err.message) || err}`;
      SIGS.actions = null; render();
    });
  }

  /** Rebuild `host`'s children without losing keyboard focus inside it. */
  function withFocus(host, fn) {
    const a = document.activeElement;
    const keep = a && host.contains(a) ? { name: a.name, value: a.value, type: a.type } : null;
    fn();
    if (!keep) return;
    const sel = keep.name
      ? `input[name="${keep.name}"][value="${keep.value}"]`
      : `input[type="${keep.type}"]`;
    const next = host.querySelector(sel);
    if (next && !next.disabled) next.focus();
  }

  const onExplainAll = (e) => {
    const open = !!(e.detail && e.detail.open);
    for (const key of Object.keys(SECTIONS)) setExplain(key, open);
  };
  document.addEventListener("fl-explain", onExplainAll);

  render();

  return {
    update(state, catalog, plan) {
      D = {
        state: state || {},
        catalog: catalog !== undefined && catalog !== null ? catalog : D.catalog,
        plan: plan !== undefined && plan !== null ? plan : D.plan,
      };
      render();
    },
    destroy() {
      document.removeEventListener("fl-explain", onExplainAll);
      if (holdRaf) cancelAnimationFrame(holdRaf);
      clearTimeout(confirmTimer);
      clear(hostEl);
      hostEl.classList.remove("fl-launch-root", "panel");
    },
  };
}

/** One plain-English line per failure type; falls back to the catalog prose. */
function plainDesc(f) {
  const own = {
    container_crash: "Kills the process with no warning — measures crash detection, cold start and cluster rejoin.",
    container_stop: "Shuts the service down cleanly and keeps it down: a sustained dependency outage.",
    container_pause: "Freezes every process. Connections stay open, so callers hang instead of failing fast.",
    network_delay: "Adds delay and jitter to everything the service sends — the classic slow network.",
    network_loss: "Drops a share of outbound packets, so calls retry and time out.",
    network_partition: "Cuts the service off the network. It is alive but unreachable: a split brain.",
    cpu_stress: "Starves the service of CPU with busy-loop workers competing for the core.",
    disk_pressure: "Fills the container's writable layer so writes and logs start failing.",
    db_flood: "Hammers MongoDB with parallel insert loops competing with Ditto's real traffic.",
    traffic_spike: "Blasts the API with a burst of concurrent requests on top of the probe load.",
  }[f.key];
  if (own) return own;
  const d = String(f.description || "").replace(/`/g, "");
  const first = d.split(/(?<=\.)\s/)[0] || d;
  return first.length > 170 ? `${first.slice(0, 167)}…` : first;
}

// ---------------------------------------------------------------------------
// Scoped styling — injected unconditionally, deliberately small. Everything with a matching app.css
// class uses that class instead; what is left here is only what the design system does not define:
// the hold-to-launch progress, the mode segmented control, the toggle switch, the range+number pair,
// the candidate rows, the safety-rail list, and a few layout fixes for long prose inside .banner.
// No literal colour anywhere — every value is a tokens.css custom property.
const SCOPED_CSS = `
.fl-launch-root [hidden]{display:none}
.fl-launch-root{container-type:inline-size}
@media (min-width:768px){.fl-launch-root{height:100%}}

/* section scaffolding: legend + disclosure + body, with no gap wasted while closed */
.fl-launch-root .fl-launch-sec{gap:0}
.fl-launch-root .fl-launch-secbody{display:flex;flex-direction:column;gap:var(--s2);min-width:0;margin-top:var(--s2)}
.fl-launch-root .fl-launch-sec > .explain__body{margin-top:var(--s2);border-radius:var(--r1)}
.fl-launch-root .fl-launch-sec > .explain__body:not([data-open="true"]){margin-top:0}
.fl-launch-root .fieldset__legend{flex-wrap:wrap;row-gap:var(--s1)}
.fl-launch-root .fieldset__legend .explain__btn{width:34px;height:34px}
.fl-launch-root .fl-launch-subacts{display:flex;align-items:center;gap:var(--s1);flex:none}
.fl-launch-root .fl-launch-status{row-gap:var(--s1)}

/* app.css ships .btn--sm at 30px; the touch-target audit wants 32px or more */
.fl-launch-root .btn--sm{min-height:32px}
/* .dot assumes a flex parent (flex items are blockified); here it sits inside running text */
.fl-launch-root .dot{display:inline-block;vertical-align:middle}
/* long prose inside a .banner should start at the top and be allowed to wrap */
.fl-launch-root .banner{align-items:flex-start;flex-wrap:wrap;row-gap:var(--s2);font-size:12px;line-height:1.5}
.fl-launch-root .banner > .icon{margin-top:2px}
.fl-launch-root .launcher__error{display:flex;align-items:flex-start;gap:var(--s2);line-height:1.5}
.fl-launch-root .empty{min-height:0;padding:var(--s4) var(--s3)}

/* mode: a two-up segmented control (app.css has no segmented control) */
.fl-launch-root .fl-launch-seg{display:grid;grid-template-columns:1fr 1fr;gap:var(--s0);padding:var(--s0);background:var(--surface-2);border:1px solid var(--border);border-radius:var(--r-pill)}
.fl-launch-root .fl-launch-seg__btn{position:relative;display:flex;align-items:center;justify-content:center;gap:var(--s2);min-height:36px;padding:0 var(--s2);border-radius:var(--r-pill);font-size:12px;font-weight:600;letter-spacing:.02em;color:var(--text-dim);white-space:nowrap;cursor:pointer;transition:background-color var(--t-fast) var(--ease),color var(--t-fast) var(--ease)}
.fl-launch-root .fl-launch-seg__btn .icon{width:14px;height:14px;color:var(--text-mute)}
.fl-launch-root .fl-launch-seg__btn:hover{color:var(--text)}
.fl-launch-root .fl-launch-seg__btn:has(:checked){background:var(--blue-dim);color:var(--text);box-shadow:var(--shadow-inset)}
.fl-launch-root .fl-launch-seg__btn:has(:checked) .icon{color:var(--blue)}
.fl-launch-root .fl-launch-seg__btn:has(:focus-visible){outline:var(--ring) solid var(--cyan);outline-offset:2px}

/* toggle switch: app.css styles a checkbox, not a switch */
.fl-launch-root .fl-launch-switch{position:relative;display:inline-flex;align-items:center;gap:var(--s2);min-height:var(--hit);padding:0 var(--s1);font-size:12px;font-weight:600;line-height:1.4;color:var(--text-dim);cursor:pointer}
.fl-launch-root .fl-launch-switch--sm{min-height:28px;font-size:11px}
.fl-launch-root .fl-launch-switch .icon{width:14px;height:14px;color:var(--text-mute)}
.fl-launch-root .fl-launch-switch__label{display:inline-flex;align-items:center;gap:var(--s1);min-width:0}
.fl-launch-root .fl-launch-switch__track{position:relative;flex:none;width:32px;height:18px;border-radius:var(--r-pill);background:var(--surface-3);border:1px solid var(--border-strong);transition:background-color var(--t-fast) var(--ease),border-color var(--t-fast) var(--ease)}
.fl-launch-root .fl-launch-switch__knob{position:absolute;top:2px;left:2px;width:12px;height:12px;border-radius:50%;background:var(--text-mute);transition:transform var(--t-fast) var(--ease),background-color var(--t-fast) var(--ease)}
.fl-launch-root .fl-launch-switch:hover{color:var(--text)}
.fl-launch-root .fl-launch-switch:has(:checked){color:var(--text)}
.fl-launch-root .fl-launch-switch:has(:checked) .fl-launch-switch__track{background:var(--tone-ok-bg);border-color:var(--tone-ok-border)}
.fl-launch-root .fl-launch-switch:has(:checked) .fl-launch-switch__knob{transform:translateX(14px);background:var(--ok)}
.fl-launch-root .fl-launch-switch:has(:checked) .icon{color:var(--ok)}
.fl-launch-root .fl-launch-switch:has(:focus-visible){outline:var(--ring) solid var(--cyan);outline-offset:2px;border-radius:var(--r1)}
.fl-launch-root .fl-launch-switch.is-off{color:var(--text-mute);cursor:not-allowed}
.fl-launch-root .fl-launch-switch .fl-launch-val{font-family:var(--font-mono);color:var(--text-dim)}

/* the protection override reads as a latch, not a preference */
.fl-launch-root .fl-launch-override{display:flex;width:100%;gap:var(--s2);padding:var(--s2) var(--s3);border:1px dashed var(--border-strong);border-radius:var(--r1);white-space:normal}
.fl-launch-root .fl-launch-override.is-hot{border-style:solid;border-color:var(--tone-danger-border);background:var(--tone-danger-bg);color:var(--tone-danger-ink)}
.fl-launch-root .fl-launch-override.is-hot .icon{color:var(--danger)}
.fl-launch-root .fl-launch-override.is-hot:has(:checked) .fl-launch-switch__track{background:var(--tone-danger-bg);border-color:var(--tone-danger-border)}
.fl-launch-root .fl-launch-override.is-hot:has(:checked) .fl-launch-switch__knob{background:var(--danger)}

/* option cards: the blocked reason is the only text app.css colours as body copy */
.fl-launch-root .card{min-height:52px}
.fl-launch-root .card__sub{white-space:normal}
.fl-launch-root .fl-launch-why-sub{color:var(--tone-warn-ink)}
.fl-launch-root .fl-launch-cardmeta{display:flex;flex-wrap:wrap;gap:var(--s1);margin-top:var(--s0)}
.fl-launch-root .card--blocked .card__name{padding-right:var(--s5)}

/* params: app.css has no slot for the exact-value input beside the slider */
.fl-launch-root .fl-launch-prange{grid-area:range;display:flex;align-items:center;gap:var(--s2);min-width:0}
.fl-launch-root .fl-launch-prange .param__range{flex:1 1 auto;min-width:0}
.fl-launch-root input.fl-launch-num{flex:none;width:70px;min-width:70px;min-height:32px;padding:0 var(--s2);font-size:12px;text-align:right}
.fl-launch-root .param__out{min-height:24px}
.fl-launch-root .param__err{grid-column:1/-1;font-size:11px;line-height:1.45;color:var(--tone-warn-ink)}
.fl-launch-root .fl-launch-impl{align-items:flex-start}

/* planner candidates: one row each — rank, service, failure, priority meter, reasons */
.fl-launch-root .fl-launch-cands{display:flex;flex-direction:column;gap:var(--s1);margin:0;padding:0;list-style:none}
.fl-launch-root .fl-launch-cand{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:var(--s1) var(--s2);align-items:center;padding:var(--s2);border:1px solid var(--border);border-radius:var(--r1);background:var(--surface-2);transition:border-color var(--t-fast) var(--ease),background-color var(--t-fast) var(--ease)}
.fl-launch-root .fl-launch-cand:hover{border-color:var(--border-strong)}
.fl-launch-root .fl-launch-cand.is-top{border-color:var(--tone-violet-border);background:var(--tone-violet-bg)}
.fl-launch-root .fl-launch-cand__rank{grid-column:1;grid-row:1;font-size:12px;font-weight:700;color:var(--text-mute)}
.fl-launch-root .fl-launch-cand.is-top .fl-launch-cand__rank{color:var(--violet)}
.fl-launch-root .fl-launch-cand__head{grid-column:2;grid-row:1;display:flex;align-items:center;gap:var(--s1);min-width:0}
.fl-launch-root .fl-launch-cand__name{font-size:12px;font-weight:600;color:var(--text)}
.fl-launch-root .fl-launch-cand__act{grid-column:3;grid-row:1;display:flex;flex:none}
.fl-launch-root .fl-launch-cand__bar{grid-column:1/-1;grid-row:2;display:block}
.fl-launch-root .fl-launch-cand__why{grid-column:1/-1;grid-row:3;text-transform:none;letter-spacing:0;font-weight:400;line-height:1.45;overflow-wrap:anywhere}

/* a badge is nowrap by design, so long meta copy becomes prose instead */
.fl-launch-root .hypothesis__meta .badge{max-width:100%;overflow:hidden}
.fl-launch-root .fl-launch-hypnote{text-transform:none;letter-spacing:0;font-weight:400;line-height:1.5;overflow-wrap:anywhere}

/* phase strip + live stats */
.fl-launch-root .fl-launch-phases{row-gap:var(--s1)}
.fl-launch-root .fl-launch-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(84px,1fr));gap:var(--s2) var(--s3);min-width:0}
.fl-launch-root .fl-launch-stats .stat__value{font-size:16px}

/* safety rails read as a checklist, not a paragraph */
.fl-launch-root .fl-launch-rails{display:flex;flex-direction:column;gap:var(--s2);margin:0;padding:0;list-style:none}
.fl-launch-root .fl-launch-rails li{display:flex;align-items:flex-start;gap:var(--s2);font-size:12px;line-height:1.5;color:var(--text-dim)}
.fl-launch-root .fl-launch-rails .icon{margin-top:3px;color:var(--ok)}

/* the launch trigger: app.css draws the button, the hold progress is ours */
.fl-launch-root .fl-launch-foot{flex-direction:column;align-items:stretch;gap:var(--s2);padding:var(--s2) var(--s3) var(--s3)}
.fl-launch-root .fl-launch-foothead{gap:var(--s2);color:var(--text-dim)}
.fl-launch-root .fl-launch-foothead .icon{color:var(--text-mute)}
.fl-launch-root .fl-launch-foothead .explain__btn{width:34px;height:34px}
.fl-launch-root .fl-launch-launch{display:flex;flex-direction:column;gap:var(--s2);min-width:0}
.fl-launch-root .btn--launch{position:relative;overflow:hidden}
.fl-launch-root .fl-launch-ring{position:absolute;inset:0;pointer-events:none;background:linear-gradient(90deg,color-mix(in srgb,var(--text) 28%,transparent) 0 var(--fl-hold,0%),transparent var(--fl-hold,0%))}
.fl-launch-root .fl-launch-ring::after{content:"";position:absolute;left:0;bottom:0;height:3px;width:var(--fl-hold,0%);background:var(--text)}
.fl-launch-root .btn--launch.is-armed{background:var(--danger);border-color:var(--danger);color:var(--bg)}
.fl-launch-root .btn--launch[disabled] .fl-launch-ring{display:none}
.fl-launch-root .btn--abort{background:linear-gradient(180deg,color-mix(in srgb,var(--danger) 92%,var(--text)) 0%,var(--danger) 46%,color-mix(in srgb,var(--danger) 76%,var(--bg)) 100%);border-color:color-mix(in srgb,var(--danger) 60%,var(--bg));box-shadow:var(--shadow-inset),0 6px 20px -12px var(--danger)}
.fl-launch-root .btn--abort:hover{border-color:var(--danger);box-shadow:var(--shadow-inset),0 8px 26px -12px var(--danger)}
.fl-launch-root .fl-launch-btn-sub{text-align:center}
.fl-launch-root .fl-launch-more{flex:1 1 100%;text-transform:none;letter-spacing:0;font-weight:400;line-height:1.45}
.fl-launch-root .fl-launch-whyacts{flex:1 1 100%}
.fl-launch-root .fl-launch-ok{gap:var(--s2);color:var(--tone-ok-ink)}
.fl-launch-root .fl-launch-ok .icon{color:var(--ok)}

/* supporting actions */
.fl-launch-root .fl-launch-actions .btn{flex:1 1 auto}
.fl-launch-root .fl-launch-confirm{flex:1 1 100%;align-items:center;row-gap:var(--s2)}
.fl-launch-root .fl-launch-confirmacts{flex:1 1 100%}

@media (prefers-reduced-motion:reduce){
  .fl-launch-root .fl-launch-ring{display:none}
}
`;
