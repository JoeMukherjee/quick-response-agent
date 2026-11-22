# Quick Response Agent

Support-ticket agent for the Roosterly helpdesk: one command reads every open ticket and posts one outcome per ticket
(reply in the customer's language, hand off with an English note, or close). Built on Agno with Gemini.
Full design, measurements and trade-offs: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## How to run
```
python -m venv .venv && .venv\Scripts\activate        # source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
copy .env.example .env                               # fill GEMINI_API_KEY, HELPDESK_KEY, CANDIDATE_EMAIL
python -m qra reset --yes && python -m qra run       # --workers 4 by default; --dry-run posts nothing
python -m qra eval                                   # also: explain <id>, status, trace <id>, cache stats|clear, pytest
python -m qra serve                                  # live dashboard: flow, worker lanes, faults, tokens, bench controls
```

## Decisions
1. **Judgments from a model, decisions in code.** `gemini-decision-1` returns calibrated probabilities for about 30
   narrow questions per ticket; a pure truth table (`qra/decision/policy.py`) turns them into the action. Client rules
   (refund, cancellation, personal data, security, legal, churn) escalate from p >= 0.3 and can only escalate; P1/P2
   always go to a human; the 0.3-0.7 band is "unsettled" and never ends in a reply. Thresholds were fixed before the eval.
2. **Replies are proven, not trusted.** `gemini-3.8-flash` drafts in Dutch, English or German from the matched help-centre
   entries only. A reply is sent only if deterministic checks pass (every number, link and menu name comes from the help
   centre or the ticket) and a per-sentence support check passes; otherwise it becomes a handoff with the draft attached.
3. **Tokens are the real budget.** Decision 1 has no context cache and the free tier allows about 45k tokens a minute, so
   I measured three phrasings of the help-centre question: stating the definition once cut tokens per ticket 21%
   (3,779 to 2,979) with no safety loss; a one-question variant cut 48% but lost accuracy and was rejected. A bounded
   frequency-aware cache and a second draw for borderline tickets (within 0.1 of a gate, safer answer wins) round it out;
   noise measurement: the margin flags 7 of 9 gate crossings.
4. **Built to be observed and to scale.** Every stage, call and failure is a telemetry event; every lookup is indexed
   (query-plan tests fail on a scan); the dashboard loads in 0.25 s first paint with 79 KB of JavaScript. The queue,
   ledger and limiter are seams for Redis or Postgres (`docs/SCALING.md`).

I built this with an AI coding assistant working under my direction. It wrote much of the code and tests; I set the
requirements and design rules, chose the thresholds and reviewed the labels and results. Every number here comes from a
run in this repository.

## Reliability
- **503:** exponential backoff with jitter, 6 tries (the final run retried two). **429:** wait `Retry-After`; a shared
  limiter holds 4 req/s. **409:** never retried, recorded as reconciled.
- **Exactly once:** the decision is written to a SQLite ledger (unique per ticket and epoch) before any POST. On restart a
  decided ticket is GET-checked, then the stored payload is re-sent, never regenerated; a timeout after a POST is an
  unknown outcome, so GET first. Proven on the real helpdesk: killed mid-run at 10 of 30, resumed with the remaining 20,
  0 errors, one answer each (`docs/LIVE_FAULTS.md`). The final run's ledger shows 30 decided, 30 sent, 30 distinct tickets.
- **Failures:** only an invalid API key stops a run. Anything else leaves that ticket open, is logged and counted, and a
  re-run finishes it. Telemetry failures are swallowed and counted.
- **Security:** payment data is redacted before any model call and from every reply and log; ticket text is data, never
  instructions; no model can post; https only, no redirects; the dashboard is loopback-only with a cookie token.

## Eval and fix
Labels: Nordavo's 10 examples plus 26 cases I wrote from the client rules before running anything; the 30 practice
tickets were never used for tuning. Baseline had one failure pattern: feature requests were escalated because the
question asked whether an entry "answers" the message. One wording change (ask for the "appropriate response") took
examples from action 9/10 to 10/10 and over-escalation from 1/6 to 0/6 in that run. Final configuration (after the token
cut, measured again): **unsafe replies 0 of 36**, handoff recall 18/18, examples action 9/10, authored cases 25/26.
The one example miss is a price question the judge puts in the unsettled band, so it goes to a human: the safe direction.
The model's answers drift between runs 20 to 30 minutes apart, which is why variants are compared within one window.
**Final run:** 30 of 30 sent, 0 errors, 138.6 s (18 handoff, 11 reply, 1 close; 8 borderline tickets resampled).
**Cost per 100 tickets: $0.11** from logged tokens (Decision 1 $0.043 at $0.10/1M input; Flash $0.065 at $0.75 in, $3.75 out).

## What I would improve
Learn from human edits to handoff notes to tune thresholds on real traffic, since mine come from the rules and 36 labelled
cases; widen the resample margin or draw a third sample for the questions that swing most (`customer_blocked`); move to
the shared Redis queue and Postgres ledger for many workers. In the client rules I would give refunds and personal-data
requests an explicit priority: the labels imply P2 but the rules do not say so.
