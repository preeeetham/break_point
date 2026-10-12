// fixes.js — Fix approvals: "Can I fix this?" requests that wait for a human.
// Contract: docs/UI_CONTRACT.md §2 (GET /api/proposals[/<id>], POST /api/proposals/<id>/approve|reject|apply,
// state.proposals), §3 (factory + update/destroy), §5 (explain), §6 (a11y). Owns its subtree inside hostEl
// (panel chrome included, like onboard.js) and writes nowhere else.
//
// The rule this panel exists to make visible: nothing is applied without a human approving the exact
// change shown. Approve sends back the change_hash this card rendered, so a change that was edited after
// it was shown is refused by the server (409) instead of applied. Proposals expire; nothing is ever
// approved by default.
//
// PRESENTATION: app.css vocabulary (.panel/.panel__head/.panel__body, .btn + variants, .chip, .banner,
// .check, .empty, .exp-sec__title, .micro/.mono/.dim, [data-tone]). The scoped <style> at the bottom only
// covers what app.css lacks (card shell, diff lines, verification table) and uses tokens.css vars only.
import { announce, clear, el, getJSON, postJSON, relTime, shortId, svgIcon } from "./util.js";
import { EXPLAIN } from "./copy.js";

const FALLBACK_EXPLAIN = {
  title: "Human-approved fixes",
  body: "After an experiment finds a problem, Faultline can propose one fix from a fixed, code-defined catalog. Each card says what is wrong, shows the exact change verbatim, what should improve, the risks, and how it is rolled back. Nothing is applied until a person approves that exact change; the approval quotes the change's hash, so an edited change cannot ride on an old approval. Unanswered proposals expire and are never approved automatically.",
};

// status -> [visible text, tone, icon]. Text is always shown: tone is never the only signal.
const STATUS = {
  pending: ["Waiting for your decision", "warn", "clock"],
  approved: ["Approved, not applied yet", "cyan", "check"],
  applying: ["Applying the change", "chaos", "refresh"],
  applied: ["Applied, verifying", "cyan", "refresh"],
  verified: ["Applied and verified", "ok", "check"],
  verification_failed: ["Verification failed", "danger", "x"],
  rolled_back: ["Rolled back", "warn", "refresh"],
  rejected: ["Rejected, nothing applied", "neutral", "x"],
  expired: ["Expired, nothing applied", "neutral", "clock"],
};
const IN_FLIGHT = new Set(["approved", "applying", "applied"]);
const VERDICT = {
  better: ["better", "ok", "trend"], same: ["no change", "neutral", "info"],
  worse: ["worse", "danger", "trendDown"], unknown: ["unknown", "neutral", "info"],
};
const PARTS = [
  ["problem", "1. What is wrong"], ["change", "2. Exactly what will change"],
  ["expected_improvement", "3. Expected improvement"], ["risks", "4. Risks"], ["rollback", "5. Rollback"],
];

const sig = (v) => { try { return JSON.stringify(v); } catch { return String(v); } };

function chip(text, tone, icon, value) {
  return el("span", { class: "chip", "data-tone": tone || "neutral" },
    el("span", { class: "chip__glyph" }, icon ? svgIcon(icon, 12) : null),
    el("span", { class: "chip__label", text }),
    value ? el("span", { class: "chip__value mono", text: value }) : null);
}

function fmtLeft(s) {
  if (s <= 0) return null;
  if (s < 60) return `${Math.floor(s)} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min ${Math.floor(s % 60)} s`;
  return `${Math.floor(m / 60)} h ${m % 60} min`;
}

function metricWords(m) {
  return String(m || "").replace(/_/g, " ");
}

function fmtVal(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return Number.isInteger(v) ? String(v) : Number(v).toFixed(Math.abs(v) < 1 ? 3 : 1);
}

/** One line of the verbatim change. The +/- characters stay as typed; the screen-reader label
 *  is not selectable, so copying the block still yields the exact preview. */
