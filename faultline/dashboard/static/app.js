// Faultline Control Room: shell, polling loop and the panels the app owns directly
// (header, phase ribbon, agent pipeline, KPIs, event feed, experiments, scorecard).
// The graph, charts and launcher are separate modules loaded defensively: if one fails to load
// the rest of the room still works, which matters when you are live in front of judges.
import {
  announce, clear, cls, el, fmtDur, fmtMs, fmtNum, fmtPct, getJSON, postJSON, relTime, clockTime,
  riskTone, scoreTone, severityTone, shortId, svgIcon, SEVERITY_ORDER, titleCase,
} from "./util.js";
import {
  CASCADE_MECHANISM, EMPTY, EXPLAIN, FINDING_HELP, PHASES, PHASE_INDEX, cascadeConfidence,
} from "./copy.js";

const POLL_IDLE = 1000;
const POLL_LIVE = 400;
const COLUMNS = ["", "Experiment", "Target", "Failure", "Chosen by", "Success", "Recovery",
                 "Hypothesis", "Risk", "Findings"];

const app = {
  state: null, catalog: null, plan: null, knowledge: null, seq: 0,
  expanded: new Set(), explainAll: false, paused: false,
  mods: {}, sparks: {}, timer: 0, inflight: false, lastPhase: "idle", lastJob: "",
  ribbon: null, ribbonRun: {},
  // cascade attribution: full reports fetched on row expand (id -> detail, null while in flight),
  // the experiment whose cascade the map is replaying, and per-row "?" disclosures left open
  details: new Map(), mapCascade: null, cascadeExplain: new Set(),
};

// ---------------------------------------------------------------- small builders
// An empty .chip__glyph makes app.css render a per-tone glyph, so a chip is never colour alone.
function chip(text, tone, icon, value) {
  return el("span", { class: "chip", "data-tone": tone || "neutral" },
    icon ? el("span", { class: "chip__glyph" }, svgIcon(icon, 12)) : el("span", { class: "chip__glyph" }),
    el("span", { class: "chip__label", text }),
    value ? el("span", { class: "chip__value mono", text: value }) : null);
}

function pill(text, tone, icon, title) {
  return el("span", { class: "pill", "data-tone": tone || "neutral", title: title || text },
    icon ? svgIcon(icon, 13) : null, el("span", { class: "pill__value", text }));
}

function btn(label, icon, opts = {}) {
  return el("button", {
    class: cls("btn", opts.variant && `btn--${opts.variant}`, opts.class),
    type: "button", disabled: opts.disabled, title: opts.title || label,
    "aria-label": opts.ariaLabel || label, onclick: opts.onclick, "data-tone": opts.tone,
  }, icon ? el("span", { class: "btn__icon" }, svgIcon(icon, 14)) : null,
     label ? el("span", { text: label }) : null);
}

function stat(label, value, tone, sub) {
  return el("div", { class: "stat" },
    el("span", { class: "stat__label", text: label }),
    el("span", { class: "stat__value mono num", "data-tone": tone || "neutral", text: value }),
    sub ? el("span", { class: "stat__sub", text: sub }) : null);
}

function emptyState(key, onAction) {
  const e = EMPTY[key];
  return el("div", { class: "empty" },
    svgIcon("flask", 26, { class: "empty__icon" }),
    el("p", { class: "empty__title", text: e.title }),
    el("p", { class: "empty__hint", text: e.hint }),
    e.action && onAction ? btn(e.action, "play", { variant: "ghost", onclick: onAction }) : null);
}

function codeBlock(text) {
  return el("div", { class: "code" },
    el("pre", { class: "mono" }, el("code", { text })),
    el("button", {
      class: "code__copy", type: "button", "aria-label": "Copy to clipboard", title: "Copy",
      onclick: async () => {
        try { await navigator.clipboard.writeText(text); toast("Copied to clipboard", "", "ok"); }
        catch { toast("Copy blocked", "Your browser denied clipboard access", "warn"); }
      },
    }, svgIcon("copy", 13)));
}

// ---------------------------------------------------------------- panel chrome
function panelHead(hostId, icon, title, explainKey, actions = []) {
  const host = document.getElementById(hostId);
  if (!host) return;
  clear(host);
  let body = null, button = null;
  if (explainKey && EXPLAIN[explainKey]) {
    body = el("div", { class: "explain__body", id: `explain-${explainKey}` },
      el("div", { class: "explain__inner" },
        el("strong", { text: EXPLAIN[explainKey].title }),
        el("p", { text: EXPLAIN[explainKey].body })));
    button = el("button", {
      class: "explain__btn", type: "button", "aria-expanded": "false",
      "aria-controls": `explain-${explainKey}`, "aria-label": `What is ${title}?`,
      title: "What is this?", onclick: () => toggleExplain(button, body),
    }, svgIcon("info", 15));
  }
  host.appendChild(el("div", { class: "panel__title" },
    svgIcon(icon, 15, { class: "panel__icon" }), el("span", { text: title })));
  host.appendChild(el("div", { class: "panel__actions" }, ...actions, button));
  if (body) host.parentElement.insertBefore(body, host.nextSibling);
}

function toggleExplain(button, body, force) {
  const open = force !== undefined ? force : body.dataset.open !== "true";
  body.dataset.open = String(open);
  button.setAttribute("aria-expanded", String(open));
  button.classList.toggle("is-open", open);
}

function setExplainAll(on) {
  app.explainAll = on;
  app.cascadeExplain.clear();
  document.querySelectorAll(".explain__btn").forEach((b) => {
    const body = document.getElementById(b.getAttribute("aria-controls"));
    if (body) toggleExplain(b, body, on);
  });
  renderHeaderRight(app.state);
  announce(on ? "Explanations shown on every panel" : "Explanations hidden");
}

// ---------------------------------------------------------------- header
function renderHeaderRight(s) {
  if (!s) return;
  const host = document.getElementById("header-right");
  const llm = s.llm || {};
  const guardianUp = (s.guardian || {}).at > Date.now() / 1000 - 15;
  const stopped = s.stop_requested;
  const live = s.run && s.run.phase !== "idle";
  clear(host);

  host.appendChild(pill(llm.available ? llm.model : "rules only", llm.available ? "violet" : "neutral",
    "brain", llm.available ? `Planner and narration use ${llm.provider}:${llm.model}`
      : `Model unavailable (${llm.error || "disabled"}), planning falls back to deterministic ranking`));

  host.appendChild(pill(guardianUp ? "Guardian" : "No guardian", guardianUp ? "ok" : "neutral", "shield",
    guardianUp ? "Independent watchdog process is running"
      : "Guardian is not running. Start it with: faultline guardian"));

  host.appendChild(pill(s.session.armed ? `${s.session.probe.samples} samples` : "disarmed",
    s.session.armed ? "cyan" : "warn", "activity",
    s.session.armed ? `Synthetic probe at ${s.session.probe.rps} req/s against ${s.session.ditto_url}`
      : "Probes are not running. Arm them from the console on the left"));

  host.appendChild(el("button", {
    class: cls("btn", "btn--ghost", "btn--sm", app.explainAll && "is-active"), type: "button",
    "aria-pressed": String(app.explainAll), title: "Explain every panel (E)",
    onclick: () => setExplainAll(!app.explainAll),
  }, el("span", { class: "btn__icon" }, svgIcon("book", 14)), el("span", { text: "Explain" })));

  host.appendChild(el("button", {
    class: cls("killswitch", stopped && "is-stopped", live && !stopped && "is-armed"), type: "button",
    title: stopped ? "Clear the stop flag so experiments can run again"
      : "Abort the running experiment and restore the sandbox (Esc)",
    onclick: () => act(stopped ? "resume" : "stop"),
  }, el("span", { class: "killswitch__icon" }, svgIcon(stopped ? "play" : "stop", 16)),
     el("span", { text: stopped ? "RESUME" : "STOP" })));
}

