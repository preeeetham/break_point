"""Approval agent: the human in the loop, by email.

Two scenarios, one mechanism — pause, email the admin a closed set of replies, wait for one of them
or for the deadline, record what happened and why:

  pre_injection     an experiment the protected-services policy would otherwise have required
                    `--allow-protected` for. Blocks the injector until the admin clears it.
  problem_occurred  an experiment whose Risk assessment came back high or critical, or whose
                    recovery needed the escalation ladder. Blocks nothing by itself.

This agent reads reports other agents already wrote and sends and receives mail. It never injects,
never calls the executor, never applies a fix and never writes its own risk or fix prose. Its only
two write effects on the system are recording a decision and writing or clearing the STOP file.

Credentials live in `.env` only. They are never logged and never written into any reports/ file: a
record says who was emailed and which token correlates the reply, never what was used to send it.
"""
from __future__ import annotations

import email
import email.utils
import imaplib
import re
import secrets
import smtplib
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any, Callable, Optional

from ..chaos.catalog import CATALOG, clamp
from ..reporting.store import ReportStore
from ..safety.monitor import Watchdog
from ..schemas import (PRE_INJECTION_REPLIES, PROBLEM_REPLIES, TIMEOUT_DEFAULTS, ApprovalDecision, ApprovalRequest,
                       ExperimentReport, ExperimentSpec, FixRecommendation, RiskAssessment)

Log = Callable[[str], None]

# A malformed reply gets the options sent back once. The cap stops a mail loop with an auto-responder
# from turning one pending decision into an outbound flood.
MAX_CLARIFICATIONS = 3
# Parameters that make an experiment longer or harsher. APPROVE REDUCED halves whichever of these the
# experiment actually carries; the catalog's own bounds then clamp the result.
INTENSITY_PARAMS = ("duration_s", "delay_ms", "jitter_ms", "loss_percent", "workers", "writers", "size_mb", "rps")
PROBLEM_RISK_LEVELS = ("high", "critical")

SUBJECT_PREFIX = "[Faultline"
# `Subject: ` plus the value must stay inside one 78-column header line. A folded subject is legal
# but it puts a CRLF inside the value, which breaks `Re: ` threading and can land between the words
# an IMAP `HEADER SUBJECT` search is matching on. The token sits near the front for the same reason.
SUBJECT_MAX = 69


