"""An independent audit of the decisions in a run log: TypeSafe's "verify and escalate" pattern, applied to our own agent.

After a run, a second System One request per ticket reviews what the agent decided against the client's rules, the brief's
priority definitions and the help centre, and flags decisions a person should look at. It is advisory: it never changes
what was posted and never blocks anything. It is cheap (about $0.0025 for 30 tickets on TypeSafe) and independent of the
questions the agent itself used, so it can disagree with the agent, which is the point.

The audit sees the ticket text, the help centre, the client's rules and the agent's decision (including the English gloss of
a reply). It reports ids, flags and probabilities only; the ticket text is not written to the report.
"""
from __future__ import annotations

import json
from pathlib import Path

from qra.core.faq import Faq
from qra.decision.policy import PRIORITY_MEANING

CATEGORIES = {
    "bug": "Something that should work does not, or gives a wrong result",
    "billing": "Money and the subscription: prices, invoices, payments, refunds, upgrading, cancelling",
    "feature_request": "A wish for something the product does not do today",
    "question": "How something works or whether it exists",
    "other": "Spam, an empty message, a privacy-law request, a legal complaint, or none of the above",
}

QUESTIONS = {
    "rules_followed": {"type": "noul", "instructions": "The decision follows the client's rules in `client_rules`.",
        "criteria": {"true_description": ("A ticket about a refund, a cancellation or contract change, personal data, a security incident, "
                                          "a legal threat or leaving went to a human, and no payment details were repeated."),
                     "false_description": "A ticket of that kind was answered with an automatic reply, or payment details appear in the decision."}},
    "reply_supported": {"type": "noul", "instructions": "Every factual statement in `decision.reply_gloss` is written in `help_centre`.",
        "criteria": {"true_description": "Each price, link, menu name and instruction in the reply appears in the help-centre text.",
                     "false_description": "The reply states something the help-centre text does not contain."}},
    "reply_answers_everything": {"type": "noul", "instructions": "The reply in `decision.reply_gloss` answers everything the customer asked in `ticket.message`.",
        "criteria": {"true_description": "Every question or request in the message gets its answer from the reply.",
                     "false_description": "A question or request in the message is left unanswered."}},
    "help_centre_could_answer": {"type": "noul", "instructions": "`help_centre` fully answers what the customer asked in `ticket.message`.",
        "criteria": {"true_description": "Every question in the message has its answer in the help-centre text.",
                     "false_description": "Part of the message needs something the help-centre text does not contain, or needs a person to act."}},
    "priority_matches": {"type": "noul", "instructions": "`decision.priority` matches the definition of that priority in `priority_definitions`.",
        "criteria": {"true_description": "The situation in the message fits the definition of the priority given.",
                     "false_description": "The situation fits the definition of a different priority."}},
    "category_matches": {"type": "noul", "instructions": "`decision.category` is the right category for `ticket.message`, using `category_definitions`.",
        "criteria": {"true_description": "The category given best describes what the message is about.",
                     "false_description": "A different category in `category_definitions` describes the message better."}},
    "was_spam_or_empty": {"type": "noul", "instructions": "`ticket.message` and `ticket.subject` together are spam or contain no real content.",
        "criteria": {"true_description": "An unsolicited advertisement, or no text to act on.",
                     "false_description": "A real customer message about the product."}},
}

# A flag means "a person should look", so each threshold is about doubt, not certainty.
DOUBT = 0.5        # a "true" judgment below this is doubted
HELP_CENTRE = 0.7  # the help centre is taken to answer the ticket from here


def audit_state(ticket: dict, row: dict, faq: Faq) -> dict:
    decision = {"action": row["action"], "category": row.get("category"), "priority": row.get("priority"),
                "language": row.get("language"), "reason": row.get("reason")}
    if row.get("en_gloss"):
        decision["reply_gloss"] = row["en_gloss"]
    return {"ticket": {"subject": ticket.get("subject") or "", "message": ticket.get("body") or "", "customer_plan": ticket.get("customer_plan")},
            "client_rules": faq.rules, "priority_definitions": PRIORITY_MEANING, "category_definitions": CATEGORIES,
            "help_centre": {i: e.text for i, e in faq.entries.items()}, "decision": decision}


def flags_for(action: str, p: dict, priority: str | None, category: str | None) -> list[str]:
    out = []
    if p["rules_followed"] < DOUBT:
        out.append(f"rules_followed {p['rules_followed']:.2f}")
    if action == "reply" and p["reply_supported"] < DOUBT:
        out.append(f"reply_unsupported {p['reply_supported']:.2f}")
    if action == "reply" and p["reply_answers_everything"] < DOUBT:
        out.append(f"reply_incomplete {p['reply_answers_everything']:.2f}")
    if action == "handoff" and p["help_centre_could_answer"] >= HELP_CENTRE:
        out.append(f"could_have_replied {p['help_centre_could_answer']:.2f}")
    if action != "close" and p["was_spam_or_empty"] >= HELP_CENTRE:
        out.append(f"should_have_closed {p['was_spam_or_empty']:.2f}")
    if p["priority_matches"] < DOUBT:
        out.append(f"priority {priority} doubted {p['priority_matches']:.2f}")
    if p["category_matches"] < DOUBT:
        out.append(f"category {category} doubted {p['category_matches']:.2f}")
    return out


def read_log(path: Path) -> list[dict]:
    rows = [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]
    return sorted((r for r in rows if r.get("ticket_id") not in (None, "-") and r.get("action")), key=lambda r: r["ticket_id"])


def audit_run(log_path: Path, client, judge, faq: Faq) -> dict:
    """Audit every decision in a run log. `client.get_ticket(id)` is read-only; nothing is posted or changed."""
    rows = []
    for r in read_log(log_path):
        ticket = client.get_ticket(r["ticket_id"])
        p = judge.ask(json.dumps(audit_state(ticket, r, faq), ensure_ascii=False), QUESTIONS).noul
        rows.append({"id": r["ticket_id"], "action": r["action"], "priority": r.get("priority"), "category": r.get("category"),
                     "flags": flags_for(r["action"], p, r.get("priority"), r.get("category")),
                     "judgments": {k: round(v, 2) for k, v in p.items()}})
    flagged = [r for r in rows if r["flags"]]
    return {"log": Path(log_path).name, "judge": f"{getattr(judge, 'provider', '?')} {getattr(judge, 'model', '?')}",
            "audited": len(rows), "flagged": len(flagged), "input_tokens": getattr(judge, "tokens", 0), "rows": rows}