// ---------------------------------------------------------------- phase ribbon
const STEP_INJECT = PHASES.findIndex((p) => p.key === "injecting");
const STEP_OBSERVE = PHASES.findIndex((p) => p.key === "observe");
const PROBE_WINDOW_S = 5;   // control.py Telemetry.sample averages the trailing 5 s of probe traffic

// The injector reports a single `injecting` phase for the whole fault hold, so Inject vs Observe is
// derived: Observe lights once a telemetry sample's 5 s window lies wholly after the fault went in,
// because until then the chart and KPIs still mostly show pre-fault traffic. Latched per experiment.
function ribbonIndex(s, run) {
  if (run.phase !== "injecting") return PHASE_INDEX[run.phase] ?? -1;
  const r = app.ribbonRun;
  if (r.exp !== run.experiment_id) Object.assign(r, { exp: run.experiment_id, faultAt: Infinity, observing: false });
  if (!r.observing) {
    // elapsed_s is stamped when live.json was written, so the smallest now-elapsed seen is the best estimate
    if (s.now != null && run.elapsed_s != null) r.faultAt = Math.min(r.faultAt, s.now - run.elapsed_s);
    const pts = (s.telemetry && s.telemetry.points) || [];
    const last = pts[pts.length - 1];
    r.observing = Boolean(last && last.t - PROBE_WINDOW_S >= r.faultAt);
  }
  return r.observing ? STEP_OBSERVE : STEP_INJECT;
}

function setAttr(node, name, value) {
  if (value == null) { if (node.hasAttribute(name)) node.removeAttribute(name); }
  else if (node.getAttribute(name) !== value) node.setAttribute(name, value);
}

function show(node, on) {
  if (node.hidden === !on) return;
  node.hidden = !on;
  node.style.display = on ? "" : "none";   // .ribbon__step sets display, which beats the UA [hidden] rule
}

function buildRibbon(host) {
  clear(host);
  const steps = PHASES.map((p, i) => {
    const link = i ? host.appendChild(el("span", { class: "pipeline__link" })) : null;
    const glyph = el("span", { class: "ribbon__glyph" });
    const node = host.appendChild(el("span", { class: "ribbon__step is-pending", title: p.hint },
      glyph, el("span", { class: "ribbon__label", text: p.label })));
    return { link, glyph, node, state: "pending" };
  });
  const count = host.appendChild(el("span", { class: "ribbon__count mono" }));
  const abort = host.appendChild(el("span", { class: "ribbon__abort" }));
  // after .ribbon__abort so `.ribbon__step + .ribbon__step` never puts a connector in front of it
  const standby = host.appendChild(el("span", { class: "ribbon__step is-pending" },
    el("span", { class: "ribbon__glyph" }), el("span", { class: "ribbon__label" })));
  return { host, steps, count, abort, standby, abortText: null, standbyIcon: null };
}

function renderRibbon(s) {
  const host = document.getElementById("ribbon");
  if (!app.ribbon || app.ribbon.host !== host) app.ribbon = buildRibbon(host);
  const R = app.ribbon;
  const run = s.run || { phase: "idle" };
  const idx = ribbonIndex(s, run);
  const live = idx >= 0;
  setAttr(host, "aria-label", live ? `Experiment phase: ${PHASES[idx].label}` : "No experiment running");

  R.steps.forEach((st, i) => {
    show(st.node, live);
    if (st.link) { show(st.link, live); st.link.classList.toggle("is-done", i <= idx); }
    const state = i < idx ? "done" : i === idx ? "active" : "pending";
    if (state === st.state) return;   // keep the active step's pulse running across polls
    st.node.classList.replace(`is-${st.state}`, `is-${state}`);
    clear(st.glyph);
    if (state === "done") st.glyph.appendChild(svgIcon("check", 11));
    st.state = state;
  });

  const job = !live && s.job && s.job.status === "running" ? s.job.label : null;
  show(R.standby, !live);
  if (!live) {
    const icon = job ? "refresh" : "clock";
    if (R.standbyIcon !== icon) { clear(R.standby.firstChild).appendChild(svgIcon(icon, 12)); R.standbyIcon = icon; }
    const label = job || "Standing by";
    if (R.standby.lastChild.textContent !== label) R.standby.lastChild.textContent = label;
  }

  const wd = live && run.deadline_in_s != null && run.deadline_in_s > 0;
  const countText = !live ? null : wd ? `watchdog ${Math.max(0, Math.round(run.deadline_in_s))}s`
    : run.elapsed_s != null ? `+${fmtDur(run.elapsed_s)}` : null;
  show(R.count, countText != null);
  if (countText != null && R.count.textContent !== countText) R.count.textContent = countText;
  setAttr(R.count, "data-tone", wd ? (run.deadline_in_s < 10 ? "danger" : "warn") : null);
  setAttr(R.count, "title", wd ? "The independent watchdog force-restores the target at this point" : null);

  const reason = live && run.safety && run.safety.abort_reason ? run.safety.abort_reason : null;
  show(R.abort, reason != null);
  if (reason !== R.abortText) {
    clear(R.abort);
    if (reason) R.abort.appendChild(chip(`abort: ${reason}`, "danger", "alert"));
    R.abortText = reason;
  }
}

// ---------------------------------------------------------------- agent pipeline
const PIPELINE = [
  ["onboarding", "book", "Onboarding", "documents an unseen repository"],
  ["topology", "graph", "Topology", "maps services and dependencies"],
  ["scout", "search", "Scout", "predicts weaknesses from static config"],
  ["planner", "brain", "Planner", "ranks and chooses the experiment"],
  ["injector", "zap", "Injector", "applies the fault and measures"],
  ["guardian", "shield", "Guardian", "independent watchdog"],
  ["risk", "gauge", "Risk", "scores severity out of 10"],
  ["fix", "wrench", "Fix", "recommends remediations"],
  ["scorecard", "trend", "Scorecard", "ranks service resilience"],
];

