// onboard.js — Repo Onboarding: point Faultline at any docker-compose application.
// Contract: docs/UI_CONTRACT.md §2 (POST /api/jobs/onboard, GET /api/knowledge), §3 (export),
// §4 (classes), §5 (explain), §6 (a11y). Owns its subtree inside hostEl and writes nowhere else.
//
// PRESENTATION: everything visual comes from app.css's own vocabulary — .panel/.panel__head/
// .panel__title/.panel__actions/.panel__body, .fieldset/.fieldset__legend/.fieldset__hint,
// .btn + variants, .chip/.chip__glyph/.chip__label, .pill, .stat, .code, .empty, .finding,
// .rank/.rank__row, .banner, .row/.col/.micro/.mono/.dim and [data-tone]. The scoped <style> at the
// bottom only covers what app.css genuinely lacks: this panel's three-column shell, the progress
// log, and two column overrides on .rank__row.
import { announce, clear, el, fmtNum, relTime, svgIcon } from "./util.js";

// ----------------------------------------------------------------- explain copy (§5)
const EXPLAIN = {
  title: "What is this panel for?",
  body: "Faultline derives its map, its hypotheses and its safety rails from a Docker-Compose file plus one small YAML of facts no parser can see: cluster membership, which service authorises another's requests, which store feeds which index. This panel generates that YAML for any repository you point it at — a GitHub URL, owner/name, or a path on this machine — which is what makes Faultline a tool you can aim at your own application instead of a demo wired to Eclipse Ditto.",
};

const PLACEHOLDER = "eclipse-ditto/ditto";
const MAX_SERVICE_CHIPS = 12;
const MAX_PROGRESS = 6;

const sig = (v) => { try { return JSON.stringify(v); } catch { return String(v); } };

/** An empty .chip__glyph makes app.css paint a per-tone glyph, so a chip is never colour alone. */
function chip(text, tone, icon, value) {
  return el("span", { class: "chip", "data-tone": tone || "neutral" },
    el("span", { class: "chip__glyph" }, icon ? svgIcon(icon, 12) : null),
    el("span", { class: "chip__label", text }),
    value ? el("span", { class: "chip__value mono", text: value }) : null);
}

function stat(label, value, tone, sub) {
  return el("div", { class: "stat" },
    el("span", { class: "stat__label micro", text: label }),
    el("span", { class: "stat__value mono num", "data-tone": tone || "neutral", text: value }),
    sub ? el("span", { class: "stat__sub", text: sub }) : null);
}

function hint(...kids) {
  return el("p", { class: "fieldset__hint" }, ...kids);
}

function banner(tone, icon, ...kids) {
  return el("p", { class: "banner", "data-tone": tone }, svgIcon(icon, 14), el("span", null, ...kids));
}