function diffLine(line) {
  const kind = line.startsWith("+++") || line.startsWith("---") ? "file"
    : line.startsWith("@@") ? "hunk" : line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : "ctx";
  const label = { add: "added: ", del: "removed: ", hunk: "", file: "", ctx: "" }[kind];
  return el("span", { class: "fxa-diff__line", "data-kind": kind },
    label ? el("span", { class: "sr-only fxa-nosel", text: label }) : null,
    line || " ");
}

export function createFixApprovals(hostEl, { toast, onChanged } = {}) {
  const ex = (EXPLAIN && EXPLAIN.fixApprovals) || FALLBACK_EXPLAIN;
  // per-proposal operator input and request state; update() never clobbers these
  const L = new Map();       // id -> {reason, allow, busy, error, result}
  const details = new Map(); // id -> {status, data|null}
  const cards = new Map();   // id -> {node, sig, countdown, summary}
  let D = { state: {}, items: [] };
  let clockOffsetMs = 0;
  let lastSweep = 0;
  let lastAnnounced = "";
  let decidedOpen = false;
  const autoOpened = new Set();   // decided cards this operator acted on: shown once, not hidden away
  const local = (id) => { if (!L.has(id)) L.set(id, { reason: "", allow: false, busy: false, error: "", result: null }); return L.get(id); };
  const say = (title, text, tone) => { if (typeof toast === "function") { try { toast(title, text, tone); } catch { /* optional */ } } };

  // ---- skeleton: .panel > .panel__head + .explain__body + .panel__body
  hostEl.classList.add("panel", "fxa-root");
  hostEl.setAttribute("role", "region");
  hostEl.setAttribute("aria-labelledby", "fxa-title");
  clear(hostEl);
  hostEl.appendChild(el("style", { text: SCOPED_CSS }));

  const explainBody = el("div", { class: "explain__body", id: "fxa-explain", "data-open": "false" },
    el("div", { class: "explain__inner" }, el("strong", { text: ex.title }), el("p", { text: ex.body })));
  const explainBtn = el("button", {
    type: "button", class: "explain__btn", "aria-expanded": "false", "aria-controls": "fxa-explain",
    title: "What is this?", "aria-label": "What is Fix approvals?",
    onclick: () => setExplain(explainBody.dataset.open !== "true"),
  }, svgIcon("info", 15));
  const countBox = el("span", { class: "fxa-count" });
  hostEl.appendChild(el("div", { class: "panel__head" },
    el("div", { class: "panel__title" },
      el("span", { class: "panel__icon" }, svgIcon("wrench", 15)),
      el("h2", { class: "fxa-h2", id: "fxa-title", text: "Fix approvals" }),
      countBox),
    el("div", { class: "panel__actions" }, explainBtn)));
  hostEl.appendChild(explainBody);
  const body = el("div", { class: "panel__body panel__body--tight fxa-body" });
  hostEl.appendChild(body);

  const intro = el("p", { class: "banner", "data-tone": "cyan" }, svgIcon("shield", 14),
    el("span", { text: "Nothing is applied without a human approving the exact change shown. Approving quotes that change's hash; proposals expire and are never approved automatically." }));
  const waitingSec = el("section", { class: "fxa-sec", "aria-labelledby": "fxa-waiting-h" });
  const flightSec = el("section", { class: "fxa-sec", "aria-labelledby": "fxa-flight-h" });
  const decided = el("details", { class: "fxa-decided", ontoggle: () => { decidedOpen = decided.open; } });
  const emptyBox = el("div");
  body.append(intro, emptyBox, waitingSec, flightSec, decided);

  function setExplain(open) {
    explainBody.dataset.open = String(open);
    explainBtn.setAttribute("aria-expanded", String(open));
    explainBtn.classList.toggle("is-open", open);
  }
  const onExplainAll = (e) => setExplain(!!(e.detail && e.detail.open));
  document.addEventListener("fl-explain", onExplainAll);

  // ---------------------------------------------------------------- data
  function secondsLeft(p) {
    const t = Date.parse(p.expires_at);
    if (Number.isNaN(t)) return p.seconds_left ?? 0;
    return (t - (Date.now() + clockOffsetMs)) / 1000;
  }

  async function loadDetail(p) {
    const have = details.get(p.id);
    if (have && have.status === p.status) return;
    details.set(p.id, { status: p.status, data: have ? have.data : null });
    try {
      const data = await getJSON(`/api/proposals/${encodeURIComponent(p.id)}`);
      details.set(p.id, { status: data.status, data });
      render(true);
    } catch {
      details.delete(p.id);   // retried on a later poll
    }
  }

  // An overdue pending proposal is only marked expired on disk by the list call (or a decision): ask
  // for it at most every 5 s so the card moves to "Expired" without anyone having to click.
  function sweepIfOverdue(items) {
    const overdue = items.some((p) => (p.status === "pending" || p.status === "approved") && secondsLeft(p) <= 0);
    if (!overdue || Date.now() - lastSweep < 5000) return;
    lastSweep = Date.now();
    getJSON("/api/proposals").then(() => { if (typeof onChanged === "function") onChanged(); }).catch(() => {});
  }

  // ---------------------------------------------------------------- actions
  async function decide(p, action) {
    const st = local(p.id);
    if (st.busy) return;
    st.busy = true; st.error = ""; st.result = null;
    render(true);
    try {
      if (action === "approve") {
        // exactly the hash this card rendered, never a freshly fetched one
        const res = await postJSON(`/api/proposals/${encodeURIComponent(p.id)}/approve`,
          { change_hash: p.change_hash, reason: st.reason || "", allow_protected: Boolean(st.allow) });
        const a = res.apply || {};
        st.result = a.started
          ? "Approved. The apply job has started; follow it in the agent activity feed."
          : `Approved, but not applied yet: ${a.reason || "the job slot is busy"}.`;
        announce(st.result);
        say("Fix approved", a.started ? "Applying the change" : "Apply not started", "ok");
      } else if (action === "reject") {
        await postJSON(`/api/proposals/${encodeURIComponent(p.id)}/reject`, { reason: st.reason || "" });
        st.result = "Rejected. Nothing will be applied.";
        announce(st.result);
        say("Fix rejected", "Nothing will be applied", "warn");
      } else {
        await postJSON(`/api/proposals/${encodeURIComponent(p.id)}/apply`, { allow_protected: Boolean(st.allow) });
        st.result = "The apply job has started; follow it in the agent activity feed.";
        announce(st.result);
      }
      st.reason = "";
    } catch (err) {
      st.error = err && err.message ? err.message : String(err);
      announce(`Not done: ${st.error}`);
    } finally {
      st.busy = false;
      details.delete(p.id);
      render(true);
      if (typeof onChanged === "function") onChanged();
    }
  }

  // ---------------------------------------------------------------- rendering
  function statusChip(p) {
    const [text, tone, icon] = STATUS[p.status] || [p.status, "neutral", "info"];
    return chip(text, tone, icon);
  }

  function partSection(p, key, title) {
    const id = `fxa-${p.id}-${key}`;
    const kids = [el("h4", { class: "exp-sec__title", id, text: title }),
      el("p", { class: "fxa-text", text: (p.explanation || {})[key] || "—" })];
    if (key === "change") {
      const lines = String(p.change_preview || "").replace(/\n$/, "").split("\n");
      kids.push(el("pre", { class: "fxa-diff mono", tabindex: "0", "aria-labelledby": id,
        "aria-describedby": `fxa-${p.id}-legend` }, el("code", null, ...lines.map(diffLine))));
      kids.push(el("div", { class: "fxa-legendrow" },
        el("p", { class: "micro dim fxa-legend", id: `fxa-${p.id}-legend`,
          text: "Verbatim, generated by code. Lines starting with + are added, lines starting with - are removed." }),
        el("button", {
          type: "button", class: "btn btn--ghost btn--sm", title: "Copy the exact change",
          "aria-label": `Copy the exact change of proposal ${p.id}`,
          onclick: async () => {
            try { await navigator.clipboard.writeText(p.change_preview); announce("Change copied"); }
            catch { announce("Copy blocked by the browser"); }
          },
        }, el("span", { class: "btn__icon" }, svgIcon("copy", 13)), el("span", { text: "Copy" }))));
    }
    if (key === "expected_improvement") {
      const d = (details.get(p.id) || {}).data;
      const ms = (d && d.expected_metrics) || [];
      if (ms.length) {
        kids.push(el("ul", { class: "fxa-list" }, ...ms.map((m) => el("li", null,
          el("span", { class: "mono", text: metricWords(m.metric) }),
          el("span", { class: "dim", text: `: now ${fmtVal(m.before)}, expected ${m.expected_after}` })))));
      }
    }
    return el("section", { class: "fxa-part", "aria-labelledby": id }, ...kids);
  }

  function hashRow(p) {
    return el("p", { class: "fxa-hash" },
      el("span", { class: "micro", text: "Change hash" }),
      el("code", { class: "mono", title: `sha256 ${p.change_hash}`, text: String(p.change_hash || "").slice(0, 12) }),
      el("span", { class: "dim fxa-small", text: "first 12 characters; hover for the full sha256" }));
  }

  function protectedBanner(p) {
    if (!p.requires_allow_protected) return null;
    return el("p", { class: "banner", "data-tone": "danger", role: "note" }, svgIcon("lock", 14),
      el("span", null, el("strong", { text: "Protected service. " }),
        el("span", { text: `This change touches ${(p.target_services || []).join(", ") || "a protected service"}, which the safety policy protects. Applying it needs the explicit permission below.` })));
  }

  function decisionForm(p) {
    const st = local(p.id);
    const reasonId = `fxa-${p.id}-reason`;
    const allowId = `fxa-${p.id}-allow`;
    const left = secondsLeft(p);
    const overdue = left <= 0;
    const reason = el("input", {
      type: "text", id: reasonId, class: "fxa-reason", autocomplete: "off", maxlength: "500",
      placeholder: "e.g. not during the demo", value: st.reason,
      oninput: (e) => { st.reason = e.target.value; },
    });
    const allow = p.requires_allow_protected ? el("label", { class: "check", for: allowId },
      el("input", { type: "checkbox", id: allowId, checked: st.allow,
        onchange: (e) => { st.allow = e.target.checked; } }),
      el("span", { text: "Allow changing a protected service when this is applied" })) : null;
    const hash12 = String(p.change_hash || "").slice(0, 12);
    // Enter in the reason field must never approve anything: no implicit submit, both buttons explicit
    return el("form", {
      class: "fxa-form", "aria-label": `Decide on proposal ${p.id}`, novalidate: true,
      onsubmit: (e) => { e.preventDefault(); },
    },
      el("label", { class: "micro dim", for: reasonId, text: "Reason (optional, kept in the audit trail)" }),
      reason,
      allow,
      el("div", { class: "fxa-actions" },
        el("button", {
          type: "button", class: "btn btn--primary fxa-btn", disabled: st.busy || overdue,
          "aria-label": `Approve proposal ${p.id}, change ${hash12}`, onclick: () => decide(p, "approve"),
        }, el("span", { class: "btn__icon" }, svgIcon("check", 15)),
           el("span", { text: st.busy ? "Sending…" : `Approve change ${hash12}` })),
        el("button", {
          type: "button", class: "btn btn--danger fxa-btn", disabled: st.busy || overdue,
          "aria-label": `Reject proposal ${p.id}`, onclick: () => decide(p, "reject"),
        }, el("span", { class: "btn__icon" }, svgIcon("x", 15)), el("span", { text: "Reject" }))),
      overdue ? el("p", { class: "micro dim", text: "This proposal has passed its deadline and can no longer be approved." }) : null);
  }

  function outcome(p) {
    const st = local(p.id);
    const out = [];
    if (p.decided_by) {
      const verb = p.status === "rejected" ? "Rejected" : p.status === "expired" ? "Expired" : "Approved";
      out.push(el("p", { class: "fxa-text" },
        el("strong", { text: `${verb} by ${p.decided_by}` }),
        el("span", { class: "dim", text: ` · ${relTime(p.decided_at)}` }),
        p.reason ? el("span", { text: ` — “${p.reason}”` }) : null));
    } else if (p.status === "expired") {
      out.push(el("p", { class: "fxa-text", text: "Nobody decided before the deadline. Nothing was applied." }));
    }
    const job = (D.state && D.state.job) || null;
    const mine = job && job.kind === "fix" && String(job.label || "").includes(p.id);
    if (mine && job.status === "running") {
      out.push(el("p", { class: "banner", "data-tone": "chaos" }, svgIcon("refresh", 14),
        el("span", { text: "Applying now. The stack is health-checked afterwards, the experiment re-runs, and the change is rolled back if it does not help." })));
    } else if (mine && job.status === "failed") {
      out.push(el("p", { class: "banner", "data-tone": "danger", role: "alert" }, svgIcon("alert", 14),
        el("span", { text: `The apply did not go ahead: ${job.error || "see the agent activity feed"}` })));
    }
    if (p.status === "approved" && !(mine && job.status === "running")) {
      out.push(el("div", { class: "fxa-actions" },
        p.requires_allow_protected ? el("label", { class: "check", for: `fxa-${p.id}-allow2` },
          el("input", { type: "checkbox", id: `fxa-${p.id}-allow2`, checked: st.allow,
            onchange: (e) => { st.allow = e.target.checked; } }),
          el("span", { text: "Allow changing a protected service" })) : null,
        el("button", {
          type: "button", class: "btn btn--primary fxa-btn", disabled: st.busy,
          "aria-label": `Apply approved proposal ${p.id} now`, onclick: () => decide(p, "apply"),
        }, el("span", { class: "btn__icon" }, svgIcon("play", 15)), el("span", { text: "Apply now" }))));
    }
    return out;
  }

  function verificationTable(p) {
    const v = p.verification;
    if (!v) return null;
    const id = `fxa-${p.id}-verify`;
    const [otext, otone, oicon] = v.outcome === "verified" ? ["verified", "ok", "check"]
      : v.outcome === "verification_failed" ? ["verification failed", "danger", "x"] : ["not run", "neutral", "info"];
    const rows = (v.comparisons || []).map((c) => {
      const [vt, tone, icon] = VERDICT[c.verdict] || [c.verdict, "neutral", "info"];
      return el("tr", { class: c.metric === v.targeted_metric ? "is-target" : null },
        el("th", { scope: "row", class: "mono" }, metricWords(c.metric),
          c.metric === v.targeted_metric ? el("span", { class: "dim", text: " (targeted)" }) : null),
        el("td", { class: "mono num", text: fmtVal(c.before) }),
        el("td", { class: "mono num", text: fmtVal(c.after) }),
        el("td", null, chip(vt, tone, icon, c.better_is === "lower" ? "lower is better" : "higher is better")));
    });
    return el("section", { class: "fxa-part", "aria-labelledby": id },
      el("h4", { class: "exp-sec__title", id }, svgIcon("activity", 13), el("span", { text: "Verification: before and after" })),
      el("p", { class: "row row--wrap" }, chip(otext, otone, oicon),
        v.rerun_experiment_id ? el("span", { class: "micro dim", text: `re-run ${shortId(v.rerun_experiment_id)}` }) : null),
      v.reason ? el("p", { class: "fxa-text", text: v.reason }) : null,
      rows.length ? el("div", { class: "fxa-scroll" }, el("table", { class: "fxa-table" },
        el("caption", { class: "sr-only", text: `Metrics before the fix and after re-running the experiment, proposal ${p.id}` }),
        el("thead", null, el("tr", null, ...["Metric", "Before", "After", "Verdict"].map((h) => el("th", { scope: "col", text: h })))),
        el("tbody", null, ...rows))) : null,
      (v.new_findings || []).length ? el("p", { class: "fxa-text" }, el("strong", { text: "New findings in the re-run: " }),
        el("span", { class: "mono", text: v.new_findings.join(", ") })) : null);
  }

  function audit(p) {
    const d = (details.get(p.id) || {}).data;
    const entries = (d && d.audit) || [];
    if (!entries.length) return null;
    return el("details", { class: "fxa-audit" },
      el("summary", { class: "micro" }, `Audit trail (${entries.length})`),
      el("ol", { class: "fxa-list" }, ...entries.map((a) => el("li", null,
        el("span", { class: "mono dim", text: `${relTime(a.at)} · ${a.actor}` }),
        el("span", { text: ` ${a.from_status && a.from_status !== a.to_status ? `${a.from_status} → ${a.to_status}` : a.to_status}` }),
        a.note ? el("span", { class: "dim", text: ` — ${a.note}` }) : null))));
  }

  function card(p) {
    const st = local(p.id);
    const titleId = `fxa-${p.id}-title`;
    const countdown = el("span", { class: "fxa-countdown micro" });
    const node = el("article", { class: "fxa-card", "data-status": p.status, "aria-labelledby": titleId },
      el("header", { class: "fxa-card__head" },
        el("h3", { class: "fxa-card__title", id: titleId },
          el("span", { text: "Can I fix this? " }),
          el("code", { class: "mono", text: p.catalog_fix_id }),
          el("span", { text: ` on ${(p.target_services || []).join(", ") || "the stack"}` })),
        el("div", { class: "row row--wrap" }, statusChip(p),
          el("span", { class: "micro dim", text: `${p.finding_code} · experiment ${shortId(p.experiment_id)} · ${p.id}` }),
          countdown)),
      protectedBanner(p),
      ...PARTS.map(([k, t]) => partSection(p, k, t)),
      hashRow(p),
      p.status === "pending" ? decisionForm(p) : null,
      ...outcome(p),
      verificationTable(p),
      st.result ? el("p", { class: "banner", "data-tone": "ok", role: "status" }, svgIcon("check", 14), el("span", { text: st.result })) : null,
      st.error ? el("p", { class: "banner", "data-tone": "danger", role: "alert" }, svgIcon("alert", 14), el("span", { text: st.error })) : null,
      audit(p));
    return { node, countdown };
  }

  function tickCountdown(entry) {
    const p = entry.summary;
    let text = "";
    if (p.status === "pending" || p.status === "approved") {
      const left = fmtLeft(secondsLeft(p));
      text = left ? `expires in ${left}` : "deadline passed: nothing will be applied";
    }
    if (entry.countdown.textContent !== text) entry.countdown.textContent = text;
  }

  function place(container, list, heading, headingId, open) {
    clear(container);
    if (!list.length) { container.hidden = true; return; }
    container.hidden = false;
    container.appendChild(el("h3", { class: "exp-sec__title", id: headingId }, svgIcon(open ? "clock" : "refresh", 13),
      el("span", { text: `${heading} (${list.length})` })));
    for (const p of list) container.appendChild(cardFor(p).node);
  }

  function cardFor(p) {
    const st = local(p.id);
    const d = details.get(p.id);
    const job = (D.state && D.state.job) || {};
    // seconds_left is deliberately not in the signature: the countdown ticks in place
    const s = sig([{ ...p, seconds_left: undefined }, st.busy, st.error, st.result, d && d.data ? d.data.audit : null,
      d && d.data ? d.data.expected_metrics : null, job.kind === "fix" ? [job.label, job.status, job.error] : null,
      secondsLeft(p) <= 0]);
    let entry = cards.get(p.id);
    if (!entry || entry.sig !== s) {
      const focusedId = document.activeElement && hostEl.contains(document.activeElement) ? document.activeElement.id : "";
      const built = card(p);
      if (entry && entry.node.parentNode) entry.node.replaceWith(built.node);
      if (entry && entry.summary.change_hash !== p.change_hash) {
        announce(`Proposal ${p.id} changed. Review the new change before approving.`);
      }
      entry = { ...built, sig: s, summary: p };
      cards.set(p.id, entry);
      if (focusedId) { const f = document.getElementById(focusedId); if (f) f.focus(); }
    }
    entry.summary = p;
    tickCountdown(entry);
    return entry;
  }

  let lastLayout = "";
  function render(force = false) {
    const items = D.items;
    const waiting = items.filter((p) => p.status === "pending");
    const flight = items.filter((p) => IN_FLIGHT.has(p.status));
    const done = items.filter((p) => !IN_FLIGHT.has(p.status) && p.status !== "pending");
    const open = waiting.filter((p) => secondsLeft(p) > 0).length;

    clear(countBox);
    if (open) countBox.appendChild(chip(`${open} waiting`, "warn", "clock"));

    clear(emptyBox);
    if (!items.length) {
      emptyBox.appendChild(el("div", { class: "empty" },
        svgIcon("wrench", 26, { class: "empty__icon" }),
        el("p", { class: "empty__title", text: "No fixes waiting for approval" }),
        el("p", { class: "empty__hint", text: "When an analysed experiment has a finding a catalog fix can address, Faultline asks here before changing anything." })));
    }

    for (const p of done) {
      const st = L.get(p.id);
      if (st && st.result && !autoOpened.has(p.id)) { autoOpened.add(p.id); decidedOpen = true; force = true; }
    }

    // rebuild the section structure only when membership or order changed; cards diff themselves
    const layout = sig([waiting.map((p) => p.id), flight.map((p) => p.id), done.map((p) => p.id)]);
    if (force || layout !== lastLayout) {
      lastLayout = layout;
      place(waitingSec, waiting, "Waiting for a decision", "fxa-waiting-h", true);
      place(flightSec, flight, "Approved and in progress", "fxa-flight-h", false);
      clear(decided);
      decided.hidden = !done.length;
      decided.open = decidedOpen;
      if (done.length) {
        decided.appendChild(el("summary", { class: "exp-sec__title" }, svgIcon("list", 13),
          el("span", { text: `Recently decided (${done.length})` })));
        for (const p of done) decided.appendChild(cardFor(p).node);
      }
    } else {
      for (const p of items) cardFor(p);
    }
    const ids = new Set(items.map((p) => p.id));
    for (const id of [...cards.keys()]) if (!ids.has(id)) { cards.delete(id); details.delete(id); L.delete(id); }

    const msg = open ? `${open} fix proposal${open === 1 ? "" : "s"} waiting for a decision` : "";
    if (msg && msg !== lastAnnounced) announce(msg);
    lastAnnounced = msg;
  }

  render();

  return {
    update(state) {
      const s = state || {};
      if (typeof s.now === "number") clockOffsetMs = s.now * 1000 - Date.now();
      const items = ((s.proposals && s.proposals.latest) || []).filter((p) => p && p.id);
      D = { state: s, items };
      for (const p of items) loadDetail(p);
      sweepIfOverdue(items);
      render();
    },
    destroy() {
      document.removeEventListener("fl-explain", onExplainAll);
      clear(hostEl);
      hostEl.classList.remove("panel", "fxa-root");
    },
  };
}