function renderPipeline(s) {
  const host = document.getElementById("pipeline-host");
  if (!host.childElementCount) {
    const strip = el("div", { class: "pipeline" });
    PIPELINE.forEach(([key, icon, label, hint], i) => {
      if (i) strip.appendChild(el("span", { class: "pipeline__link", dataset: { link: key } }));
      strip.appendChild(el("div", { class: "pipeline__node is-idle", dataset: { agent: key },
        title: hint, role: "status" },
        el("span", { class: "node__ring" }, svgIcon(icon, 15)),
        el("span", { class: "node__name", text: label }),
        el("span", { class: "node__detail mono", text: "" })));
    });
    host.appendChild(strip);
  }
  const agents = s.agents || {};
  PIPELINE.forEach(([key], i) => {
    const a = agents[key] || { state: "idle", detail: "" };
    const node = host.querySelector(`[data-agent="${key}"]`);
    if (!node) return;
    node.classList.remove("is-idle", "is-working", "is-done", "is-error");
    node.classList.add(`is-${a.state}`);
    node.querySelector(".node__detail").textContent = a.detail || "";
    node.setAttribute("aria-label", `${key} agent ${a.state}${a.detail ? `: ${a.detail}` : ""}`);
    if (i) {
      const link = host.querySelector(`.pipeline__link[data-link="${key}"]`);
      if (link) {
        link.classList.toggle("is-flow", a.state === "working");
        link.classList.toggle("is-done", a.state === "done");
      }
    }
  });
}

// ---------------------------------------------------------------- KPIs
function renderKpis(s) {
  const host = document.getElementById("kpis-host");
  const run = s.run || {};
  const base = s.baseline;
  const pts = (s.telemetry && s.telemetry.points) || [];
  const latest = pts.length ? pts[pts.length - 1] : null;
  const live = run.phase !== "idle";
  const success = live && run.success_rate != null ? run.success_rate : latest ? latest.success : null;
  const p95 = live && run.p95_ms != null ? run.p95_ms : latest ? latest.p95 : null;
  const tone = success == null ? "neutral" : success >= 0.99 ? "ok" : success >= 0.5 ? "warn" : "danger";
  const label = success == null ? "no data" : success >= 0.99 ? "steady" : success >= 0.5 ? "degraded" : "failing";
  const sc = s.scorecard || {};
  const done = (s.experiments || []).filter((e) => e.status !== "skipped");
  const findings = done.reduce((n, e) => n + (e.findings || []).length, 0);
  const highRisk = done.filter((e) => e.risk && ["high", "critical"].includes(e.risk.level)).length;
  const basep95 = base && base.api ? base.api.p95_ms : null;

  if (!host.childElementCount) host.appendChild(el("div", { class: "kpis", id: "kpis" }));
  const grid = document.getElementById("kpis");
  clear(grid);
  grid.appendChild(kpi("api-success", "API success", success == null ? "—" : fmtPct(success, 1), tone, label,
    pts.map((p) => (p.success == null ? 0 : p.success * 100))));
  grid.appendChild(kpi("api-p95", "p95 latency", fmtMs(p95),
    p95 == null ? "neutral" : basep95 && p95 > basep95 * 3 ? "danger" : basep95 && p95 > basep95 * 1.5 ? "warn" : "cyan",
    basep95 ? `baseline ${fmtMs(basep95)}` : "no baseline yet", pts.map((p) => p.p95 || 0)));
  grid.appendChild(kpi("score", "Resilience", sc.overall_score == null ? "—" : fmtNum(sc.overall_score, 1),
    scoreTone(sc.overall_score),
    sc.overall_score == null ? "not scored yet" : `${done.length} experiment${done.length === 1 ? "" : "s"}`));
  // "weaknesses" now names the Scout's PREDICTIONS; this tile counts measured findings only
  grid.appendChild(kpi("findings", "Findings", String(findings), findings ? (highRisk ? "danger" : "warn") : "neutral",
    highRisk ? `${highRisk} high or critical` : "findings observed"));
}

function kpi(id, label, value, tone, sub, spark) {
  const node = el("div", { class: "kpi", "data-tone": tone },
    el("span", { class: "kpi__label", text: label }),
    el("span", { class: "kpi__value mono num", text: value }),
    el("span", { class: "kpi__sub", text: sub }));
  if (spark && spark.length > 2 && app.mods.charts && app.mods.charts.createSparkline) {
    const h = el("span", { class: "kpi__delta" });
    node.appendChild(h);
    try { app.mods.charts.createSparkline(h, { tone }).update(spark.slice(-60)); } catch { /* decorative */ }
  }
  return node;
}

// ---------------------------------------------------------------- event feed
function renderFeedDelta(events) {
  const host = document.getElementById("feed-host");
  if (!host.querySelector(".feed__list")) {
    clear(host);
    host.appendChild(el("div", { class: "feed" }, el("div", { class: "feed__list", id: "feed-list" })));
  }
  const list = document.getElementById("feed-list");
  const pinned = host.scrollTop < 40;
  for (const ev of events) {
    const row = el("div", {
      class: cls("feed__row", "is-new", ev.level === "chaos" && "feed__row--chaos", ev.level === "ai" && "feed__row--ai"),
      "data-tone": feedTone(ev.level),
    },
      el("span", { class: "feed__time mono", text: clockTime(ev.ts) }),
      el("span", { class: "feed__src", text: ev.source }),
      el("span", { class: "feed__text", text: ev.text }));
    list.insertBefore(row, list.firstChild);
    window.setTimeout(() => row.classList.remove("is-new"), 700);
  }
  while (list.childElementCount > 400) list.removeChild(list.lastChild);
  if (pinned) host.scrollTop = 0;
}

function feedTone(level) {
  return { error: "danger", warn: "warn", success: "ok", chaos: "chaos", ai: "violet" }[level] || "neutral";
}

// ---------------------------------------------------------------- experiments
function renderExperiments(s) {
  const host = document.getElementById("experiments-host");
  const list = s.experiments || [];
  clear(host);
  if (!list.length) { host.appendChild(emptyState("experiments")); return; }

  const thead = el("thead", null, el("tr", null,
    ...COLUMNS.map((h) => el("th", { scope: "col", text: h }))));
  const tbody = el("tbody");
  for (const e of list) {
    const open = app.expanded.has(e.experiment_id);
    tbody.appendChild(expRow(e, open));
    const detail = el("tr", { class: "exp-detail-row", hidden: !open },
      el("td", { class: "exp-detail-cell", colspan: COLUMNS.length },
        open ? expDetail(e) : el("span")));
    tbody.appendChild(detail);
  }
  host.appendChild(el("table", { class: "exp-table" }, thead, tbody));
}