export function createOnboard(hostEl, { onJob } = {}) {
  // ---- local state. Only the operator writes these; update() never clobbers them.
  const S = { advanced: false, pending: false, error: null, explain: false, copied: false };
  let D = { state: {}, knowledge: null };
  const PROGRESS = [];           // last few onboarding log lines, newest last
  let seenSeq = 0;
  const SIGS = {};
  let copiedTimer = 0;

  // ---- skeleton: .panel > .panel__head + .explain__body + .panel__body (built once)
  hostEl.classList.add("panel", "fl-onb-root");
  clear(hostEl);
  hostEl.appendChild(el("style", { text: SCOPED_CSS }));

  const explainBody = el("div", { class: "explain__body", id: "fl-onb-explain", "data-open": "false" },
    el("div", { class: "explain__inner" },
      el("strong", { text: EXPLAIN.title }),
      el("p", { text: EXPLAIN.body })));

  const explainBtn = el("button", {
    type: "button", class: "explain__btn", "aria-expanded": "false", "aria-controls": "fl-onb-explain",
    title: EXPLAIN.title, "aria-label": "Explain: Repo onboarding",
    onclick: () => setExplain(explainBody.dataset.open !== "true"),
  }, svgIcon("info", 15));

  const headActions = el("div", { class: "panel__actions" }, explainBtn);
  const head = el("div", { class: "panel__head" },
    el("div", { class: "panel__title" },
      el("span", { class: "panel__icon" }, svgIcon("book", 15)),
      el("span", { text: "Repo onboarding" }),
      el("span", { class: "micro dim fl-onb-sub-title", text: "any compose app, not just Ditto" })),
    headActions);

  // ---- the form (left column). The input is created once so typing is never interrupted.
  const repoInput = el("input", {
    type: "text", id: "fl-onb-repo", name: "repo", placeholder: PLACEHOLDER, autocomplete: "off",
    spellcheck: "false", "aria-describedby": "fl-onb-repo-help", class: "mono",
    oninput: () => { S.error = null; render(); },
  });
  const composeInput = el("input", {
    type: "text", id: "fl-onb-compose", name: "compose", autocomplete: "off", spellcheck: "false",
    placeholder: "deployment/docker/docker-compose.yml", class: "mono",
  });
  const refInput = el("input", {
    type: "text", id: "fl-onb-ref", name: "ref", autocomplete: "off", spellcheck: "false",
    placeholder: "master", class: "mono",
  });

  const noLlmBtn = el("button", {
    type: "button", class: "btn btn--sm btn--ghost", "aria-pressed": "false",
    title: "Skip the language model and write a structural-only knowledge file",
    onclick: () => { S.noLlm = !S.noLlm; SIGS.adv = null; render(); },
  }, el("span", { class: "btn__icon" }, svgIcon("brain", 14)), el("span", { text: "Use the model" }));

  const advBody = el("div", { class: "fl-onb-adv", id: "fl-onb-adv", hidden: true },
    el("label", { class: "micro dim", for: "fl-onb-compose", text: "--compose · compose file inside the repo" }),
    composeInput,
    el("label", { class: "micro dim", for: "fl-onb-ref", text: "--ref · git branch or tag" }),
    refInput,
    el("div", { class: "row row--wrap" }, noLlmBtn),
    hint("Auto-detection prefers a deployment-specific compose file over a toy one at the repository root. Override it when it picks wrong."));

  const advBtn = el("button", {
    type: "button", class: "btn btn--sm btn--ghost", "aria-expanded": "false", "aria-controls": "fl-onb-adv",
    onclick: () => { S.advanced = !S.advanced; SIGS.adv = null; render(); },
  }, el("span", { class: "btn__icon" }, svgIcon("chevron", 14)), el("span", { text: "Advanced" }));

  const generateBtn = el("button", {
    type: "submit", class: "btn btn--primary btn--block",
  }, el("span", { class: "btn__icon" }, svgIcon("brain", 15)),
     el("span", { text: "Generate knowledge file" }));

  const whyBox = el("div", { class: "col fl-onb-why" });
  const progressBox = el("div", { class: "col fl-onb-progress" });

  const form = el("form", { class: "fieldset fl-onb-form", novalidate: true, onsubmit: submit },
    el("h3", { class: "fieldset__legend" }, svgIcon("search", 13),
      el("span", { class: "truncate", text: "Point it at a repository" })),
    el("label", { class: "micro dim", for: "fl-onb-repo", text: "Repository" }),
    repoInput,
    el("p", { class: "fieldset__hint", id: "fl-onb-repo-help" },
      "A GitHub URL, ", el("span", { class: "mono", text: "owner/name" }),
      ", or a path on this machine. Remote repositories are shallow-cloned once and cached."),
    el("div", { class: "row row--wrap" }, advBtn),
    advBody,
    generateBtn,
    whyBox,
    progressBox);

  const resultBox = el("section", { class: "fieldset fl-onb-result" });
  const filesBox = el("section", { class: "fieldset fl-onb-files" });

  const body = el("div", { class: "panel__body fl-onb-grid" }, form, resultBox, filesBox);
  hostEl.append(head, explainBody, body);

  function setExplain(open) {
    explainBody.dataset.open = String(open);
    explainBtn.setAttribute("aria-expanded", String(open));
    S.explain = open;
  }

  // ---------------------------------------------------------------- derived view
  function derive() {
    const state = D.state || {};
    const job = state.job || null;
    const busy = !!(job && job.status === "running");
    const mine = !!(job && job.kind === "onboard");
    const running = busy && mine;
    const onb = state.onboarding || null;
    const report = onb && onb.report ? onb.report : null;
    const repo = repoInput.value.trim();
    const reasons = [];
    if (!repo) reasons.push("type a repository reference first");
    if (running) reasons.push("this onboarding is still running");
    else if (busy) reasons.push(`a ${job.kind} job is already running — one at a time`);
    if (S.pending) reasons.push("submitting …");
    return { state, job, busy, running, onb, report, repo, reasons,
             knowledge: D.knowledge || null, llm: state.llm || {} };
  }

  // ---------------------------------------------------------------- actions
  function payload() {
    const repo = repoInput.value.trim();
    const compose = composeInput.value.trim();
    const ref = refInput.value.trim();
    return { repo, out: null, compose: compose || null, ref: ref || null,
             no_llm: !!S.noLlm, compare_to: null };
  }

  function submit(ev) {
    if (ev) ev.preventDefault();
    const V = derive();
    if (V.reasons.length) { S.error = V.reasons[0]; render(); return; }
    const body_ = payload();
    S.pending = true; S.error = null; PROGRESS.length = 0; render();
    announce(`Onboarding ${body_.repo}`);
    Promise.resolve(onJob && onJob("onboard", body_)).then(() => {
      S.pending = false; render();
    }).catch((err) => {
      S.pending = false;
      S.error = (err && (err.message || err.error)) || "could not start the onboarding job";
      announce(`Onboarding failed to start: ${S.error}`);
      render();
    });
  }

  async function copyPath(path, button) {
    try {
      await navigator.clipboard.writeText(path);
      S.copied = true;
      announce("Output path copied");
    } catch {
      S.copied = false;
      S.error = "the browser blocked clipboard access — select the path above instead";
    }
    button.classList.toggle("is-done", S.copied);
    clearTimeout(copiedTimer);
    copiedTimer = window.setTimeout(() => {
      S.copied = false;
      if (S.error && S.error.startsWith("the browser blocked")) S.error = null;
      SIGS.result = SIGS.form = null;
      render();
    }, 2500);
    SIGS.result = SIGS.form = null;
    render();
  }

  // ---------------------------------------------------------------- render: form state
  function renderForm(V) {
    const key = sig([V.reasons, S.pending, S.error, S.advanced, !!S.noLlm, V.running, V.llm.model, V.llm.available]);
    if (key === SIGS.form) return;
    SIGS.form = key;

    const blocked = V.reasons.length > 0;
    generateBtn.disabled = blocked;
    generateBtn.setAttribute("aria-disabled", String(blocked));
    generateBtn.title = blocked ? `Cannot generate: ${V.reasons[0]}` : "Clone, read and document this repository";
    generateBtn.lastChild.textContent = V.running ? "Onboarding …"
      : S.pending ? "Starting …" : "Generate knowledge file";
    clear(generateBtn.firstChild).appendChild(svgIcon(V.running || S.pending ? "refresh" : "brain", 15));

    advBtn.setAttribute("aria-expanded", String(S.advanced));
    advBody.hidden = !S.advanced;
    noLlmBtn.setAttribute("aria-pressed", String(!S.noLlm));
    noLlmBtn.setAttribute("data-tone", S.noLlm ? "neutral" : "violet");
    noLlmBtn.lastChild.textContent = S.noLlm ? "Structural only (--no-llm)" : "Use the model";

    clear(whyBox);
    if (S.error) whyBox.appendChild(banner("danger", "alert", S.error));
    else if (blocked && V.reasons[0] !== "type a repository reference first") {
      whyBox.appendChild(banner("warn", "alert", V.reasons[0]));
    } else if (blocked) {
      whyBox.appendChild(hint("Enter a repository above, for example ",
        el("span", { class: "mono", text: PLACEHOLDER }), "."));
    } else {
      whyBox.appendChild(hint("One bounded ",
        V.llm.available ? el("span", { class: "mono", text: V.llm.model || "model" }) : "model",
        " call, validated in code, then ",
        el("span", { class: "mono", text: "config/<slug>_knowledge.yaml" }), " is written."));
    }
  }

  // ---------------------------------------------------------------- render: live progress
  function renderProgress(V) {
    const key = sig([V.running, V.job && V.job.id, V.job && V.job.status, PROGRESS.length,
                     PROGRESS[PROGRESS.length - 1], V.running && Math.round(elapsed(V))]);
    if (key === SIGS.progress) return;
    SIGS.progress = key;
    clear(progressBox);
    if (!V.running) return;
    progressBox.appendChild(banner("cyan", "activity",
      el("span", { text: `${V.job.label} · ` }),
      el("span", { class: "mono", text: `+${fmtNum(elapsed(V), 0)}s` })));
    const steps = el("ol", { class: "fl-onb-steps" });
    for (const line of PROGRESS.slice(-MAX_PROGRESS)) {
      steps.appendChild(el("li", { class: "micro mono dim", text: line }));
    }
    if (PROGRESS.length) progressBox.appendChild(steps);
    progressBox.appendChild(hint("Every step is also narrated in the agent activity feed."));
  }

  function elapsed(V) {
    const now = V.state.now || Date.now() / 1000;
    return Math.max(0, now - ((V.job && V.job.started_at) || now));
  }

  // ---------------------------------------------------------------- render: the report
  function renderResult(V) {
    const key = sig([V.onb && V.onb.at, V.report && V.report.output_path, S.copied, V.running]);
    if (key === SIGS.result) return;
    SIGS.result = key;
    clear(resultBox);

    resultBox.appendChild(el("h3", { class: "fieldset__legend" }, svgIcon("flask", 13),
      el("span", { class: "truncate", text: "What the agent found" }),
      el("span", { class: "spacer" }),
      // llm.describe() is "provider:model"; the provider is already a pill in the header
      V.report ? chip(V.report.generated_by === "llm"
                        ? `documented by ${String(V.report.model || "the model").split(":").pop()}`
                        : "structural only",
        V.report.generated_by === "llm" ? "violet" : "neutral",
        V.report.generated_by === "llm" ? "brain" : "grid") : null));

    if (!V.report) {
      resultBox.appendChild(el("div", { class: "empty" },
        svgIcon("book", 26, { class: "empty__icon" }),
        el("p", { class: "empty__title", text: "No repository onboarded yet" }),
        el("p", { class: "empty__hint",
          text: "Generate a knowledge file and this is where the services, the documented edges and the claims validation refused will appear." })));
      return;
    }

    const r = V.report;
    const k = r.knowledge || {};
    const edges = k.documented_edges || [];
    const roles = k.roles || {};
    const rejected = r.rejected || [];
    const services = r.services || [];

    resultBox.appendChild(el("div", { class: "row row--wrap fl-onb-head" },
      chip(r.slug, "cyan", "book"),
      chip("compose", "neutral", "list", shortPath(r.compose_path)),
      V.onb.at ? el("span", { class: "micro dim", text: relTime(V.onb.at) }) : null));

    resultBox.appendChild(el("div", { class: "fl-onb-stats" },
      stat("Services", String(services.length), "cyan", "parsed from compose"),
      stat("Structural edges", String(r.structural_edges || 0), "neutral", "depends_on, env, nginx"),
      stat("Documented edges", String(edges.length), edges.length ? "violet" : "neutral", "added by the agent"),
      stat("Refused", String(rejected.length), rejected.length ? "warn" : "ok", "claims code rejected"),
      stat("Health port", k.health_port == null ? "—" : String(k.health_port), "neutral", "shared management port"),
      stat("Cluster alias", k.cluster_alias || "—", "neutral", "shared network alias")));

    // services
    const svcRow = el("div", { class: "row row--wrap" },
      ...services.slice(0, MAX_SERVICE_CHIPS).map((n) => chip(n, "neutral", "server")));
    if (services.length > MAX_SERVICE_CHIPS) {
      svcRow.appendChild(el("span", { class: "micro dim",
        text: `+${services.length - MAX_SERVICE_CHIPS} more` }));
    }
    resultBox.appendChild(sub("Services found", "server", svcRow));

    // documented edges, each with its evidence
    if (edges.length) {
      resultBox.appendChild(sub(`Documented edges (${edges.length})`, "link",
        ...edges.map((e) => el("div", { class: "finding", "data-tone": "violet" },
          el("div", { class: "finding__head" },
            chip("documented", "violet", "link"),
            el("span", { class: "finding__title mono", text: `${e.src} → ${e.dst}` })),
          el("p", { class: "finding__evidence", text: e.evidence || "documented" })))));
    } else {
      resultBox.appendChild(sub("Documented edges", "link",
        hint("None survived validation — the structural parsers already saw every relationship the documentation asserts.")));
    }

    // role overrides
    if (Object.keys(roles).length) {
      resultBox.appendChild(sub("Role overrides", "crosshair",
        el("div", { class: "row row--wrap" },
          ...Object.entries(roles).map(([svc, role]) => chip(`${svc} → ${role}`, "cyan", "crosshair"))),
        hint("Roles decide what may be broken: only app, proxy and datastore services are chaos-relevant.")));
    }

    // the "code disposed" moments — a virtue, not an error
    if (rejected.length) {
      resultBox.appendChild(sub(`Code disposed of ${rejected.length} model claim${rejected.length === 1 ? "" : "s"}`,
        "shield",
        hint("The model proposes; code decides what is admissible. Every line below was refused before anything reached disk — an invented service, a role that would have quietly exempted a product service from testing, a port out of range."),
        ...rejected.map((text) => el("div", { class: "finding", "data-tone": "warn" },
          el("div", { class: "finding__head" },
            chip("refused", "warn", "shield"),
            el("span", { class: "finding__title fl-onb-refused", text }))))));
    }

    if ((r.notes || []).length) {
      resultBox.appendChild(sub("Notes", "info",
        ...r.notes.map((n) => banner("warn", "info", n))));
    }

    if (V.onb.comparison) {
      const c = V.onb.comparison;
      const cov = c.final_graph_coverage || {};
      const ed = c.edges || {};
      resultBox.appendChild(sub("Against the hand-authored file", "gauge",
        el("div", { class: "row row--wrap" },
          chip("coverage", "cyan", "check", String(cov.coverage)),
          chip("edge f1", "violet", "link", String(ed.f1)),
          chip("role accuracy", "neutral", "crosshair", String((c.roles || {}).accuracy)))));
    }

    // output path + copy
    const copyBtn = el("button", {
      type: "button", class: "btn btn--sm btn--ghost",
      title: "Copy the output path to the clipboard",
      onclick: (ev) => copyPath(r.output_path, ev.currentTarget),
    }, el("span", { class: "btn__icon" }, svgIcon(S.copied ? "check" : "copy", 14)),
       el("span", { text: S.copied ? "Copied" : "Copy path" }));
    // a plain span, not <pre>: .code already wraps, and a <pre> would scroll sideways on a phone
    resultBox.appendChild(sub("Written to", "terminal",
      el("div", { class: "code" }, el("span", { class: "mono", text: r.output_path })),
      el("div", { class: "btn-row fl-onb-copyrow" }, copyBtn),
      hint("Point the mapper at it with ",
        el("span", { class: "mono", text: `faultline topology --knowledge ${shortPath(r.output_path)}` }),
        ", or set ", el("span", { class: "mono", text: "knowledge_file" }), " in config/settings.yaml.")));
  }

  /** One titled block inside a fieldset: a small heading plus its content. */
  function sub(title, icon, ...kids) {
    return el("div", { class: "fl-onb-sub" },
      el("h4", { class: "micro dim fl-onb-subtitle" }, svgIcon(icon, 12), el("span", { text: title })),
      ...kids);
  }

  function shortPath(p) {
    const parts = String(p || "").split("/");
    return parts.length > 3 ? parts.slice(-3).join("/") : String(p || "—");
  }

  // ---------------------------------------------------------------- render: files on disk
  function renderFiles(V) {
    const key = sig(V.knowledge);
    if (key === SIGS.files) return;
    SIGS.files = key;
    clear(filesBox);
    const kn = V.knowledge;
    const files = (kn && kn.files) || [];

    filesBox.appendChild(el("h3", { class: "fieldset__legend" }, svgIcon("database", 13),
      el("span", { class: "truncate", text: "Knowledge files on disk" }),
      el("span", { class: "spacer" }),
      chip(String(files.length), "neutral", "list")));

    if (!files.length) {
      filesBox.appendChild(el("div", { class: "empty" },
        svgIcon("database", 26, { class: "empty__icon" }),
        el("p", { class: "empty__title", text: kn ? "No knowledge files yet" : "Loading …" }),
        el("p", { class: "empty__hint", text: kn
          ? "Onboard a repository and its documented knowledge lands next to the Ditto one."
          : "Reading the config directory." })));
      return;
    }

    const rank = el("div", { class: "rank" });
    for (const f of files) {
      const bits = [`${f.documented_edges} documented edge${f.documented_edges === 1 ? "" : "s"}`];
      const roleCount = Object.keys(f.roles || {}).length;
      if (roleCount) bits.push(`${roleCount} role override${roleCount === 1 ? "" : "s"}`);
      if (f.health_port) bits.push(`health_port ${f.health_port}`);
      if (f.cluster_alias) bits.push(`alias ${f.cluster_alias}`);
      bits.push(`${(f.services_referenced || []).length} services referenced`);
      if (f.mtime) bits.push(relTime(f.mtime));
      rank.appendChild(el("div", {
        class: "rank__row", "data-tone": f.is_active ? "ok" : "neutral", title: f.path,
      },
        el("span", { class: "rank__name", text: f.slug }),
        el("span", { class: "rank__score", text: String(f.documented_edges),
          title: "documented edges in this file" }),
        el("span", { class: "rank__grade" },
          f.is_active ? chip("active", "ok", "check") : chip("on disk", "neutral", "list")),
        el("span", { class: "rank__bar micro dim", text: bits.join(" · ") })));
    }
    filesBox.appendChild(rank);
    filesBox.appendChild(hint("“Active” is the file ",
      el("span", { class: "mono", text: "knowledge_file" }),
      " in config/settings.yaml points at — the one the live application map is layered with."));
    for (const n of (kn.notes || [])) filesBox.appendChild(banner("warn", "alert", n));
  }

  // ---------------------------------------------------------------- render
  function render() {
    const V = derive();
    renderForm(V);
    renderProgress(V);
    renderResult(V);
    renderFiles(V);
  }

  /** Keep the last few onboarding log lines so the panel shows progress, not just a spinner. */
  function absorbEvents(state) {
    for (const ev of (state && state.onboarding_events) || []) {
      if (!ev || ev.source !== "onboarding" || ev.seq <= seenSeq) continue;
      seenSeq = ev.seq;
      PROGRESS.push(String(ev.text || ""));
      if (PROGRESS.length > 40) PROGRESS.shift();
    }
  }

  const onExplainAll = (e) => setExplain(!!(e.detail && e.detail.open));
  document.addEventListener("fl-explain", onExplainAll);

  render();

  return {
    update(state, knowledge) {
      D = {
        state: state || {},
        knowledge: knowledge !== undefined && knowledge !== null ? knowledge : D.knowledge,
      };
      absorbEvents(D.state);
      render();
    },
    destroy() {
      document.removeEventListener("fl-explain", onExplainAll);
      clearTimeout(copiedTimer);
      clear(hostEl);
      hostEl.classList.remove("panel", "fl-onb-root");
    },
  };
}

