# Quick Response Agent: PRD

Product: a support-ticket agent for the fictional Dutch SaaS Roosterly (shift planning). Built for the Nordavo take-home. Framework: Agno. Models: Gemini.

## 1. Problem
A three-person support team gets ~200 tickets a week, almost all Dutch. The agent must answer what the help centre (`faq.md`) already covers and hand everything else to a human with a note they can act on in under a minute. A wrong customer-facing answer is worse than an escalation; escalating everything is also a failure.

## 2. Goals and non-goals
Goals, in the order Nordavo scores them: (1) safe, (2) useful FAQ replies in the right language, (3) robust, (4) measured, (5) clearly explained.
Non-goals: UI, multi-tenant support, learning from tickets, anything beyond the 12 FAQ entries.

## 3. Users
- Customer (Dutch, sometimes English/German): receives a reply only if the FAQ fully covers the question.
- Support agent: receives handoffs with a short English note (what, why, what to do next).
- Reviewer (Nordavo): reads code, history, logs, README; scores the final run; watches a live run on unseen tickets.

## 4. Functional requirements
FR1 Read all open tickets (paginate `GET /tickets?limit=10&cursor=`, follow `next_cursor`).
FR2 Per ticket decide: `language` (nl/en/de), `category` (bug/billing/feature_request/question/other), `priority` (P1-P4 + reason), `action` (reply/handoff/close), `confidence`.
FR3 `reply`: draft in the customer's language, grounded only in cited FAQ entries (ids recorded in the log).
FR4 `handoff`: English note: summary, why escalated, suggested next step, key facts (plan, ids). No payment details.
FR5 `close`: only for spam or empty tickets.
FR6 Hard rules (deterministic, cannot be overridden by the model): refunds/disputed charges, cancellations/contract changes, GDPR/AVG requests, possible security incidents, legal threats, churn threats always go to a human. The model may escalate more, never less.
FR7 Never repeat card numbers or IBANs in a reply, handoff note or log; redact before the model sees them and before logging.
FR8 Ticket text is untrusted data: delimited, never executed as instructions; injection attempts escalate.
FR9 One log line per ticket in `logs/` (JSONL): id, language, category, priority, action, confidence, faq_ids, model, latency, tokens, HTTP outcome. No payment data, no raw body.
FR10 One command: `python -m qra run`. Other commands: `eval`, `reset`, `status`.

## 5. Reliability requirements
- 503: exponential backoff with jitter, max 6 tries. Actions are safe to retry because a 503 means nothing was saved.
- 429: honour `Retry-After`; client-side limiter at 4 req/s so 429s are rare.
- 409: never retry; mark handled in the ledger; log it.
- Crash safety and no double answers: a local ledger (`state/ledger.jsonl`) records `decided` before any POST and `sent` after. On start the agent (a) skips tickets marked `sent`, (b) re-reads `GET /tickets/{id}` for any ticket marked `decided` but not `sent` to see if the server already handled it, (c) only then posts. The decision is reused from the ledger, not regenerated, so a re-run cannot send a different answer.
- Secrets: environment variables only; `.env.example` committed, `.env` ignored.

## 6. Architecture (Agno) - judgments from Decision 1, decisions in code
Design rule taken from the Gemini and TypeSafe skills on System-1 models: `gemini-decision-1` only returns calibrated probabilities (0 output tokens, no text, no tools), so it answers narrow typed questions and ordinary code holds the decision logic as a truth table. We never ask it "should this be escalated?".