// ---------------------------------------------------------------------------
// Scoped styling: only what app.css does not define. No literal colour; tokens.css vars only.
// prefers-reduced-motion: nothing here animates, and app.css already zeroes transitions globally.
const SCOPED_CSS = `
.fxa-root [hidden]{display:none}
.fxa-root .fxa-h2{margin:0;font:inherit;letter-spacing:inherit;text-transform:inherit;color:inherit}
.fxa-root .fxa-count{display:inline-flex;margin-left:var(--s2)}
.fxa-root .fxa-body{display:flex;flex-direction:column;gap:var(--s3)}
.fxa-root .banner{align-items:flex-start;line-height:1.5;overflow-wrap:anywhere}
.fxa-root .banner > .icon{margin-top:3px;flex:none}
.fxa-root .fxa-sec{display:flex;flex-direction:column;gap:var(--s3)}
.fxa-root .fxa-card{display:flex;flex-direction:column;gap:var(--s3);padding:var(--s3);
  border:1px solid var(--border);border-left:3px solid var(--tone-warn-border);border-radius:var(--r2);background:var(--surface-2)}
.fxa-root .fxa-card[data-status="approved"],.fxa-root .fxa-card[data-status="applied"]{border-left-color:var(--tone-cyan-border)}
.fxa-root .fxa-card[data-status="applying"]{border-left-color:var(--tone-chaos-border)}
.fxa-root .fxa-card[data-status="verified"]{border-left-color:var(--tone-ok-border)}
.fxa-root .fxa-card[data-status="verification_failed"]{border-left-color:var(--tone-danger-border)}
.fxa-root .fxa-card[data-status="rejected"],.fxa-root .fxa-card[data-status="expired"],
.fxa-root .fxa-card[data-status="rolled_back"]{border-left-color:var(--border-strong)}
.fxa-root .fxa-card__head{display:flex;flex-direction:column;gap:var(--s2)}
.fxa-root .fxa-card__title{margin:0;font-size:14px;font-weight:600;line-height:1.4;color:var(--text);overflow-wrap:anywhere}
.fxa-root .fxa-card__title code{font-size:13px;color:var(--text)}
.fxa-root .fxa-countdown{margin-left:auto;text-transform:none;letter-spacing:0}
.fxa-root .fxa-part{display:flex;flex-direction:column;gap:var(--s1);min-width:0}
.fxa-root .fxa-part .exp-sec__title{margin:0}
.fxa-root .fxa-text{margin:0;font-size:13px;line-height:1.55;color:var(--text);overflow-wrap:anywhere}
.fxa-root .fxa-small{font-size:12px}
.fxa-root .fxa-list{margin:0;padding-left:var(--s4);font-size:12px;line-height:1.6;color:var(--text);overflow-wrap:anywhere}
.fxa-root .fxa-diff{margin:0;padding:var(--s2) var(--s3);max-height:320px;overflow:auto;
  background:var(--bg);border:1px solid var(--border);border-radius:var(--r1);font-size:12px;line-height:1.6;color:var(--text-dim)}
.fxa-root .fxa-diff code{display:block;min-width:max-content}
.fxa-root .fxa-diff__line{display:block;white-space:pre;padding-inline:var(--s1);border-left:2px solid transparent}
.fxa-root .fxa-diff__line[data-kind="add"]{color:var(--text);background:var(--tone-ok-bg);border-left-color:var(--tone-ok-border)}
.fxa-root .fxa-diff__line[data-kind="del"]{color:var(--text);background:var(--tone-danger-bg);border-left-color:var(--tone-danger-border)}
.fxa-root .fxa-diff__line[data-kind="hunk"],.fxa-root .fxa-diff__line[data-kind="file"]{color:var(--text-dim);font-weight:600}
.fxa-root .fxa-nosel{user-select:none;-webkit-user-select:none}
.fxa-root .fxa-legendrow{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:var(--s2)}
.fxa-root .fxa-legend{margin:0;text-transform:none;letter-spacing:0;flex:1 1 220px}
.fxa-root .fxa-hash{display:flex;flex-wrap:wrap;align-items:baseline;gap:var(--s2);margin:0}
.fxa-root .fxa-hash code{font-size:13px;color:var(--text);padding:0 var(--s1);border:1px solid var(--border);border-radius:var(--r1);background:var(--bg)}
.fxa-root .fxa-form{display:flex;flex-direction:column;gap:var(--s2);padding-top:var(--s2);border-top:1px solid var(--border)}
.fxa-root .fxa-reason{min-height:var(--hit)}
.fxa-root .fxa-actions{display:flex;flex-wrap:wrap;gap:var(--s2);align-items:center}
/* 44px targets for the two buttons that decide anything, at every pointer type */
.fxa-root .fxa-btn{min-height:var(--hit);padding-inline:var(--s4)}
.fxa-root .fxa-scroll{overflow-x:auto}
.fxa-root .fxa-table{width:100%;border-collapse:collapse;font-size:12px}
.fxa-root .fxa-table th,.fxa-root .fxa-table td{padding:5px var(--s2);text-align:left;vertical-align:middle;border-bottom:1px solid var(--border)}
.fxa-root .fxa-table thead th{color:var(--text-dim);font-weight:600;font-size:11px;letter-spacing:0.04em;text-transform:uppercase}
.fxa-root .fxa-table tbody th{color:var(--text);font-weight:600}
.fxa-root .fxa-table td.num{text-align:right}
.fxa-root .fxa-table tr.is-target th{color:var(--text)}
.fxa-root .fxa-decided > summary,.fxa-root .fxa-audit > summary{cursor:pointer;min-height:var(--hit);display:flex;align-items:center;gap:var(--s2)}
.fxa-root .fxa-decided{display:flex;flex-direction:column;gap:var(--s3)}
.fxa-root .fxa-decided[open] > .fxa-card{margin-top:var(--s3)}
.fxa-root .empty{min-height:0;padding:var(--s5) var(--s3)}
`;
