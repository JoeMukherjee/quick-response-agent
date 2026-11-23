# Quick Response Agent

Support-ticket agent for the Roosterly helpdesk: one command reads every open ticket and posts one outcome per ticket
(reply in the customer's language, hand off with an English note, or close). Built on Agno with Gemini.
Rules in plain language: [`docs/RULES.md`](docs/RULES.md). Design and measurements: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## How to run
```
python -m venv .venv && .venv\Scripts\activate        # source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
copy .env.example .env                               # fill GEMINI_API_KEY, HELPDESK_KEY, CANDIDATE_EMAIL
python -m qra reset --yes && python -m qra run       # --dry-run posts nothing; --judge typesafe swaps the decision model
python -m qra eval                                   # also: explain <id>, status, trace <id>, audit, serve (dashboard + Try a ticket), pytest
```

## Decisions
1. **Judgments from a model, decisions in code.** `gemini-decision-1` (or Jev on TypeSafe: one setting, nothing else changes)
   returns calibrated probabilities for about 30 narrow questions per ticket; a pure truth table (`qra/decision/policy.py`)
   turns them into the action. Client rules (refund, cancellation, personal data, security, legal, churn) escalate from
   p >= 0.3 and can only escalate; property tests prove the table is total.
2. **Dutch and other languages.** The model picks nl, en, de or other; the reply is drafted in that language from the matched
   help-centre entries only, handoff notes are always English, `other` goes to a person, and a reply in the wrong language
   fails verification and becomes a handoff.
3. **Assumptions where the brief is silent.** The agent counts itself unsure when a gate probability is between 0.3 and 0.7:
   that never ends in a reply, gets a second draw (safer answer wins) and, for a borderline reply, a second-opinion agent.
   Only an empty ticket or clear spam (>= 0.85) is closed. Priority follows the brief's four definitions; refunds and personal
   data are P2 (the examples imply it).
4. **Replies are proven, not trusted.** `gemini-3.8-flash` drafts; a reply is sent only if deterministic checks pass (every
   number, link and menu name comes from the help centre or the ticket) and no sentence is judged to state anything outside
   them. Otherwise it becomes a handoff with the draft attached.
5. **Tokens are the budget.** Decision 1 has no context cache and the free tier allows about 45k tokens a minute; stating the
   definition once cut tokens per ticket 21% with no safety loss. Explicit context caching was measured and rejected (about
   $0.0006 per call saved). No sampling parameters are set (no temperature, top_p, top_k). Prompts contain no test data: a test fails if six words of an eval ticket appear in the code.

I built this with an AI coding assistant working under my direction. It wrote much of the code and tests; I set the
requirements and design rules, chose the thresholds and reviewed the labels and results.

## Reliability
- **503:** backoff with jitter, 6 tries. **429:** wait `Retry-After`; a shared limiter holds 4 req/s. **409:** never retried.
- **Exactly once:** the decision is written to a SQLite ledger (unique per ticket and epoch) before any POST. On restart a
  decided ticket is GET-checked, then the stored payload is re-sent, never regenerated. Proven on the real helpdesk: killed
  at 10 of 30, resumed, one answer each (`docs/LIVE_FAULTS.md`). Final run ledger: 30 decided, 30 sent.
- **Failures:** only an invalid API key stops a run; anything else leaves that ticket open, logged, and a re-run finishes it.
- **Security:** payment data is redacted before any model call and from every reply and log; ticket text is data, never
  instructions; no model can post; https only, no redirects; the dashboard is loopback-only.

## Eval and fix
Labels: Nordavo's 10 examples, 26 cases written from the client rules before any run, 12 held-out; the 30 practice tickets
were never used for tuning. Full pipeline (decide, draft, verify) on Nordavo's examples: language, category, priority and
**action 10/10**, FAQ 6/6, unsafe replies 0, over-escalation 0/6; authored 26/26; held-out 10/12 (both misses go to a human).
Fixes that mattered: asking for the "appropriate response" instead of "answers" (feature requests were escalated), priority
from the brief's definitions, and one-judgment verification: the old "supported OR a greeting" question rejected 16/16 good
replies on TypeSafe and 1/16 on Gemini, the new one 2/16 and 0/16, catching the same 45 of 48 injected invented claims.
**Decision models, same code and requests:** Gemini 10/10 action, TypeSafe 7/10 (Jev reads a plain VAT change as injection,
0.60 vs 0.01); both fail toward a human. Judge call p50 445 ms vs 298 ms, 35% fewer judge tokens on Jev; full cost per 100 tickets
$0.126 vs $0.082 (same Flash drafter, only the judge line differs: $0.045 vs $0.012). Latency figures exclude Gemini's free-tier
50k tokens/min wait, which only the Gemini mode pays.
**Final run:** 30 of 30 sent, 0 errors, 189.2 s (17 handoff, 11 reply, 2 close; 7 borderline tickets resampled).
**Cost per 100 tickets: $0.11**, from logged tokens: Decision 1 137,859 in x $0.10/M = $0.0138; Flash 7,862 in x $0.75/M
+ 3,690 out x $3.75/M = $0.0197; $0.0335 for 30 tickets x 100/30 = $0.112.

## What I would improve
Tune thresholds on real traffic and human edits to handoff notes, since mine come from the rules and 48 labelled cases
(`python -m qra audit` already lists decisions a person should glance at). Give Jev its own calibrated thresholds; I kept
them shared so the two models stay comparable. Move to a shared Redis queue and Postgres ledger for many workers
(`docs/SCALING.md`). The client rules should state priorities for refunds and personal-data requests.
