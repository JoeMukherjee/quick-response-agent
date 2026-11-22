"""Every failure class the agent can meet, with how it is handled. The dashboard shows ALL of them (zero included),
so "nothing went wrong" and "that failure is not monitored" can never look the same.

severity: info (expected, harmless) | warn (handled, no ticket lost) | error (a ticket is left open or a human is
needed) | fatal (the only class that stops a run: invalid credentials).
"""
from __future__ import annotations

TAXONOMY: dict[str, dict[str, str]] = {
    "http_503": {"label": "Helpdesk 503", "severity": "warn", "layer": "helpdesk",
                 "handling": "Retried with jittered backoff (6 tries). Nothing was saved, so retrying is safe."},
    "http_429": {"label": "Helpdesk rate limit (429)", "severity": "warn", "layer": "helpdesk",
                 "handling": "Waits Retry-After and pushes every worker's next slot out, so the whole pool backs off."},
    "http_409": {"label": "Already answered (409)", "severity": "warn", "layer": "helpdesk",
                 "handling": "Never retried. The ticket is recorded as reconciled: no customer gets two answers."},
    "unknown_outcome": {"label": "Timeout after sending", "severity": "warn", "layer": "helpdesk",
                        "handling": "The server may have saved it: GET the ticket before any retry."},
    "transport_error": {"label": "Network error on a read", "severity": "warn", "layer": "helpdesk",
                        "handling": "Reads are retried with backoff."},
    "http_404": {"label": "Ticket not found (404)", "severity": "error", "layer": "helpdesk",
                 "handling": "Logged; the ticket is skipped and counted."},
    "http_422": {"label": "Payload rejected (422)", "severity": "error", "layer": "helpdesk",
                 "handling": "Never resent blindly; the ticket stays open and is logged."},
    "http_400": {"label": "Bad request (400)", "severity": "error", "layer": "helpdesk",
                 "handling": "Never resent blindly; the ticket stays open and is logged."},
    "http_401": {"label": "Helpdesk key invalid (401)", "severity": "fatal", "layer": "helpdesk",
                 "handling": "The only way a run stops: nothing can succeed. Clean message, no ticket lost."},
    "judge_transient": {"label": "Decision model unavailable", "severity": "warn", "layer": "model",
                        "handling": "Retried with backoff and a token-budget window; after 5 tries the ticket stays open."},
    "judge_rejected": {"label": "Decision request rejected", "severity": "error", "layer": "model",
                       "handling": "Ticket left open and logged; never turned into an automatic handoff."},
    "judge_safety": {"label": "Content blocked by safety filter", "severity": "warn", "layer": "model",
                     "handling": "That one ticket goes to a human."},
    "draft_error": {"label": "Draft generation failed", "severity": "warn", "layer": "model",
                    "handling": "Retried 3 times; then the ticket stays open and a re-run finishes it."},
    "fatal_auth": {"label": "Gemini key invalid", "severity": "fatal", "layer": "model",
                   "handling": "The only way a run stops. Clean message; no ticket handed off."},
    "verify_reject": {"label": "Draft failed verification", "severity": "warn", "layer": "safety",
                      "handling": "Not sent. The ticket goes to a human with the draft attached."},
    "payment_data_redacted": {"label": "Payment data redacted", "severity": "info", "layer": "safety",
                              "handling": "Removed before any model sees it and from every reply and log."},
    "ticket_error": {"label": "Ticket failed", "severity": "error", "layer": "runner",
                     "handling": "Left open, logged, run continues and exits non-zero; a re-run finishes it."},
    "stale_epoch": {"label": "Ledger and server disagree", "severity": "error", "layer": "runner",
                    "handling": "Refuses to run until `reset` starts a new epoch."},
    "list_failed": {"label": "Could not list tickets", "severity": "error", "layer": "runner",
                    "handling": "Reported cleanly; nothing was touched."},
    "lock_held": {"label": "Another run is active", "severity": "error", "layer": "runner",
                  "handling": "Refuses to start a second run."},
}


def describe(error_class: str) -> dict[str, str]:
    return TAXONOMY.get(error_class, {"label": error_class, "severity": "error", "layer": "other",
                                      "handling": "Unclassified: logged."})