function expRow(e, open) {
  const sTone = e.during_success_rate == null ? "neutral"
    : e.during_success_rate >= 0.95 ? "ok" : e.during_success_rate >= 0.5 ? "warn" : "danger";
  const hyp = e.hypothesis_held === true ? ["held", "ok", "check"]
    : e.hypothesis_held === false ? ["violated", "danger", "x"] : ["n/a", "neutral", "info"];
  const by = e.planned_by === "llm" ? ["AI", "violet", "brain"]
    : e.planned_by === "operator" ? ["You", "cyan", "terminal"] : ["Rules", "neutral", "grid"];
  return el("tr", {
    class: cls("exp-row", open && "is-open"), tabindex: "0", role: "button",
    "aria-expanded": String(open),
    "aria-label": `Experiment ${shortId(e.experiment_id)}: ${e.failure_type} on ${e.target_service}, ${e.status}`,
    onclick: () => toggleRow(e.experiment_id),
    onkeydown: (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); toggleRow(e.experiment_id); } },
  },
    el("td", null, el("span", { class: "exp-row__chev" }, svgIcon("chevron", 13))),
    el("td", null, el("span", { class: "mono", text: shortId(e.experiment_id) }),
      el("span", { class: "micro dim", text: relTime(e.started_at) })),
    el("td", null, chip(e.target_service || "—", "neutral", "server")),
    el("td", { class: "mono nowrap", text: e.failure_type }),
    el("td", null, chip(by[0], by[1], by[2])),
    el("td", { class: "num mono", "data-tone": sTone, text: fmtPct(e.during_success_rate, 0) }),
    el("td", { class: "num mono", text: e.time_to_recover_s == null ? "—" : fmtDur(e.time_to_recover_s) }),
    el("td", null, chip(hyp[0], hyp[1], hyp[2])),
    el("td", null, e.risk ? chip(e.risk.level, riskTone(e.risk.level), "gauge", fmtNum(e.risk.severity, 1))
      : chip("—", "neutral")),
    el("td", { class: "num mono", text: String((e.findings || []).length) }));
}

function toggleRow(id) {
  const opening = !app.expanded.has(id);
  opening ? app.expanded.add(id) : app.expanded.delete(id);
  // expanding a row with a cascade replays it on the map (a live run still wins); collapsing it clears
  const e = ((app.state && app.state.experiments) || []).find((x) => x.experiment_id === id);
  if (opening && e && e.cascade_hops_beyond_hypothesis != null) app.mapCascade = id;
  else if (!opening && app.mapCascade === id) app.mapCascade = null;
  renderExperiments(app.state);
  syncMapCascade();
}

// ---------------------------------------------------------------- cascade attribution
// The summary in /api/state only says whether a report has a cascade; the hops, edges and summary
// come from GET /api/experiment/<id>, fetched once per experiment when its row is first expanded.
async function loadDetail(id) {
  if (app.details.has(id)) return;
  app.details.set(id, null);
  try {
    app.details.set(id, await getJSON(`/api/experiment/${encodeURIComponent(id)}`));
  } catch {
    app.details.delete(id);   // retried on the next render of the open row
    return;
  }
  if (app.state) renderExperiments(app.state);
  syncMapCascade();
}

function cascadeOf(id) {
  const d = app.details.get(id);
  return (d && d.experiment && d.experiment.cascade) || null;
}

function syncMapCascade() {
  const t = app.mods.topology;
  if (!t || !t.showCascade) return;
  const id = app.mapCascade;
  const e = id && ((app.state && app.state.experiments) || []).find((x) => x.experiment_id === id);
  const c = e && cascadeOf(id);
  if (!c) { t.showCascade(null); return; }
  const hops = c.hops || [];
  t.showCascade({
    id, label: shortId(id), note: `${e.failure_type} on ${e.target_service}`,
    target: c.origin || e.target_service,
    expected: (e.hypothesis && e.hypothesis.expected_affected) || [],
    observed: [...new Set([...(e.affected_services_observed || []), ...hops.map((h) => h.service)])],
    edges: c.edges || [], hops,
  });
}

const fmt1 = (v) => Number(v).toFixed(1);   // onsets keep their decimal: +3.0 s, not +3 s
function onset(h) {
  if (h.path_confidence === "origin") return "+0.0 s";
  const a = h.onset_earliest_s, b = h.onset_latest_s;
  if (a == null && b == null) return "—";
  if (a == null || b == null || a === b) return `+${fmt1(a ?? b)} s`;
  return `+${fmt1(a)}–${fmt1(b)} s`;
}

// confidence chip: tone + glyph + text, never colour alone
const CONF_CHIP = {
  origin: ["origin", "chaos"], ordered: ["ordered", "cyan"], same_interval: ["same interval", "warn"],
  ambiguous: ["ambiguous", "warn"], no_graph_path: ["no graph path", "warn"],
};

function cascadeHopRow(h) {
  const [label, tone] = CONF_CHIP[h.path_confidence] || [h.path_confidence, "neutral"];
  const alts = [...new Set((h.candidate_paths || []).map((p) => p[1]).filter((x) => x && x !== h.upstream))];
  return el("tr", { class: cls("cascade__hop", h.path_confidence === "origin" && "is-origin") },
    el("th", { scope: "row", class: "mono" }, h.service),
    el("td", { class: "mono nowrap", text: onset(h) }),
    el("td", null,
      el("span", { class: "cascade__mech", text: CASCADE_MECHANISM[h.mechanism] || h.mechanism }),
      h.mechanism_evidence
        ? el("span", { class: "cascade__ev mono", title: h.mechanism_evidence, text: h.mechanism_evidence }) : null),
    el("td", { class: "mono" }, h.upstream || "—",
      alts.length ? el("span", { class: "cascade__ev", text: `or via ${alts.join(", ")}` }) : null),
    el("td", { title: cascadeConfidence(h.path_confidence, h.upstream, h.lag_s) },
      chip(label, tone, null, h.path_confidence === "ordered" && typeof h.lag_s === "number"
        ? `+${fmt1(h.lag_s)} s` : null)),
    el("td", null, el("span", { class: "cascade__tags" },
      h.masked_by_health_check ? chip("masked: health check green", "warn") : null,
      h.beyond_hypothesis ? chip("beyond hypothesis", "critical") : null,
      h.phase === "after_rollback" ? chip("after rollback", "neutral") : null)));
}