// ---------------------------------------------------------------------------
// Scoped styling — injected unconditionally, deliberately small. Anything with a matching app.css
// class uses that class instead; what is left here is only what the design system does not define:
// this panel's responsive three-column shell, the stat strip, the progress log, the small block
// headings, and two column overrides on .rank__row (whose 1fr/64px/44px grid is tuned for scores).
// No literal colour anywhere — every value is a tokens.css custom property.
const SCOPED_CSS = `
.fl-onb-root [hidden]{display:none}
.fl-onb-root{container-type:inline-size}
/* one titled block: a small heading and its content, stacked */
.fl-onb-root .fl-onb-sub{display:flex;flex-direction:column;gap:var(--s2);min-width:0;
  margin-top:var(--s3);overflow-wrap:anywhere}
.fl-onb-root .fl-onb-subtitle{display:flex;align-items:center;gap:var(--s2);min-width:0}
.fl-onb-root .fl-onb-subtitle .icon{color:var(--text-mute);flex:none}

/* the panel shell: form | result | files, collapsing to one column on a narrow container */
.fl-onb-root .fl-onb-grid{display:grid;gap:var(--s4);grid-template-columns:minmax(0,1fr);align-items:start}
.fl-onb-root .fl-onb-form,
.fl-onb-root .fl-onb-result,
.fl-onb-root .fl-onb-files{min-width:0}
.fl-onb-root .fl-onb-form{gap:var(--s2)}
.fl-onb-root .fl-onb-form > label{padding-top:var(--s1)}
@container (min-width:720px){
  .fl-onb-root .fl-onb-grid{grid-template-columns:minmax(240px,300px) minmax(0,1fr)}
  .fl-onb-root .fl-onb-files{grid-column:1 / -1;border-top:1px solid var(--border);padding-top:var(--s3)}
  /* a long report scrolls inside its column, the way every other panel body in the room does */
  .fl-onb-root .fl-onb-result,
  .fl-onb-root .fl-onb-files{max-height:clamp(380px,58vh,640px);overflow:auto;overscroll-behavior:contain}
  .fl-onb-root .fl-onb-result > .fieldset__legend,
  .fl-onb-root .fl-onb-files > .fieldset__legend{position:sticky;top:0;z-index:1;
    background:var(--surface);margin-top:0}
}
@container (min-width:1180px){
  .fl-onb-root .fl-onb-grid{grid-template-columns:minmax(240px,300px) minmax(0,1.5fr) minmax(260px,0.95fr)}
  .fl-onb-root .fl-onb-files{grid-column:auto;border-top:0;padding-top:0}
}

/* advanced disclosure: a plain stack, since app.css has no form-row vocabulary */
.fl-onb-root .fl-onb-adv{display:flex;flex-direction:column;gap:var(--s1);padding:var(--s2) var(--s3);
  border:1px solid var(--border);border-radius:var(--r1);background:var(--surface-2)}
.fl-onb-root .fl-onb-adv > label{padding-top:var(--s1)}

/* result header + the stat strip (.stat exists, a responsive strip of them does not) */
.fl-onb-root .fl-onb-head{row-gap:var(--s1)}
/* in "compose <path>" it is the path that should shrink, never the word it is labelled with */
.fl-onb-root .fl-onb-head .chip__label{flex:none}
.fl-onb-root .fl-onb-stats{display:grid;gap:var(--s2) var(--s3);margin-top:var(--s3);
  grid-template-columns:repeat(auto-fit,minmax(128px,1fr))}
.fl-onb-root .fl-onb-stats .stat__value{font-size:16px}

/* live progress log */
.fl-onb-root .fl-onb-steps{display:flex;flex-direction:column;gap:var(--s1);margin:0;padding:0;list-style:none}
/* log lines, not labels: .micro's caps are wrong for a sentence the agent just printed */
.fl-onb-root .fl-onb-steps > li{padding-left:var(--s3);border-left:2px solid var(--tone-cyan-border);
  line-height:1.5;overflow-wrap:anywhere;text-transform:none;letter-spacing:0}
.fl-onb-root .fl-onb-why,
.fl-onb-root .fl-onb-progress{gap:var(--s2)}
.fl-onb-root .banner{align-items:flex-start;font-size:12px;line-height:1.5;overflow-wrap:anywhere}
.fl-onb-root .banner > .icon{margin-top:2px;flex:none}

/* app.css ships .btn--sm at 30px; the touch-target audit wants 32px or more */
.fl-onb-root .btn--sm{min-height:32px}
.fl-onb-root .fieldset__legend{flex-wrap:wrap;row-gap:var(--s1)}
.fl-onb-root .fieldset__legend .explain__btn{width:34px;height:34px}
.fl-onb-root .empty{min-height:0;padding:var(--s5) var(--s3)}
.fl-onb-root .code{max-width:100%;padding-right:var(--s3)}
/* a rejected claim is a whole sentence, not a label: let it wrap like body copy */
.fl-onb-root .fl-onb-refused{font-weight:600;line-height:1.5;overflow-wrap:anywhere}
.fl-onb-root .fl-onb-copyrow > .btn{flex:0 0 auto}
.fl-onb-root .chip{max-width:100%}
.fl-onb-root .chip__value{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
/* the panel title's subtitle is the first thing to go when the head gets tight */
@container (max-width:560px){.fl-onb-root .fl-onb-sub-title{display:none}}

/* file list: .rank__row's 1fr/64px/44px grid is tuned for numeric scores + a grade */
.fl-onb-root .rank__row{grid-template-columns:minmax(0,1fr) auto auto;gap:var(--s1) var(--s2);
  padding:var(--s2) 0}
.fl-onb-root .rank__row[data-tone="ok"] .rank__name{color:var(--text)}
.fl-onb-root .rank__score{width:auto;text-align:right}
/* the per-file meta line is a sentence of facts, not a label: .micro's caps would shout it */
.fl-onb-root .rank__bar{line-height:1.5;overflow-wrap:anywhere;text-transform:none;letter-spacing:0}
`;
