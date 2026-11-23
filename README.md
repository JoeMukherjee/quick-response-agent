# Quick Response Agent

Support-ticket agent for the Roosterly helpdesk: one command reads every open ticket and posts one outcome per ticket
(reply in the customer's language, hand off with an English note, or close). Built on Agno; **TypeSafe Jev is the recommended
decision model** (default when `TYPESAFE_API_KEY` is set), Gemini drafts.
Rules in plain language: [`docs/RULES.md`](docs/RULES.md). Design and measurements: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## How to run
```
python -m venv .venv && .venv\Scripts\activate        # source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
copy .env.example .env                               # fill TYPESAFE_API_KEY (recommended), GEMINI_API_KEY, HELPDESK_KEY, CANDIDATE_EMAIL
python -m qra reset --yes && python -m qra run       # --dry-run posts nothing; --judge gemini swaps the decision model
python -m qra eval                                   # also: explain <id>, status, trace <id>, audit, serve (dashboard + Try a ticket), pytest
```

## Decisions
1. **Judgments from a model, decisions in code.** Jev on TypeSafe (or `gemini-decision-1`: one setting, nothing else changes)
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
5. **Tokens are the budget.** Stating the definition once cut tokens per ticket 21%. No sampling parameters are set.

I built this with an AI coding assistant working under my direction. It wrote much of the code and tests; I set the
requirements and design rules, chose the thresholds and reviewed the labels and results.

## Reliability
- **503:** backoff with jitter, 6 tries. **429:** wait `Retry-After`; a shared limiter holds 4 req/s. **409:** never retried.
- **Exactly once:** the decision is written to a SQLite ledger (unique per ticket and epoch) before any POST. On restart a
  decided ticket is GET-checked, then the stored payload is re-sent, never regenerated. Proven on the real helpdesk: killed
  at 10 of 30, resumed, one answer each (`docs/LIVE_FAULTS.md`). Final run ledger: 30 decided, 30 sent.
- **Failures:** only an invalid API key stops a run; anything else leaves that ticket open, logged, and a re-run finishes it.
- **Security:** payment data is redacted before any model call and from every reply and log; ticket text is data, never
  instructions; no model can post.

## Eval and fix
Labels: Nordavo's 10 examples; 26 cases I wrote from the client rules; and my labels for their 30 practice tickets (written from
the rules, with the rule behind each, `eval/practice_labels.jsonl`; not their answer key, and the file holds no ticket text, the
tickets are fetched live). Prompts hold no test text: a test fails if six words of a case appear in the code.
**Jev, final code, all 66 labelled cases: 100%** on language, category, priority and action (examples 10/10, written cases 26/26
and 14/14, practice tickets 30/30), FAQ match 100%, unsafe replies 0, over-escalation 0.
Gemini (`JUDGE_BACKEND=gemini`) on the same final wording also scores 100% on all 66 (`eval/results/final-gemini.json`).
Fixes that mattered (details in `docs/ARCHITECTURE.md`): sharper "this is not X" question clauses, a `time_critical` question, and
verification at 0.7 with greetings decided in code.
**Which decision model: I recommend Jev.** It matches Gemini on every labelled case and a full run takes 38 s instead of about
184 s (Gemini's free tier caps 50k tokens a minute; that wait is never counted in latency), at about 30% lower cost.
**Final run (Jev + Flash-Lite drafts):** 30 of 30 sent, 0 errors, 38.5 s (17 handoff, 11 reply, 2 close), all 30 as labelled. Timings
vary run to run, so these are the average of 4 runs on near-final code: whole run **42.9 s** (38.5 to 46.3), per ticket p50
**5.4 s**, p90 **8.0 s**, p95 **9.7 s** (5.1 to 5.7, 6.9 to 9.3, 6.9 to 12.5); a Jev judge call about 300 ms, a Flash-Lite draft
about 1.4 s. **Cost per 100 tickets: $0.054** (average of the 4, $0.0537 to $0.0554), from logged tokens: Jev 91,090 in x $0.042/M =
$0.0038; Flash-Lite 7,935 in x $0.30/M + 2,898 out x $2.50/M = $0.0096; second opinion (3.8 Flash) $0.0026; $0.0161 for 30 tickets x 100/30.
Every judgment is a calibrated probability: all 28 per ticket are in the run log, and the ticket's confidence is stored in the ledger
with its decision, before anything is sent.

**Results in this repo:** `eval/results/final-jev.json` and `final-gemini.json` (all 66 labelled cases, per-case decision, rule row, probabilities and confidence), `eval/practice_labels.jsonl` (my labels and the rule behind each), `logs/run-20261123-110121.jsonl`
(the final run: per ticket decision, confidence, all 28 judgments, tokens and time), `eval/results/verify-ab.json` (verification
experiment), `eval/results/final-cascade.json` (retrieval comparison).

## What I would improve
**Self-hosting is the next cost step at scale.** The judge asks typed questions over one protocol and the rules are code, so an open-source
decision model behind a compatible endpoint, and a small open LLM for drafts on a vLLM runtime, can replace the hosted ones without
touching the questions or the truth table; cost then becomes GPU time per ticket. Not built or measured here (the base URL is fixed).
**Cost and latency can be cut further by giving up accuracy** (one choice question for all entries -48% tokens, cascade retrieval
-20%, no repeat draw -19%); the measured table is in `docs/ARCHITECTURE.md`.
Tune thresholds on real traffic and human edits to handoff notes, since mine come from the rules and 66 labelled cases
(`python -m qra audit` already lists decisions a person should glance at). Move to a shared Redis queue and Postgres ledger for many workers
(`docs/SCALING.md`). The client rules should state priorities for refunds and personal-data requests.