function cascadeSection(e) {
  if (e.cascade_hops_beyond_hypothesis == null) return null;   // report predates cascade attribution
  const id = e.experiment_id;
  if (!app.details.get(id)) {
    loadDetail(id);
    return section("Cascade", "graph", el("p", { class: "micro dim", text: "Loading the cascade ..." }));
  }
  const c = cascadeOf(id);
  if (!c) return null;

  const open = app.explainAll || app.cascadeExplain.has(id);
  const body = el("div", { class: "explain__body", id: `explain-cascade-${id}`, "data-open": String(open) },
    el("div", { class: "explain__inner" },
      el("strong", { text: EXPLAIN.cascade.title }), el("p", { text: EXPLAIN.cascade.body })));
  const help = el("button", {
    class: cls("explain__btn", open && "is-open"), type: "button", "aria-expanded": String(open),
    "aria-controls": `explain-cascade-${id}`, "aria-label": "What is the cascade?", title: "What is this?",
    onclick: () => {
      toggleExplain(help, body);
      body.dataset.open === "true" ? app.cascadeExplain.add(id) : app.cascadeExplain.delete(id);
    },
  }, svgIcon("info", 15));
  const onMap = app.mapCascade === id;
  const mapBtn = btn(onMap ? "On the map" : "Show on map", "graph", {
    variant: "ghost", class: cls("btn--sm", onMap && "is-active"),
    title: onMap ? "The application map is replaying this cascade (a live run takes over)" : "Replay this cascade on the application map",
    onclick: () => {
      app.mapCascade = onMap ? null : id;
      renderExperiments(app.state);
      syncMapCascade();
      if (!onMap) (document.getElementById("graph-host") || document.body).scrollIntoView({ behavior: "smooth", block: "center" });
    },
  });
  mapBtn.setAttribute("aria-pressed", String(onMap));

  const hops = c.hops || [];
  return el("div", { class: "exp-sec exp-sec--full cascade" },
    el("h4", { class: "exp-sec__title" }, svgIcon("graph", 13),
      el("span", { text: `Cascade: ${Math.max(0, hops.length - 1)} service${hops.length === 2 ? "" : "s"} failed after ${c.origin}` }),
      el("span", { class: "spacer" }), mapBtn, help),
    body,
    c.summary ? el("p", { class: "cascade__summary", text: c.summary }) : null,
    hops.length ? el("div", { class: "cascade__scroll" },
      el("table", { class: "cascade__table" },
        el("thead", null, el("tr", null,
          ...["Service", "Onset", "How it failed", "Via", "Confidence", ""].map((h) => el("th", { scope: "col", text: h })))),
        el("tbody", null, ...hops.map(cascadeHopRow)))) : null,
    (c.notes || []).length ? el("ul", { class: "cascade__notes" }, ...c.notes.map((n) => el("li", { text: n }))) : null);
}

// one "what to fix, how to fix" card. Shared by the per-experiment detail (§expDetail)
// and the standalone Recommended Fixes panel, so both read the same recommendation the
// same way.
function recCard(r, titleText) {
  return el("div", { class: "rec" },
    el("div", { class: "rec__head" },
      el("span", { class: "rec__title", text: titleText }),
      el("span", { class: "rec__badges" },
        chip(r.category, "cyan"), chip(`effort ${r.effort}`, "neutral"),
        chip("confidence", r.confidence >= 0.7 ? "ok" : "warn", null, `${Math.round((r.confidence || 0) * 100)}%`))),
    el("p", { class: "rec__rationale", text: r.rationale }),
    r.steps && r.steps.length
      ? el("ol", { class: "rec__steps" }, ...r.steps.map((x) => el("li", { class: "rec__step", text: x })))
      : null,
    r.verify && r.verify.length ? el("div", { class: "rec__verify" }, codeBlock(r.verify.join("\n"))) : null,
    r.references && r.references.length
      ? el("p", { class: "rec__rationale" }, ...r.references.map((u) =>
          el("a", { href: u, target: "_blank", rel: "noreferrer noopener", text: u })))
      : null);
}

function section(title, icon, ...children) {
  return el("div", { class: "exp-sec" },
    el("h4", { class: "exp-sec__title" }, svgIcon(icon, 13), el("span", { text: title })),
    ...children);
}

function expDetail(e) {
  const d = el("div", { class: "exp-detail" });

  d.appendChild(el("div", { class: "exp-sec exp-sec--full" },
    el("p", { class: "rec__rationale" }, el("strong", { text: "Why this experiment: " }), e.rationale || "—"),
    el("div", { class: "hypothesis" },
      el("span", { class: "hypothesis__label", text: "Hypothesis" }),
      el("p", { class: "hypothesis__text mono", text: e.hypothesis ? e.hypothesis.statement : "—" }),
      e.hypothesis_notes ? el("p", { class: "hypothesis__meta", text: e.hypothesis_notes }) : null)));

  d.appendChild(section("Measured impact", "activity",
    el("div", { class: "exp-card__grid" },
      stat("Success before", fmtPct(e.baseline_success_rate, 1), "ok"),
      stat("Success during", fmtPct(e.during_success_rate, 1),
        e.during_success_rate >= 0.95 ? "ok" : e.during_success_rate >= 0.5 ? "warn" : "danger"),
      stat("Success after", fmtPct(e.after_success_rate, 1), "ok"),
      stat("p95 before", fmtMs(e.baseline_p95_ms)),
      stat("p95 during", fmtMs(e.during_p95_ms),
        e.during_p95_ms > (e.baseline_p95_ms || 0) * 3 ? "danger" : "warn"),
      stat("Time to recover", fmtDur(e.time_to_recover_s), e.time_to_recover_s > 60 ? "warn" : "ok"),
      stat("Client-visible outage", fmtDur(e.outage_duration_s), "warn"),
      stat("Docker auto-restart", e.auto_restarted_by_docker == null ? "—" : e.auto_restarted_by_docker ? "yes" : "no"),
      stat("Acknowledged writes kept", e.data_integrity_ok == null ? "—" : e.data_integrity_ok ? "yes" : "NO",
        e.data_integrity_ok === false ? "critical" : "ok", e.data_integrity_detail || ""),
      stat("Blast radius", String((e.affected_services_observed || []).length),
        "neutral", (e.affected_services_observed || []).join(", ") || "none"))));

  const cascade = cascadeSection(e);
  if (cascade) d.appendChild(cascade);

  if (e.remediation_actions && e.remediation_actions.length) {
    d.appendChild(section("Manual intervention required", "wrench",
      el("ol", { class: "rec__steps" },
        ...e.remediation_actions.map((a) => el("li", { class: "rec__step", text: a })))));
  }

  const findings = [...(e.findings || [])].sort(
    (a, b) => (SEVERITY_ORDER[a.severity] ?? 9) - (SEVERITY_ORDER[b.severity] ?? 9));
  if (findings.length) {
    d.appendChild(section(`Findings (${findings.length})`, "search",
      ...findings.map((f) => el("div", { class: "finding", "data-sev": f.severity,
          "data-tone": severityTone(f.severity) },
        el("div", { class: "finding__head" },
          chip(f.severity, severityTone(f.severity), f.severity === "info" ? "info" : "alert"),
          el("span", { class: "finding__title", text: f.title }),
          el("code", { class: "finding__code", text: f.code })),
        el("p", { class: "finding__evidence mono", text: f.evidence }),
        FINDING_HELP[f.code] ? el("p", { class: "rec__rationale", text: FINDING_HELP[f.code] }) : null))));
  }

  if (e.risk) {
    d.appendChild(el("div", { class: "exp-sec exp-sec--full" },
      el("div", { class: "risk", "data-tone": riskTone(e.risk.level) },
        el("span", { class: "risk__sev mono", text: fmtNum(e.risk.severity, 1) }),
        el("span", { class: "risk__level" }, chip(`${e.risk.level} risk`, riskTone(e.risk.level), "gauge")),
        el("p", { class: "risk__summary", text: e.risk.summary }),
        el("p", { class: "risk__impl" }, el("strong", { text: "In production: " }), e.risk.production_implication),
        el("p", { class: "risk__factors mono",
          text: `blast radius ${e.risk.blast_radius} · scored by ${e.risk.generated_by}` }))));
  }

  if (e.fix && e.fix.recommendations && e.fix.recommendations.length) {
    d.appendChild(section(
      `Recommended fixes (${e.fix.recommendations.length}): advisory only, nothing is applied`, "wrench",
      ...e.fix.recommendations.map((r, i) => recCard(r, `${i + 1}. ${r.title}`))));
  }

  if (e.timeline && e.timeline.length) {
    d.appendChild(section("Timeline", "clock",
      el("div", { class: "tl" }, ...e.timeline.map((t) => el("div", { class: "tl__item" },
        el("span", { class: "tl__t", text: `+${fmtNum(t.t_plus_s, 1)}s` }),
        el("span", null,
          el("span", { class: "tl__event", text: titleCase(t.event) }),
          t.detail ? el("span", { class: "tl__detail mono", text: t.detail }) : null))))));
  }

  if (e.reproduce && e.reproduce.length) {
    d.appendChild(section("Reproduce by hand", "terminal", codeBlock(e.reproduce.join("\n"))));
  }
  return d;
}

