# Quick Response Agent

Support-ticket agent for the Roosterly helpdesk. One command reads every open ticket and posts one decision per
ticket: reply (in the customer's language), hand off (English note) or close. Built on Agno with Gemini.

## How to run
```
python -m venv .venv && .venv\Scripts\activate        # source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
copy .env.example .env                               # fill GEMINI_API_KEY, HELPDESK_KEY, CANDIDATE_EMAIL
python -m qra reset --yes && python -m qra run       # --workers 4 by default; --dry-run posts nothing
python -m qra eval                                   # the eval below; explain <id>, status, pytest also work
```

## Decisions
1. **Judgments from a model, decisions in code.** `gemini-decision-1` answers about 30 narrow yes/no and choice
   questions per ticket in one call (wording in `qra/questions.py`). A pure truth table (`qra/policy.py`) turns the
   probabilities into the action. Client rules (refund, cancellation, GDPR, security, legal, churn) escalate from
   p >= 0.3 and can only escalate; P1/P2 always go to a human; anything in the 0.3-0.7 band is "unsure" and never
   ends in a reply. Thresholds were fixed before the eval and not tuned.
2. **Dutch without reading it.** The judge reads Dutch directly (no translate step: one call less, no lost nuance);
   `gemini-3.8-flash` drafts in the customer's language from the matched FAQ entries only. A reply is sent only if
   deterministic checks pass (every number, link and menu name must come from faq.md or the ticket; derived values
   such as 25 x EUR 6 are allowed) and a per-sentence "supported by the help centre" check passes. An English
   gloss is logged for human review. Cost: two extra Decision calls per reply.
3. **A drafted reply never outranks safety.** A reply that fails verification becomes a handoff with the draft attached.
   I used an AI coding assistant for scaffolding, tests and review; I wrote the design, the eval cases and the
   thresholds, and I can walk through every module.
   Assumptions: "how do I upgrade" is a reply, "upgrade us now" is a contract change (human); a feature request gets
   an acknowledgement pointing to the roadmap; a language outside nl/en/de goes to a human.

## Reliability
- **503:** exponential backoff with jitter, 6 tries. **429:** wait `Retry-After`; a shared limiter holds 4 req/s.
  **409:** never retried, recorded as reconciled.
- **Crash safety:** the decision is written to a SQLite ledger (unique per ticket) before any POST. On restart a
  decided ticket is GET-checked, then the stored payload is re-sent, never regenerated. A timeout after a POST is
  an unknown outcome: GET first. Guarantee: at most one answer per ticket; every open ticket is eventually handled.
- **Failures:** only an invalid API key stops a run. Anything else (503, model errors, bad structure) leaves that
  ticket open, is logged and counted, and the run exits non-zero; a re-run finishes it.
- **Security:** payment data is redacted before any model call and from every reply and log; ticket text is data
  (never in instructions), no model has a tool that can post; keys come from the environment; https only.

## Eval and fix
Labels come from Nordavo's 10 examples plus 26 cases I wrote from the client rules before running anything.
Baseline: unsafe replies 0; examples action 9/10, over-escalation 1/6 (feature requests escalated). Cause: the FAQ
questions asked whether the text "answers" the message, which a feature request cannot be. Fix (one wording
change, `eval/results/`): ask for the "appropriate response". After: unsafe 0, action 10/10, over-escalation 0/6,
FAQ match 6/6; authored cases 23/26 -> 24/26. Remaining miss: "how do I upgrade" trips the contract rule (0.51);
left as is, because it errs toward a human.
Cost per 100 tickets: **{{COST}}** from logged tokens (Decision 1 $0.10/1M input; Flash $0.75 in, $3.75 out).

## What I would improve
Learn from human corrections (agents' edits to handoff notes) to tune thresholds on real traffic; shared Redis
queue and Postgres ledger for many workers (`docs/SCALING.md`). In the client rules I would give refunds and
personal-data requests an explicit priority: today the labels imply P2 but the rules do not say so.