def _utc(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _pct(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{round(x * 100)}%"


def _ms(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x:.0f} ms"


def _subject(token: str, tail: str) -> str:
    """`[Faultline FL-xxxxxxxx] <tail>`, kept ASCII and short so the header never folds.

    One non-ASCII character turns the whole subject into an RFC 2047 encoded-word, which folds and
    puts a CRLF inside the value: `Re:` threading breaks and an IMAP `HEADER SUBJECT` match on the
    correlation token becomes a matter of where the fold happened to land.
    """
    head = f"{SUBJECT_PREFIX} {token}] "
    plain = tail.encode("ascii", "replace").decode("ascii").replace("?", "-")
    room = SUBJECT_MAX - len(head)
    return head + (plain if len(plain) <= room else plain[: room - 3].rstrip() + "...")


def _params(params: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v}" for k, v in (params or {}).items()) or "defaults"


# ---------------------------------------------------------------- configuration check
def preflight(settings) -> list[str]:
    """Everything that is unset or unusable, named so the operator can fix it without reading code.

    Deliberately loud and deliberately specific: the same discipline as the LLM client reporting
    "LLM_API_KEY not set". An approval gate that cannot reach the admin must say so, not quietly
    behave as though every experiment was approved.
    """
    issues: list[str] = []
    if not settings.admin_email:
        issues.append("ADMIN_EMAIL is not set (.env) — there is nobody to ask. The project owner has "
                      "not supplied the real address yet; it is deliberately not hardcoded anywhere.")
    if not settings.smtp_host:
        issues.append("SMTP_HOST is not set (.env) — cannot send the request")
    if not settings.smtp_user:
        issues.append("SMTP_USER is not set (.env)")
    if not settings.smtp_password:
        issues.append("SMTP_PASSWORD is not set (.env)")
    if not settings.imap_host:
        issues.append("IMAP_HOST is not set (.env, or leave it empty to reuse SMTP_HOST) — cannot read the reply")
    if not settings.imap_user:
        issues.append("IMAP_USER is not set (.env, or leave it empty to reuse SMTP_USER)")
    if not settings.imap_password:
        issues.append("IMAP_PASSWORD is not set (.env, or leave it empty to reuse SMTP_PASSWORD)")
    if settings.approval_timeout_s <= 0:
        issues.append(f"APPROVAL_TIMEOUT_SECONDS must be positive, got {settings.approval_timeout_s}")
    return issues


# ---------------------------------------------------------------- scenario triggers
def needs_pre_injection_approval(spec: ExperimentSpec) -> tuple[bool, str]:
    """Scenario A's trigger. The planner has already decided this by consulting the policy.

    Weakness Scout hook (not wired on this branch — the Scout agent had not landed when this was
    written): when `reports/weaknesses.json` exists, a spec whose target and failure type match a
    weakness the Scout predicted at high or critical severity should also come through here. Add it
    in `faultline.planning.planner.Planner.candidates`, next to the policy check that already sets
    `requires_approval`, so the trigger stays in one place — see `scout_predicted_severity` below.
    """
    if spec.requires_approval:
        return True, spec.approval_reason or "the protected-services policy requires an explicit decision"
    return False, ""


def scout_predicted_severity(store: ReportStore, spec: ExperimentSpec) -> Optional[str]:
    """The documented Weakness Scout hook, duck-typed so it starts working the moment the Scout lands.

    Returns the predicted level ("high"/"critical"/...) the Scout attached to a weakness matching this
    spec's target and failure type, or None when the Scout has not run (or has not landed at all).
    Nothing calls this yet; `needs_pre_injection_approval` is where it belongs once `weaknesses.json`
    is a thing.
    """
    read = getattr(store, "weaknesses", None)
    if read is None:
        return None
    try:
        report = read()
    except Exception:
        return None
    for weakness in getattr(report, "weaknesses", None) or []:
        suggested = getattr(weakness, "suggested_experiment", None)
        if suggested is None:
            continue
        if (getattr(suggested, "target_service", None) == spec.target_service
                and getattr(suggested, "failure_type", None) == spec.failure_type):
            return getattr(weakness, "predicted_level", None)
    return None


def problem_occurred(report: ExperimentReport, risk: Optional[RiskAssessment]) -> tuple[bool, str]:
    """Scenario B's trigger: a severe assessment, or a recovery the platform could not do on its own."""
    reasons: list[str] = []
    if risk is not None and risk.level in PROBLEM_RISK_LEVELS:
        reasons.append(f"the Risk agent scored this {risk.level.upper()} ({risk.severity:.1f}/10)")
    if report.recovery.remediation_actions:
        n = len(report.recovery.remediation_actions)
        reasons.append(f"the recovery ladder had to intervene {n} time{'s' if n > 1 else ''} "
                       f"({'; '.join(report.recovery.remediation_actions)})")
    return bool(reasons), " and ".join(reasons)


# ---------------------------------------------------------------- APPROVE REDUCED
def halve(failure_type: str, params: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Halve duration and intensity, then let the catalog clamp the result.

    The admin picks between "as planned" and "half", never an arbitrary number: a free-form figure in
    an email is a parameter nobody validated. Halving is applied first and the catalog's own bounds
    then raise anything that fell under a minimum, recorded the same way every other clamped
    parameter in this project is.
    """
    ft = CATALOG.get(failure_type)
    if ft is None:
        return dict(params), [f"unknown failure type {failure_type!r}; parameters left untouched"]
    reduced = dict(params or {})
    notes: list[str] = []
    for name in INTENSITY_PARAMS:
        if name not in reduced or isinstance(reduced[name], bool):
            continue
        try:
            before = float(reduced[name])
        except (TypeError, ValueError):
            continue
        # Floor rather than round: half of an odd duration should not come back as 22.5 s, which
        # `pumba --duration` and the reproduce commands would both have to carry as a fraction.
        after = int(before // 2) if float(before).is_integer() else round(before / 2, 3)
        reduced[name] = after
        notes.append(f"APPROVE REDUCED: {name} halved {before:g} -> {after:g}")
    clamped, clamp_notes = clamp(ft, reduced)
    return clamped, notes + clamp_notes


# ---------------------------------------------------------------- reply parsing
@dataclass
class ParsedAnswer:
    answer: str
    raw_first_line: str
    fix_index: Optional[int] = None


_FIX_RE = re.compile(r"^FIX\s+(\d{1,3})$")


def first_meaningful_line(body: str) -> str:
    """The first non-empty, non-quoted line. Quoted lines are the original mail, not an answer."""
    for raw in (body or "").splitlines():
        line = raw.strip()
        if not line or line.startswith(">"):
            continue
        return line
    return ""


def _normalise(line: str) -> str:
    """Collapse whitespace, drop surrounding quotes and one trailing full stop or exclamation mark.

    This is normalisation, not interpretation: `Approve.` and `  approve ` are the word APPROVE typed
    by a human on a phone. Anything with extra words in it still fails to match, and must.
    """
    out = " ".join((line or "").split()).strip().strip('"\'')
    return out[:-1].strip() if out[-1:] in (".", "!") else out


def parse_reply(line: str, scenario: str, fix_count: int = 0) -> Optional[ParsedAnswer]:
    """Match the first line against the scenario's closed vocabulary. None means "not an answer"."""
    raw = (line or "").strip()
    text = _normalise(raw).upper()
    if not text:
        return None
    if scenario == "pre_injection":
        # Longest first: APPROVE REDUCED must never be read as a bare APPROVE.
        for phrase in sorted(PRE_INJECTION_REPLIES, key=len, reverse=True):
            if text == phrase:
                return ParsedAnswer(PRE_INJECTION_REPLIES[phrase], raw)
        return None
    if text in ("WAIT", "CONTINUE"):
        return ParsedAnswer(PROBLEM_REPLIES[text], raw)
    m = _FIX_RE.match(text)
    if m:
        n = int(m.group(1))
        # Out of range is not an answer: the admin may have meant a fix from another email.
        if 1 <= n <= fix_count:
            return ParsedAnswer("fix", raw, fix_index=n)
    return None


# ---------------------------------------------------------------- templates
def _option_block(scenario: str, timeout_s: float, fix_count: int) -> list[str]:
    if scenario == "pre_injection":
        lines = [
            "Reply with ONE of these, on the first line of your reply:",
            "",
            "  APPROVE            run it exactly as planned",
            "  APPROVE REDUCED    run it with the duration and intensity halved, clamped to the",
            "                     catalog's own bounds (the email you get next records what changed)",
            "  SKIP               reject this candidate; the planner moves to the next-ranked one",
            "  STOP RUN           abort the whole run (writes reports/STOP, the same kill switch as",
            "                     `faultline stop`; clear it with `faultline resume`)",
        ]
    else:
        nth = f"1-{fix_count}" if fix_count > 1 else "1"
        lines = [
            "Reply with ONE of these, on the first line of your reply:",
            "",
            f"  FIX <n>            the fix you intend to apply, numbered as above ({nth}). Faultline",
            "                     records your choice against this experiment and does nothing else:",
            "                     no agent in this system ever applies a fix to the running stack.",
            "  WAIT               pause the whole run now (writes reports/STOP) so you can work on it",
            "                     without experiments running underneath you; `faultline resume`",
            "                     clears it when you are done",
            "  CONTINUE           acknowledged, no action needed, the run proceeds",
        ]
    default = TIMEOUT_DEFAULTS[scenario].replace("_", " ").upper()
    lines += ["", f"Anything else is not an answer: you will get one short mail listing these options again,",
              "and nothing will be decided on your behalf.",
              f"If no reply arrives within {timeout_s:.0f} s, this request times out and {default} is applied."]
    return lines


def render_pre_injection(spec: ExperimentSpec, token: str, reason: str, timeout_s: float) -> tuple[str, str]:
    """Scenario A's email. Every fact here already exists on the spec; nothing is re-derived."""
    ft = CATALOG.get(spec.failure_type)
    title = ft.title if ft else spec.failure_type
    h = spec.hypothesis
    subject = _subject(token, f"approve {spec.failure_type} on {spec.target_service}?")
    body = [
        "Faultline is holding an experiment and will not inject anything until you answer.",
        "",
        "What it wants to do",
        f"  Target        {spec.target_service}  (container {spec.container})",
        f"  Failure       {spec.failure_type} — {title}",
        f"  Parameters    {_params(spec.parameters)}",
        f"  Criticality   {spec.criticality_score:g}",
        f"  Planned by    {spec.planned_by}",
        f"  Experiment    {spec.experiment_id}",
        "",
        "Why this experiment (planner's rationale)",
        f"  {spec.rationale.strip() or 'none recorded'}",
        "",
        "Hypothesis",
        f"  {h.statement.strip()}",
        f"  expected to affect: {', '.join(h.expected_affected) or 'nothing beyond the target'}",
        f"  expected error rate at worst: {_pct(h.expected_max_error_rate)}",
        f"  expected to recover within: {h.expected_recovery_s:.0f} s",
        "",
        "Why you are being asked",
        f"  {reason}",
    ]
    if spec.planner_notes:
        body += ["", "Planner notes"] + [f"  - {n}" for n in spec.planner_notes]
    if spec.reproduce:
        body += ["", "Equivalent by hand"] + [f"  {c}" for c in spec.reproduce]
    body += ["", *_option_block("pre_injection", timeout_s, 0), "",
             f"-- Faultline approval {token}. Keep it in the subject line so your reply is matched to "
             f"this request and not to another one."]
    return subject, "\n".join(body)


def render_problem(report: ExperimentReport, risk: Optional[RiskAssessment], fix: Optional[FixRecommendation],
                   narrative: list[str], token: str, reason: str, timeout_s: float) -> tuple[str, str]:
    """Scenario B's email: the Narrator's prose, the Risk agent's assessment, the Fix agent's list.

    No summarising happens here. The plain-English account is whatever `narrator.narrate` produced for
    this experiment, the assessment is the Risk agent's own `summary` and `production_implication`,
    and the fixes are the Fix agent's ranked `recommendations` — numbered so that `FIX <n>` is
    unambiguous.
    """
    service = report.target.get("service", "?")
    level = risk.level.upper() if risk else "UNASSESSED"
    subject = _subject(token, f"{level}: {report.failure_type} on {service} - decide")
    body = [
        f"An experiment finished and needs a decision. Nothing is running because of this mail, and "
        f"nothing has been changed on the stack.",
        "",
        "Why you are being asked",
        f"  {reason}",
        "",
        "What happened (Narrator agent)",
    ]
    # The Narrator closes with its own one-line risk and top-fix summary. This mail carries both in
    # full below, so those two lines are dropped rather than said twice.
    kept = [ln for ln in narrative if not ln.strip().startswith(("Risk:", "Top fix:"))]
    body += [f"  {line.strip()}" if line.strip() else "" for line in kept] or ["  no narrative available"]
    body += ["", "Risk assessment (Risk agent)"]
    if risk is None:
        body.append("  not assessed; run `faultline risk` for this experiment")
    else:
        body += [
            f"  {risk.level.upper()}  severity {risk.severity:.1f}/10   blast radius {risk.blast_radius} "
            f"service(s)   user impact {_pct(risk.user_impact)}   "
            f"recovery {('%.0f s' % risk.recovery_s) if risk.recovery_s is not None else 'not recovered'}",
            f"  factors: {', '.join(f'{k} {v:g}' for k, v in risk.factors.items())}",
            f"  {risk.summary.strip()}",
            f"  In production: {risk.production_implication.strip()}",
        ]
    fixes = list(fix.recommendations) if fix else []
    body += ["", f"Recommended fixes (Fix agent — advisory only, nothing has been applied)"]
    if not fixes:
        body.append("  none available; run `faultline fix` for this experiment")
    for i, rec in enumerate(fixes, 1):
        body += [
            "",
            f"  {i}. {rec.title}",
            f"     {rec.category}, effort {rec.effort}, confidence {rec.confidence:.0%}",
            f"     {rec.rationale.strip()}",
            "     steps:",
            *[f"       {n}. {s}" for n, s in enumerate(rec.steps, 1)],
        ]
        if rec.verify:
            body += ["     verify:", *[f"       - {v}" for v in rec.verify]]
    body += ["", f"Full detail on the blackboard: reports/experiments/{report.experiment_id}.json, "
                 f"reports/risk/{report.experiment_id}.json, reports/fixes/{report.experiment_id}.md",
             "", *_option_block("problem_occurred", timeout_s, len(fixes)), "",
             f"-- Faultline approval {token}. Keep it in the subject line so your reply is matched to "
             f"this request and not to another one."]
    return subject, "\n".join(body)


def render_clarification(req: ApprovalRequest, got: str, fix_count: int) -> tuple[str, str]:
    """One short mail when a reply does not match. It re-states the options and decides nothing."""
    subject = "Re: " + _subject(req.token, "that reply was not an option")
    body = [
        "That reply was not one of the options, so nothing has been decided and Faultline is still "
        "waiting.",
        "",
        f"  you wrote: {got.strip()[:200] or '(empty first line)'}",
        "",
        *_option_block(req.scenario, req.timeout_s, fix_count),
        "",
        f"-- Faultline approval {req.token}",
    ]
    return subject, "\n".join(body)


# ---------------------------------------------------------------- transport
@dataclass
class Reply:
    uid: bytes
    message_id: str
    first_line: str


class Mailer:
    """SMTP send. Raises on any failure so the caller can fail safe rather than assume delivery."""

    def __init__(self, settings, log: Log = print):
        self.s = settings
        self.log = log

    def send(self, to: str, subject: str, body: str, in_reply_to: str = "") -> str:
        msg = EmailMessage()
        msg["From"] = self.s.smtp_from or self.s.smtp_user
        msg["To"] = to
        msg["Subject"] = subject
        msg["Message-ID"] = email.utils.make_msgid(domain="faultline.local")
        msg["Date"] = email.utils.formatdate(localtime=True)
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        msg.set_content(body)
        host, port = self.s.smtp_host, int(self.s.smtp_port)
        if port == 465:
            client: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            client = smtplib.SMTP(host, port, timeout=30)
        try:
            client.ehlo()
            if self.s.smtp_starttls and port != 465:
                client.starttls()
                client.ehlo()
            if self.s.smtp_user:
                client.login(self.s.smtp_user, self.s.smtp_password)
            client.send_message(msg)
        finally:
            try:
                client.quit()
            except Exception:
                pass
        return msg["Message-ID"]


class Mailbox:
    """IMAP polling, one short-lived connection per poll.

    A reply is matched by the correlation token in the subject or by In-Reply-To pointing at the
    message we sent — never by "the newest unread mail", because two decisions can be open at once
    and unrelated mail arrives in between.
    """

    def __init__(self, settings, log: Log = print):
        self.s = settings
        self.log = log

    def _connect(self):
        host, port = self.s.imap_host, int(self.s.imap_port)
        client = (imaplib.IMAP4_SSL(host, port) if self.s.imap_ssl else imaplib.IMAP4(host, port))
        client.login(self.s.imap_user, self.s.imap_password)
        client.select(self.s.imap_mailbox)
        return client

    def find_replies(self, token: str, message_id: str = "") -> list[Reply]:
        client = self._connect()
        try:
            uids: list[bytes] = []
            queries = [("HEADER", "SUBJECT", token)]
            if message_id:
                queries.append(("HEADER", "IN-REPLY-TO", message_id))
            for q in queries:
                try:
                    typ, data = client.uid("SEARCH", None, *q)
                except imaplib.IMAP4.error:
                    continue
                if typ == "OK" and data and data[0]:
                    uids += [u for u in data[0].split() if u not in uids]
            out: list[Reply] = []
            for uid in uids:
                # PEEK, not RFC822: a plain body fetch sets \Seen on a real server, so polling would
                # consume the very unseen state an operator may be relying on in their own mail client.
                typ, data = client.uid("FETCH", uid, "(BODY.PEEK[])")
                if typ != "OK" or not data:
                    continue
                raw = next((part[1] for part in data if isinstance(part, tuple) and len(part) > 1), None)
                if not raw:
                    continue
                msg = email.message_from_bytes(raw)
                out.append(Reply(uid=uid, message_id=(msg.get("Message-ID") or "").strip(),
                                 first_line=first_meaningful_line(_text_body(msg))))
            return out
        finally:
            try:
                client.logout()
            except Exception:
                pass

    def mark_seen(self, uid: bytes) -> None:
        client = self._connect()
        try:
            client.uid("STORE", uid, "+FLAGS", "(\\Seen)")
        finally:
            try:
                client.logout()
            except Exception:
                pass


def _text_body(msg: email.message.Message) -> str:
    """The text/plain part, or the whole payload for a single-part message."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
        return ""
    payload = msg.get_payload(decode=True)
    if payload is None:
        return str(msg.get_payload() or "")
    return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")


# ---------------------------------------------------------------- the agent
class ApprovalAgent:
    """Opens a pending decision, waits for a bounded time, records how it ended.

    `mailer` and `mailbox` are injectable so tests can drive the real smtplib/imaplib clients against
    a loopback double instead of an inbox.
    """

    def __init__(self, settings, store: ReportStore, log: Log = print,
                 mailer: Optional[Mailer] = None, mailbox: Optional[Mailbox] = None):
        self.s = settings
        self.store = store
        self.log = log
        self.mailer = mailer or Mailer(settings, log)
        self.mailbox = mailbox or Mailbox(settings, log)
        self.issues = preflight(settings)

    # ------------------------------------------------------------------ public
    def available(self) -> bool:
        return not self.issues

    def describe(self) -> str:
        where = self.s.admin_email or "nobody (ADMIN_EMAIL unset)"
        return f"email to {where} via {self.s.smtp_host or 'no SMTP host'}, {self.s.approval_timeout_s:.0f}s timeout"

    def request_pre_injection(self, spec: ExperimentSpec, reason: str = "") -> ApprovalDecision:
        """Scenario A. Returns the decision; the caller is what actually holds the injector back."""
        if not reason:
            _, reason = needs_pre_injection_approval(spec)
        req = self._open("pre_injection", spec.experiment_id, spec.target_service, spec.failure_type)
        subject, body = render_pre_injection(spec, req.token, reason, req.timeout_s)
        req.subject, req.options = subject, list(PRE_INJECTION_REPLIES)
        decision = self._send_and_wait(req, body, fix_count=0)
        if decision.answer == "approve_reduced":
            before = dict(spec.parameters)
            after, notes = halve(spec.failure_type, before)
            decision.parameters_before, decision.parameters_after, decision.parameter_notes = before, after, notes
            self._finish(req, decision)
        if decision.answer == "stop_run":
            self.store.request_stop(f"approval:{req.approval_id}")
            self.log(f"[approval] STOP RUN: wrote {self.store.stop_file} — `faultline resume` clears it")
        return decision

    def request_problem(self, report: ExperimentReport, risk: Optional[RiskAssessment],
                        fix: Optional[FixRecommendation], narrative: list[str], reason: str = "") -> ApprovalDecision:
        """Scenario B. Blocks nothing by itself; WAIT is the one answer with a side effect."""
        if not reason:
            _, reason = problem_occurred(report, risk)
        req = self._open("problem_occurred", report.experiment_id, report.target.get("service", ""),
                         report.failure_type)
        subject, body = render_problem(report, risk, fix, narrative, req.token, reason, req.timeout_s)
        req.subject, req.options = subject, list(PROBLEM_REPLIES)
        fixes = list(fix.recommendations) if fix else []
        decision = self._send_and_wait(req, body, fix_count=len(fixes))
        if decision.answer == "fix" and decision.fix_index and decision.fix_index <= len(fixes):
            decision.fix_title = fixes[decision.fix_index - 1].title
            self._finish(req, decision)
            self.log(f"[approval] recorded chosen remediation #{decision.fix_index} "
                     f"({decision.fix_title}) — a decision log entry; nothing was applied")
        if decision.answer == "wait":
            self.store.request_stop(f"approval:{req.approval_id}")
            self.log(f"[approval] WAIT: wrote {self.store.stop_file} — the run is paused until "
                     f"`faultline resume`")
        self._record_on_report(report.experiment_id, decision)
        return decision

    def resend(self, approval_id: str) -> bool:
        """Re-send a pending decision's email, keeping the same token so an earlier reply still matches."""
        req = self.store.pending_approval()
        if req is None or req.approval_id != approval_id:
            self.log(f"[approval] {approval_id} is not the pending decision")
            return False
        if self.issues:
            for i in self.issues:
                self.log(f"[approval] cannot resend: {i}")
            return False
        body = (f"Re-sending the decision Faultline is still waiting on (opened "
                f"{req.created_at.isoformat()}, deadline {req.deadline.isoformat()}).\n\n"
                + "\n".join(_option_block(req.scenario, req.timeout_s, 0))
                + f"\n\n-- Faultline approval {req.token}")
        try:
            self.mailer.send(req.to, _subject(req.token, "decision still open"), body,
                             in_reply_to=req.message_id)
        except Exception as exc:
            self.log(f"[approval] resend failed: {type(exc).__name__}: {exc}")
            return False
        req.resends += 1
        self.store.set_pending_approval(req)
        self.store.write_approval(req)
        self.log(f"[approval] re-sent {req.approval_id} to {req.to} (resend #{req.resends})")
        return True

    # ------------------------------------------------------------------ internals
    def _open(self, scenario: str, experiment_id: str, service: str, failure_type: str) -> ApprovalRequest:
        now = time.time()
        timeout = float(self.s.approval_timeout_s)
        return ApprovalRequest(
            approval_id=uuid.uuid4().hex[:12], scenario=scenario, experiment_id=experiment_id,
            token=f"FL-{secrets.token_hex(4)}", to=self.s.admin_email, target_service=service,
            failure_type=failure_type, created_at=_utc(now), deadline=_utc(now + timeout), timeout_s=timeout,
            default_on_timeout=TIMEOUT_DEFAULTS[scenario],
        )

    def _timeout_decision(self, req: ApprovalRequest, reason: str, clarifications: int = 0) -> ApprovalDecision:
        return ApprovalDecision(answer=TIMEOUT_DEFAULTS[req.scenario], answered=False, reason=reason,
                                clarifications_sent=clarifications)

    def _send_and_wait(self, req: ApprovalRequest, body: str, fix_count: int) -> ApprovalDecision:
        default = TIMEOUT_DEFAULTS[req.scenario].replace("_", " ").upper()
        if self.issues:
            # Not configured is not consent. Scenario A falls to SKIP, exactly as a timeout would.
            req.status, req.send_error = "failed", "; ".join(self.issues)
            decision = self._timeout_decision(req, f"the approval gate is not configured, so nobody could be "
                                                   f"asked; safe default {default} applied — {req.send_error}")
            for i in self.issues:
                self.log(f"[approval] NOT CONFIGURED: {i}")
            self._finish(req, decision)
            return decision

        self.store.set_pending_approval(req)
        try:
            req.message_id = self.mailer.send(req.to, req.subject, body)
            req.sent = True
            self.log(f"[approval] asked {req.to}: {req.subject} (token {req.token}, "
                     f"{req.timeout_s:.0f}s to answer, timeout default {default})")
        except Exception as exc:
            # An approval gate that cannot reach the admin must never become a no-op that approves.
            req.status, req.send_error = "failed", f"{type(exc).__name__}: {exc}"
            decision = self._timeout_decision(req, f"could not send the request ({req.send_error}); "
                                                   f"treated as a timeout, safe default {default} applied")
            self.log(f"[approval] send FAILED ({req.send_error}); failing safe to {default}")
            self._finish(req, decision)
            return decision

        self.store.set_pending_approval(req)
        decision = self._wait(req, fix_count)
        self._finish(req, decision)
        return decision

    def _wait(self, req: ApprovalRequest, fix_count: int) -> ApprovalDecision:
        """Wait for a matching reply until an independent timer says the deadline passed.

        The mailbox is polled on its own daemon thread and the deadline is a `safety.monitor.Watchdog`
        on a third. That is the point: a poll wedged in a blocking socket read cannot hold the
        decision past its deadline, the same discipline as the injector's watchdog, which reverts the
        fault from the timer thread rather than hoping the main loop comes back round.
        """
        expired = threading.Event()
        answered = threading.Event()
        box: list[ApprovalDecision] = []
        counter = {"clarifications": 0}
        watchdog = Watchdog(req.timeout_s, expired.set)
        watchdog.arm()
        threading.Thread(target=self._poll, args=(req, fix_count, box, answered, expired, counter),
                         daemon=True, name=f"approval-{req.approval_id}").start()
        try:
            while not expired.is_set():
                if answered.wait(0.1):
                    return box[0]
                if self.store.stop_requested():
                    return self._timeout_decision(
                        req, "STOP was latched while waiting for the decision; treated as a timeout, safe "
                             f"default {TIMEOUT_DEFAULTS[req.scenario].upper()} applied",
                        counter["clarifications"])
        finally:
            watchdog.disarm()
        if answered.is_set():                       # a reply landed in the same instant the timer fired
            return box[0]
        return self._timeout_decision(
            req, f"no valid reply within {req.timeout_s:.0f} s; safe default "
                 f"{TIMEOUT_DEFAULTS[req.scenario].replace('_', ' ').upper()} applied",
            counter["clarifications"])

    def _poll(self, req: ApprovalRequest, fix_count: int, box: list[ApprovalDecision],
              answered: threading.Event, expired: threading.Event, counter: dict[str, int]) -> None:
        """Poll the mailbox until a valid reply lands or the deadline passes. Daemon thread."""
        handled: set[str] = set()
        logged_error = False
        while not expired.is_set() and not answered.is_set():
            try:
                replies = self.mailbox.find_replies(req.token, req.message_id)
            except Exception as exc:
                if not logged_error:
                    self.log(f"[approval] mailbox unreachable ({type(exc).__name__}: {exc}); "
                             f"retrying until the deadline")
                    logged_error = True
                replies = []
            for reply in replies:
                key = reply.message_id or reply.uid.decode(errors="replace")
                if key in handled:
                    continue
                handled.add(key)
                parsed = parse_reply(reply.first_line, req.scenario, fix_count)
                self._safe_mark_seen(reply.uid)
                if parsed:
                    self.log(f"[approval] {req.to} replied {parsed.raw_first_line!r} -> {parsed.answer}")
                    box.append(ApprovalDecision(
                        answer=parsed.answer, answered=True,
                        reason=f"the admin replied {parsed.raw_first_line.strip()!r}",
                        fix_index=parsed.fix_index, raw_first_line=parsed.raw_first_line,
                        clarifications_sent=counter["clarifications"]))
                    answered.set()
                    return
                if counter["clarifications"] < MAX_CLARIFICATIONS:
                    counter["clarifications"] += 1
                    self._clarify(req, reply.first_line, fix_count)
                else:
                    self.log(f"[approval] another unparseable reply ({reply.first_line!r}); "
                             f"{MAX_CLARIFICATIONS} clarifications already sent, staying quiet")
            expired.wait(max(0.05, float(self.s.approval_poll_s)))

    def _clarify(self, req: ApprovalRequest, got: str, fix_count: int) -> None:
        subject, body = render_clarification(req, got, fix_count)
        try:
            self.mailer.send(req.to, subject, body, in_reply_to=req.message_id)
            self.log(f"[approval] reply {got.strip()[:60]!r} is not one of {req.options}; "
                     f"sent the options back and kept waiting")
        except Exception as exc:
            self.log(f"[approval] could not send the clarification ({type(exc).__name__}: {exc}); still waiting")

    def _safe_mark_seen(self, uid: bytes) -> None:
        try:
            self.mailbox.mark_seen(uid)
        except Exception:
            pass

    def _finish(self, req: ApprovalRequest, decision: ApprovalDecision) -> None:
        req.decision = decision
        if req.status != "failed":
            req.status = "answered" if decision.answered else "timed_out"
        self.store.write_approval(req)
        self.store.clear_pending_approval()

    def _record_on_report(self, experiment_id: str, decision: ApprovalDecision) -> None:
        """Attach the decision to the experiment's own report. A log entry, not an action."""
        report = self.store.experiment(experiment_id)
        if report is None:
            return
        report.approval = decision
        self.store.write_experiment(report)


# ---------------------------------------------------------------- entry points
def gate(settings, store: ReportStore, spec: ExperimentSpec, log: Log = print,
         agent: Optional[ApprovalAgent] = None) -> ApprovalDecision:
    """Scenario A, for a run path to call right before it would have injected.

    On APPROVE REDUCED the spec is mutated in place: halved parameters and a planner note, so the
    report that comes out of the experiment says what the admin changed and why.
    """
    agent = agent or ApprovalAgent(settings, store, log=log)
    decision = agent.request_pre_injection(spec)
    if decision.answer == "approve_reduced":
        spec.parameters = dict(decision.parameters_after)
        spec.planner_notes = list(spec.planner_notes) + decision.parameter_notes + [
            f"admin approved a reduced run of this experiment ({decision.reason})"]
    elif decision.answer == "approve":
        spec.planner_notes = list(spec.planner_notes) + [f"admin approved this experiment ({decision.reason})"]
    return decision


def run_post_experiment(settings, store: ReportStore, experiment_id: str, llm=None, use_llm: bool = False,
                        log: Log = print, agent: Optional[ApprovalAgent] = None) -> Optional[ApprovalDecision]:
    """Scenario B, for a run path to call once an experiment's risk and fix outputs exist.

    Returns None when the triggers do not fire, which is the common case.
    """
    from . import fix as fix_agent
    from . import narrator
    from . import risk as risk_agent

    report = store.experiment(experiment_id)
    if report is None:
        log(f"[approval] no report for {experiment_id}")
        return None
    # Assess on the fly when the Risk agent has not written a file for this experiment yet, exactly
    # as the Fix agent does, so the trigger does not silently depend on `--analyze` having been used.
    risk = store.risk(experiment_id) or risk_agent.assess(report, store.topology(), None)
    fire, reason = problem_occurred(report, risk)
    if not fire:
        return None
    fix = store.fix(experiment_id) or fix_agent.recommend(report, risk, None)
    narrative = narrator.narrate(report, risk, fix, llm, use_llm)
    agent = agent or ApprovalAgent(settings, store, log=log)
    log(f"[approval] {experiment_id}: {reason}; asking {agent.s.admin_email or 'nobody (ADMIN_EMAIL unset)'}")
    return agent.request_problem(report, risk, fix, narrative, reason)


def describe_pending(req: Optional[ApprovalRequest]) -> list[str]:
    """`faultline approvals` output. One block, the same shape as `faultline status`."""
    if req is None:
        return ["no decision is pending"]
    left = (req.deadline - datetime.now(timezone.utc)).total_seconds()
    overdue = left <= 0
    return [
        f"approval {req.approval_id}  [{req.scenario}]  status {req.status}",
        f"  experiment     {req.experiment_id}  ({req.failure_type} on {req.target_service})",
        f"  asked          {req.to or '(nobody: ADMIN_EMAIL unset)'} at {req.created_at.isoformat()}",
        f"  token          {req.token}" + (f"   message-id {req.message_id}" if req.message_id else ""),
        f"  deadline       {req.deadline.isoformat()}  "
        + (f"({-left:.0f}s overdue; the safe default should have fired)" if overdue else f"({left:.0f}s left)"),
        f"  on timeout     {req.default_on_timeout.replace('_', ' ').upper()}",
        f"  options        {', '.join(req.options) or '-'}",
        f"  sent           {req.sent}" + (f"   resends {req.resends}" if req.resends else "")
        + (f"   error {req.send_error}" if req.send_error else ""),
    ]


__all__ = ["ApprovalAgent", "Mailbox", "Mailer", "ParsedAnswer", "Reply", "describe_pending", "first_meaningful_line",
           "gate", "halve", "needs_pre_injection_approval", "parse_reply", "preflight", "problem_occurred",
           "render_clarification", "render_pre_injection", "render_problem", "run_post_experiment",
           "scout_predicted_severity"]
