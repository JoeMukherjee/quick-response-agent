"""The exactly-once-per-ticket delivery protocol.

Guarantee: a customer never gets two answers from this agent. A ticket gets at most one POST that the
server accepted, because (1) the decision is persisted before it is sent, (2) before any retry we ask
the server whether the ticket is already handled, and (3) 409 is never retried.
Residual case: if the process dies after the server saved a POST but before we wrote `sent`, the next run
GETs the ticket, sees it handled and records `reconciled` instead of sending again.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .helpdesk import AlreadyHandled, HelpdeskClient, ServiceUnavailable, UnknownOutcome
from .ledger import TERMINAL, Ledger

log = logging.getLogger("qra.delivery")


@dataclass(frozen=True)
class Outcome:
    ticket_id: str
    result: str  # sent | reconciled | skipped | deferred
    detail: str = ""


def _is_handled(ticket: dict) -> bool:
    return bool(ticket.get("handled"))


def deliver(client: HelpdeskClient, ledger: Ledger, ticket_id: str, action: str, payload: dict) -> Outcome:
    """Send a decision exactly once. `action`/`payload` are used only if no decision is stored yet."""
    state = ledger.state(ticket_id)
    if state in TERMINAL:
        return Outcome(ticket_id, "skipped", f"already {state}")
    if state == "decided":
        # A previous run died between persisting and confirming. Re-post the STORED payload, never a new one.
        action, payload = ledger.decision(ticket_id)
        if _is_handled(client.get_ticket(ticket_id)):
            ledger.record_reconciled(ticket_id, "handled on server before resend")
            return Outcome(ticket_id, "reconciled", "server already had it")
    else:
        ledger.record_decided(ticket_id, action, payload)

    try:
        client.act(ticket_id, action, payload)
    except AlreadyHandled:
        ledger.record_reconciled(ticket_id, "409 already_handled")
        return Outcome(ticket_id, "reconciled", "409")
    except UnknownOutcome:
        # timeout or reset: the server may have saved it. Ask before deciding anything.
        if _is_handled(client.get_ticket(ticket_id)):
            ledger.record_sent(ticket_id)
            return Outcome(ticket_id, "sent", "confirmed by GET after unknown outcome")
        try:
            client.act(ticket_id, action, payload)
        except AlreadyHandled:
            ledger.record_reconciled(ticket_id, "409 on resend")
            return Outcome(ticket_id, "reconciled", "409 on resend")
        except (UnknownOutcome, ServiceUnavailable) as exc:
            return Outcome(ticket_id, "deferred", f"left decided, next run reconciles: {exc}")
    except ServiceUnavailable as exc:
        return Outcome(ticket_id, "deferred", f"left decided, next run retries: {exc}")
    ledger.record_sent(ticket_id)
    return Outcome(ticket_id, "sent")
