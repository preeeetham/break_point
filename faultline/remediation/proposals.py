"""The FixProposal lifecycle: the only legal status transitions, the change hash, and expiry.

Every status change goes through `transition()`, which refuses anything not in TRANSITIONS and appends an
audit entry. That is the guarantee that, for example, a rejected or expired proposal can never become
applied, whatever calls it (CLI, dashboard, email, apply engine).

    pending   -> approved | rejected | expired
    approved  -> applying | expired          (expired: approved but never applied before its deadline)
    applying  -> applied | rolled_back       (rolled_back: the stack was unhealthy after applying)
    applied   -> verified | verification_failed | rolled_back
    verification_failed -> rolled_back
    rejected, expired, verified, rolled_back are final.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from faultline.reporting.store import ReportStore
from faultline.schemas import FixProposal, ProposalAuditEntry

TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"approved", "rejected", "expired"}),
    "approved": frozenset({"applying", "expired"}),
    "applying": frozenset({"applied", "rolled_back"}),
    "applied": frozenset({"verified", "verification_failed", "rolled_back"}),
    "verification_failed": frozenset({"rolled_back"}),
    "rejected": frozenset(),
    "expired": frozenset(),
    "verified": frozenset(),
    "rolled_back": frozenset(),
}
FINAL = frozenset(k for k, v in TRANSITIONS.items() if not v)
OPEN = frozenset({"pending", "approved"})          # still waiting on a human or on the apply step


class IllegalTransition(ValueError):
    pass


class HashMismatch(ValueError):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def change_hash(change_preview: str) -> str:
    return hashlib.sha256(change_preview.encode("utf-8")).hexdigest()


def new_proposal_id() -> str:
    return "fx-" + uuid.uuid4().hex[:10]


def expires_at(timeout_s: float, now: Optional[datetime] = None) -> datetime:
    return (now or utcnow()) + timedelta(seconds=max(1.0, float(timeout_s)))


def can_transition(from_status: str, to_status: str) -> bool:
    return to_status in TRANSITIONS.get(from_status, frozenset())


def transition(p: FixProposal, to_status: str, actor: str, note: str = "") -> FixProposal:
    """Move `p` to `to_status` in place, or raise IllegalTransition. Records an audit entry either way it succeeds."""
    if not can_transition(p.status, to_status):
        raise IllegalTransition(f"proposal {p.proposal_id}: {p.status} -> {to_status} is not allowed")
    p.audit.append(ProposalAuditEntry(actor=actor, from_status=p.status, to_status=to_status, note=note))
    p.status = to_status  # type: ignore[assignment]
    return p


def note(p: FixProposal, actor: str, text: str) -> FixProposal:
    """An audit entry that does not change status (e.g. an email sent, a preflight refusal)."""
    p.audit.append(ProposalAuditEntry(actor=actor, from_status=p.status, to_status=p.status, note=text))
    return p


def is_expired(p: FixProposal, now: Optional[datetime] = None) -> bool:
    return p.status in OPEN and (now or utcnow()) >= p.expires_at


def expire_if_due(store: ReportStore, p: FixProposal, now: Optional[datetime] = None) -> FixProposal:
    """Expire an open proposal past its deadline and persist it. Never approves anything."""
    if is_expired(p, now):
        transition(p, "expired", "faultline", "no decision before the deadline; nothing was applied")
        store.write_proposal(p)
    return p


def sweep_expired(store: ReportStore, now: Optional[datetime] = None) -> list[FixProposal]:
    """Expire every overdue open proposal; returns all proposals (newest first) after the sweep."""
    return [expire_if_due(store, p, now) for p in store.proposals()]


def check_hash(p: FixProposal, shown_hash: str) -> None:
    """Refuse an approval that quotes a different change than the one stored now."""
    current = change_hash(p.change_preview)
    if current != p.change_hash:
        raise HashMismatch(f"proposal {p.proposal_id}: stored preview no longer matches its own hash")
    if (shown_hash or "").strip().lower() != current:
        raise HashMismatch(f"proposal {p.proposal_id}: the change you approved is not the current change; "
                           "re-read the proposal and approve the hash it shows now")


def approve(store: ReportStore, proposal_id: str, shown_hash: str, actor: str, reason: str = "") -> FixProposal:
    p = store.proposal(proposal_id)
    if p is None:
        raise KeyError(f"no proposal {proposal_id}")
    expire_if_due(store, p)
    if p.status != "pending":
        raise IllegalTransition(f"proposal {proposal_id} is {p.status}, not pending; it cannot be approved")
    check_hash(p, shown_hash)
    transition(p, "approved", actor, reason or "approved")
    p.decided_by, p.decided_at, p.reason = actor, utcnow(), reason
    store.write_proposal(p)
    return p


def reject(store: ReportStore, proposal_id: str, actor: str, reason: str = "") -> FixProposal:
    p = store.proposal(proposal_id)
    if p is None:
        raise KeyError(f"no proposal {proposal_id}")
    expire_if_due(store, p)
    if p.status != "pending":
        raise IllegalTransition(f"proposal {proposal_id} is {p.status}, not pending; it cannot be rejected")
    transition(p, "rejected", actor, reason or "rejected")
    p.decided_by, p.decided_at, p.reason = actor, utcnow(), reason
    store.write_proposal(p)
    return p