// ---------------------------------------------------------------- predicted weaknesses
// The Weakness Scout statically reads the deployment and PREDICTS where it will break, before any
// experiment exists. Provenance is the whole job of this renderer: every row carries the project's
// "AI-authored / not measured" channel (data-tone="violet", UI_CONTRACT §1) plus the record's
// literal `label` ("[predicted, unconfirmed]") as visible text, and riskTone() tints only the
// inner severity number — predicted_level shares RiskAssessment.level's domain, so the number
// alone is never allowed to stand in for the Risk agent's measured severity.
function renderWeaknesses(s) {
  const host = document.getElementById("weaknesses-host");
  if (!host) return;
  const w = s.weaknesses;
  // same cheap-and-idempotent discipline as renderScorecard: only rebuild when the report changed
  const key = JSON.stringify(w || null);
  if (key === app.weaknessKey) return;
  app.weaknessKey = key;
  clear(host);

  const list = (w && w.weaknesses) || [];
  if (!list.length) {
    host.appendChild(emptyState("weaknesses", () => act("scout", { llm: Boolean((s.llm || {}).available) })));
    return;
  }

  const label = w.label || "[predicted, unconfirmed]";
  const wrap = el("div", { class: "weaknesses" });
  wrap.appendChild(el("div", { class: "banner", "data-tone": "violet" },
    svgIcon("brain", 15),
    el("span", null,
      el("strong", { text: `${label} ` }),
      el("span", { text: `${list.length} weakness${list.length === 1 ? "" : "es"} predicted by reading ` }),
      el("code", { class: "mono", text: w.compose_file || "the compose file" }),
      el("span", { text: `${w.knowledge_file ? " and the documented knowledge file" : ""} — static analysis only, nothing here has been measured. Each one names the experiment that would confirm or clear it.` }),
      el("span", { class: "micro dim", text: ` (${w.checks_run ? w.checks_run.length : 0} check(s), by ${w.generated_by || "rules"}, ${relTime(w.generated_at)})` }))));

  const rows = el("div", { class: "weaknesses__list" });
  for (const x of list) rows.appendChild(weaknessRow(x, label));
  wrap.appendChild(section(`Predicted, worst first — ${label}`, "search", rows));

  const patterns = (w.systemic_patterns || []);
  if (patterns.length) {
    const byId = new Map(list.map((x) => [x.weakness_id, x]));
    wrap.appendChild(section("Systemic patterns — language-model narrative across the predictions above, not findings", "brain",
      ...patterns.map((sp) => el("div", { class: "weak-pattern", "data-tone": "violet" },
        el("div", { class: "row row--wrap" },
          chip("LLM narrative", "violet", "brain"),
          el("span", { class: "weak-pattern__title", text: sp.title }),
          el("span", { class: "weak__label", text: label })),
        el("p", { class: "weak-pattern__text", text: sp.narrative }),
        el("p", { class: "weak__caveat",
          text: `Over predicted weakness(es): ${(sp.weakness_ids || []).map((id) => (byId.get(id) || {}).title || id).join("; ") || "—"}` })))));
  }

  if (w.notes && w.notes.length) {
    wrap.appendChild(el("p", { class: "micro dim", text: `Scout notes: ${w.notes.join(" · ")}` }));
  }
  host.appendChild(wrap);
}

function weaknessRow(x, reportLabel) {
  const lvl = String(x.predicted_level || "");
  const label = x.label || reportLabel;
  const se = x.suggested_experiment || {};
  const codes = x.confirming_finding_codes || [];
  return el("div", {
    class: "finding finding--predicted", "data-tone": "violet",
    "aria-label": `Predicted ${lvl} weakness ${label}: ${x.title}`,
  },
    el("div", { class: "finding__head" },
      chip(`predicted · ${lvl || "unrated"}`, "violet", "brain"),
      // the only place the level's own colour appears, and never without the label beside it
      el("span", { class: "weak__sev mono", "data-tone": riskTone(lvl) },
        el("span", { text: fmtNum(x.predicted_severity, 1) }),
        el("span", { class: "weak__sev-unit", text: "/10 predicted" })),
      el("span", { class: "weak__label", text: label }),
      el("span", { class: "finding__title", text: x.title }),
      el("code", { class: "finding__code", text: x.category }),
      x.status && x.status !== "predicted" ? chip(`since ${x.status}`, "cyan", "info") : null),
    el("div", { class: "row row--wrap" },
      ...(x.services || []).map((n) => chip(n, "neutral", "server"))),
    x.evidence ? el("p", { class: "finding__evidence mono", text: x.evidence }) : null,
    x.why_it_matters ? el("p", { class: "weak__why", text: x.why_it_matters }) : null,
    el("p", { class: "weak__exp" },
      el("span", { text: "would be tested by" }),
      el("code", { text: se.failure_type || "—" }),
      el("span", { text: "on" }),
      el("code", { text: se.target_service || "—" }),
      se.reason ? el("span", { class: "dim", text: `· ${se.reason}` }) : null,
      se.policy_note ? el("span", { class: "weak__policy", text: `· ${se.policy_note}` }) : null),
    codes.length ? el("p", { class: "weak__caveat",
      text: `Counts as confirmed only once an experiment produces: ${codes.join(", ")}` }) : null);
}