```
helpdesk client (retry, limiter, 409) <-> ledger
ticket -> redact payment data -> state (named JSON: subject, body, plan, channel, help_centre.F01..F12)
  -> ONE Decision 1 call, ~30 atomic questions in parallel, same state:
       language (choice nl/en/de/other); category (choice); is_spam_or_empty (noul);
       6 hard-rule noul (refund/dispute, cancel/contract, personal-data, security, legal, churn);
       priority inputs (noul: many_users_blocked, security_or_legal_risk, one_customer_blocked,
                        money_or_contract, workaround_exists); tries_to_instruct_the_assistant (noul);
       12 FAQ-coverage noul (customer asks what help_centre.Fxx answers) + asks_beyond_faq (noul)
  -> truth table in code (thresholds 0.7 / 0.3, the band between = "unsettled" -> handoff):
       any hard rule > 0.3 -> handoff ; injection > 0.3 -> handoff ; spam > 0.7 -> close ;
       exactly one FAQ entry > 0.7 and asks_beyond_faq < 0.3 -> reply ; else handoff
       priority from the five inputs (P1 > P2 > P3 > P4 by rule) ; category from the choice
  -> Agno Draft agent (Gemini 3.8 Flash): writes the reply in the ticket language, may use only the matched FAQ entries
  -> Verify: Decision 1 noul over {reply, entries}: reply_uses_only_entry_facts, reply_in_ticket_language,
     reply_repeats_no_payment_data; plus a 3.8 Flash back-translation to English stored in the log
     (fail -> handoff; the draft is kept in the handoff note)
  -> Agno Handoff-note agent (3.8 Flash, English): what, why escalated, next step
  -> merge (most conservative wins) -> POST -> log
```
- Confidence is not the gate on its own. Per the TypeSafe guidance, confidence measures how concentrated the answer is, not how likely it is correct; we gate on the yes-probabilities of specific statements and tune the thresholds on the labelled examples, not on the 30 practice tickets.
- Question wording lives in one module (`qra/questions.py`) so it can be audited and edited. One judgment per question, plain wording, positive phrasing, option keys that describe content, a separate yes/no scope check next to every choice, no wording tuned to a single ticket.
- State design: only the fields the questions need, named fields, ticket text sent as data inside a JSON field, never concatenated into instructions.
- The runner does every POST. No model has a tool that can reach the helpdesk.
- Agno: `Agent`s for drafting and handoff notes with Pydantic `output_schema`; Agno Skills (`skills/`) only if we find we need references, templates or scripts for the agents after the first working version; otherwise we skip them. Install: latest `agno` and `google-genai` in the project `.venv` (pinned in `requirements.txt`).
- Model routing (env-configurable): `gemini-decision-1` for all judgments and verification (v1beta, `client.decisions.create`), `gemini-3.8-flash` for drafting and notes, a flash-lite model is optional, not part of the design: use it only if cost or latency measurements show a need (the account has no 3.8 Flash-Lite text model; `gemini-3.5-flash-lite` is the closest). Back-translation defaults to 3.8 Flash. Cost: Decision 1 bills input tokens only, so the ~30 questions share one billed state.

## 7. Dutch handling
Decision 1 reads Dutch directly, so no translate-first step (saves a call, avoids losing nuance). Drafting is in the customer's language. Because we cannot read Dutch, correctness is checked three ways: (1) Decision 1 noul checks that the reply uses only facts from the cited entries and is in the ticket language, (2) a back-translation to English stored in the log lets a reviewer read it, (3) string assertions that every UI term in the reply (e.g. "Medewerker toevoegen") and every number or price appears in the FAQ text.

## 8. Eval
- Source of truth: the 10 labelled examples from `GET /examples`. Labels come from Nordavo, never from a model. The 30 practice tickets are used only for the final run, not for tuning.
- Metrics: language, category, priority, action accuracy; safety (must-handoff recall, target 100%); over-escalation rate; reply grounding pass rate.
- Fix loop: run baseline, pick the largest failure class, fix it in the questions or the truth table (never by overriding answers in code for one ticket), rerun, report before and after. Check each change on cases not used while tuning.
- Cost per 100 tickets from logged token usage x public prices, calculation in the README.
- Probe Decision 1 first (a few representative, hard and negative Dutch cases) before building on it; keep each request payload for reruns.

## 9. Deliverables
Repo `quick-response-agent` (private until the final check, then public or shared with `nordavo-hiring`): code, eval, logs, `.env.example`, README <= 1 page with the five required headings (How to run, Decisions, Reliability, Eval and fix, What I would improve). Commit as we work; Nordavo reads the history.

## 10. Milestones (deadline Mon 23 Nov, 6:59 PM; target done by Mon midday)
1. Scaffold, API client, ledger, tests against the live helpdesk (today).
2. Rules gate + triage + eval baseline.
3. Draft + verifier + fix + eval after.
4. Logs, README, clean run (`reset` then `run`), repo public, reply to Judith.

## 11. Risks
- Decision 1 thresholds are untuned: calibrate on the 10 labelled examples; if a question type misbehaves on Dutch, split it or move that judgment to 3.8 Flash.
- Decision 1 treats input as untrusted but ticket text can argue for its own classification: explicit criteria, an injection question, and the code truth table (never the model) decides the action.
- Over-escalation: tune the confidence threshold against the eval, not by feel.
- Hand-typed answers disqualify: no ticket-specific text anywhere in the code.
- The shared API key appears in the email; it never enters the repo.