// ---------------------------------------------------------------- scorecard
function renderScorecard(s) {
  const host = document.getElementById("scorecard-host");
  const sc = s.scorecard;
  // rebuilding every poll replayed the ring animation and reset the scroll: only rebuild on change
  const key = JSON.stringify(sc || null);
  if (key === app.scorecardKey) return;
  app.scorecardKey = key;
  clear(host);
  if (!sc || !sc.ranking || !sc.ranking.length) { host.appendChild(emptyState("scorecard")); return; }

  const ringHost = el("div");
  host.appendChild(el("div", { class: "scorecard" }, ringHost,
    el("p", { class: "scorecard__summary", text: sc.summary || "" })));
  if (app.mods.charts && app.mods.charts.createScoreRing) {
    try {
      app.mods.charts.createScoreRing(ringHost, {}).update(sc.overall_score, grade(sc.overall_score));
    } catch { /* the ranked list below carries the same data */ }
  }

  const rank = el("div", { class: "rank" });
  for (const r of sc.ranking) {
    rank.appendChild(el("div", { class: "rank__row", "data-tone": r.score == null ? "neutral" : scoreTone(r.score),
        title: (r.notes || []).join("; ") },
      el("span", { class: "rank__name", text: r.service }),
      el("span", { class: "rank__score mono num", text: r.score == null ? "untested" : fmtNum(r.score, 1) }),
      el("span", { class: "rank__grade mono", text: r.grade || "—" }),
      el("span", { class: "rank__bar bar" },
        el("span", { class: "bar__fill", style: { "--v": String((r.score == null ? 0 : r.score) / 100) } }))));
  }
  host.appendChild(rank);
}

function grade(score) {
  if (score == null) return "—";
  return score >= 90 ? "A" : score >= 80 ? "B" : score >= 65 ? "C" : score >= 50 ? "D" : "F";
}

// ---------------------------------------------------------------- recommended fixes
// Every experiment's report already carries its own fix recommendations (see expDetail);
// this panel pools them across all experiments so the worst problem is visible without
// having to open rows one by one, worst risk first.
function renderFixes(s) {
  const host = document.getElementById("fixes-host");
  const list = s.experiments || [];
  const items = [];
  for (const e of list) {
    for (const r of (e.fix && e.fix.recommendations) || []) items.push({ e, r });
  }
  const key = JSON.stringify(items.map(({ e, r }) => [e.experiment_id, r.title, r.confidence]));
  if (key === app.fixesKey) return;
  app.fixesKey = key;
  clear(host);
  if (!items.length) { host.appendChild(emptyState("fixes")); return; }

  items.sort((a, b) => (sevNum(b.e) - sevNum(a.e)) || ((b.r.confidence || 0) - (a.r.confidence || 0)));
  const top = items.slice(0, 8);
  for (const { e, r } of top) {
    const card = recCard(r, r.title);
    // evidence is one element built through el(), so this native prepend never sees
    // a bare null the way launchBox.append() once did
    card.prepend(el("div", { class: "rec__evidence row row--wrap" },
      e.risk ? chip(`${e.risk.level} risk`, riskTone(e.risk.level), "gauge", fmtNum(e.risk.severity, 1)) : null,
      chip(e.target_service || "—", "neutral", "server"),
      el("span", { class: "micro dim",
        text: `${titleCase(e.failure_type || "")} · ${shortId(e.experiment_id)}` }),
      el("button", {
        type: "button", class: "btn btn--sm btn--ghost",
        onclick: () => jumpToExperiment(e.experiment_id),
      }, el("span", { text: "View evidence" }))));
    host.appendChild(card);
  }
  if (items.length > top.length) {
    host.appendChild(el("p", { class: "micro dim",
      text: `${items.length - top.length} more recommendation(s) in the experiments below.` }));
  }
}

function sevNum(e) { return e.risk ? Number(e.risk.severity || 0) : 0; }

function jumpToExperiment(id) {
  app.expanded.add(id);
  renderExperiments(app.state);
  const row = document.querySelector(`[aria-label^="Experiment ${shortId(id)}"]`);
  (row || document.getElementById("experiments-host")).scrollIntoView({ behavior: "smooth", block: "center" });
}

// ---------------------------------------------------------------- toasts
function toast(title, text = "", tone = "neutral", ms = 5000) {
  const host = document.getElementById("toasts");
  const node = el("div", { class: "toast", "data-tone": tone, role: "status" },
    svgIcon(tone === "danger" ? "alert" : tone === "ok" ? "check" : "info", 15),
    el("div", { class: "toast__body" },
      el("div", { class: "toast__title", text: title }),
      text ? el("div", { class: "toast__text", text }) : null),
    el("button", { class: "toast__close", type: "button", "aria-label": "Dismiss",
      onclick: () => node.remove() }, svgIcon("x", 13)));
  host.appendChild(node);
  window.setTimeout(() => { node.classList.add("is-out"); window.setTimeout(() => node.remove(), 320); }, ms);
}

// ---------------------------------------------------------------- actions
async function act(kind, body = {}) {
  try {
    if (kind === "stop") { await postJSON("/api/stop"); toast("Kill switch fired", "Rolling back and restoring the sandbox", "danger"); }
    else if (kind === "resume") { await postJSON("/api/resume"); toast("Stop cleared", "Experiments may run again", "ok"); }
    else if (kind === "arm") { await postJSON("/api/arm"); toast("Session armed", "Probes and health checks are running", "ok"); }
    else if (kind === "disarm") { await postJSON("/api/disarm"); toast("Session disarmed", "", "warn"); }
    else if (kind === "cancel") { await postJSON("/api/jobs/cancel"); toast("Cancelling", "", "warn"); }
    else { const r = await postJSON(`/api/jobs/${kind}`, body); toast("Started", r.job.label, "ok"); }
    await poll(true);
  } catch (err) {
    toast("Action failed", err.message || String(err), "danger", 9000);
    throw err;
  }
}

async function refreshPlan(useLlm) {
  try { app.plan = await getJSON(`/api/plan?llm=${useLlm ? 1 : 0}`); return app.plan; }
  catch (err) { toast("Planner unavailable", err.message, "warn"); return null; }
}

async function refreshCatalog() {
  try { app.catalog = await getJSON("/api/catalog"); } catch { /* retried on the next poll */ }
}

// The knowledge files on disk change only when an onboard job writes one, so this is fetched once
// at boot and again whenever that job ends — never on the poll loop.
async function refreshKnowledge() {
  try { app.knowledge = await getJSON("/api/knowledge"); } catch { /* retried after the next onboard */ }
}

// ---------------------------------------------------------------- render + poll
function render(s) {
  app.state = s;
  renderHeaderRight(s);
  renderRibbon(s);
  renderPipeline(s);
  renderKpis(s);
  renderWeaknesses(s);
  renderExperiments(s);
  renderScorecard(s);
  renderFixes(s);
  const root = document.getElementById("app");
  root.dataset.phase = (s.run && s.run.phase) || "idle";
  root.dataset.stopped = String(Boolean(s.stop_requested));
  document.querySelectorAll(".panel--graph, .panel--chart").forEach((p) =>
    p.classList.toggle("is-live", Boolean(s.run && s.run.phase !== "idle")));

  if (app.mods.topology) { try { app.mods.topology.update(s); } catch (e) { console.warn("topology", e); } }
  if (app.mods.chart) { try { app.mods.chart.update(s.telemetry, s.run, s.baseline); } catch (e) { console.warn("chart", e); } }
  if (app.mods.launcher) { try { app.mods.launcher.update(s, app.catalog, app.plan); } catch (e) { console.warn("launcher", e); } }
  if (app.mods.onboard) { try { app.mods.onboard.update(s, app.knowledge); } catch (e) { console.warn("onboard", e); } }
}

function announceTransitions(s) {
  const phase = (s.run && s.run.phase) || "idle";
  if (phase !== app.lastPhase) {
    if (phase !== "idle") announce(`Phase ${phase}${s.run.target_service ? `, target ${s.run.target_service}` : ""}`);
    else if (app.lastPhase !== "idle") announce("Experiment finished");
    app.lastPhase = phase;
  }
  const job = s.job;
  const sig = job ? `${job.id}:${job.status}` : "";
  if (sig !== app.lastJob) {
    if (job && job.status === "failed") toast(`${job.label} failed`, job.error || "", "danger", 10000);
    else if (job && job.status === "done" && app.lastJob.startsWith(`${job.id}:`)) toast("Finished", job.label, "ok");
    // an onboard job writes a new knowledge file; re-read the list once it stops running
    if (job && job.kind === "onboard" && job.status !== "running") refreshKnowledge();
    app.lastJob = sig;
  }
}

async function poll(force = false) {
  if (app.inflight || (app.paused && !force)) return;
  app.inflight = true;
  try {
    const s = await getJSON(`/api/state?since=${app.seq}`);
    app.seq = s.seq;
    renderFeedDelta(s.events || []);
    // the feed owns the full stream; the onboarding panel shows its own last few progress lines
    s.onboarding_events = (s.events || []).filter((ev) => ev.source === "onboarding");
    s.events = [];
    announceTransitions(s);
    render(s);
    document.body.classList.remove("is-offline");
  } catch {
    document.body.classList.add("is-offline");
  } finally {
    app.inflight = false;
    schedule();
  }
}

function schedule() {
  clearTimeout(app.timer);
  const live = app.state && app.state.run && app.state.run.phase !== "idle";
  app.timer = window.setTimeout(poll, live ? POLL_LIVE : POLL_IDLE);
}

// ---------------------------------------------------------------- boot
async function loadModule(path, name) {
  try { return await import(path); }
  catch (err) {
    console.warn(`module ${name} unavailable`, err);
    toast(`${name} unavailable`, "The rest of the control room still works", "warn", 9000);
    return null;
  }
}

async function boot() {
  panelHead("head-pipeline", "grid", "Agent pipeline", "pipeline");
  panelHead("head-graph", "graph", "Application map", "graph",
    [btn("Rebuild", "refresh", { variant: "ghost", class: "btn--sm", onclick: () => act("topology") })]);
  panelHead("head-chart", "activity", "Live telemetry", "chart");
  panelHead("head-kpis", "gauge", "Right now", "kpis");
  panelHead("head-feed", "terminal", "Agent activity", "feed");
  panelHead("head-weaknesses", "search", "Predicted weaknesses", "weaknesses",
    [btn("Scout", "search", { variant: "ghost", class: "btn--sm",
      title: "Re-read the deployment's configuration and predict where it is weak. Breaks nothing.",
      onclick: () => act("scout", { llm: true }) })]);
  panelHead("head-experiments", "flask", "Experiments", "experiments",
    [btn("Analyse", "brain", { variant: "ghost", class: "btn--sm", onclick: () => act("analyze", { llm: true }) })]);
  panelHead("head-scorecard", "trend", "Resilience scorecard", "scorecard");
  panelHead("head-fixes", "wrench", "Recommended fixes", "fixes");

  const [charts, topology, launcher, onboard] = await Promise.all([
    loadModule("./charts.js", "Telemetry charts"),
    loadModule("./topology.js", "Application map"),
    loadModule("./launcher.js", "Injection console"),
    loadModule("./onboard.js", "Repo onboarding"),
  ]);

  app.mods.charts = charts;
  if (charts && charts.createLiveChart) {
    app.mods.chart = charts.createLiveChart(document.getElementById("chart-host"), {});
  }
  if (topology && topology.createTopology) {
    app.mods.topology = topology.createTopology(document.getElementById("graph-host"), {
      onSelect: (name) => {
        if (app.mods.launcher && app.mods.launcher.selectTarget) app.mods.launcher.selectTarget(name);
      },
      onReplayClear: () => { app.mapCascade = null; if (app.state) renderExperiments(app.state); },
    });
  }
  if (launcher && launcher.createLauncher) {
    app.mods.launcher = launcher.createLauncher(document.getElementById("launcher-host"), {
      onLaunch: (payload) => act("run", payload),
      onPlan: ({ llm }) => refreshPlan(llm),
      onStop: () => act("stop"),
      onJob: (kind, body) => act(kind, body),
    });
  }
  const onboardHost = document.getElementById("onboard-host");
  if (onboard && onboard.createOnboard && onboardHost) {
    app.mods.onboard = onboard.createOnboard(onboardHost, {
      onJob: (kind, body) => act(kind, body),
    });
  }

  await Promise.all([refreshCatalog(), refreshKnowledge()]);
  await poll(true);
  refreshPlan(false);

  document.addEventListener("keydown", (e) => {
    if (e.target.matches("input, textarea, select, [contenteditable]")) return;
    if (e.key === "Escape") act("stop");
    else if (e.key.toLowerCase() === "e") setExplainAll(!app.explainAll);
    else if (e.key === " ") {
      e.preventDefault();
      app.paused = !app.paused;
      toast(app.paused ? "Updates paused" : "Updates resumed", "Press space again to toggle", "warn", 2500);
      if (!app.paused) poll(true);
    }
  });

  const root = document.getElementById("app");
  const setDrawer = (open) => {
    root.dataset.drawer = open ? "open" : "closed";
    const t = document.getElementById("drawer-toggle");
    if (t) t.setAttribute("aria-expanded", String(open));
  };
  const scrim = document.getElementById("drawer-scrim");
  if (scrim) scrim.addEventListener("click", () => setDrawer(false));
  const toggle = document.getElementById("drawer-toggle");
  if (toggle) toggle.addEventListener("click", () => setDrawer(root.dataset.drawer !== "open"));

  window.addEventListener("beforeunload", () => clearTimeout(app.timer));
}

boot();
