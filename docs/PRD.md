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

## 6. Architecture (Agno)
```
helpdesk client (retry, limiter, 409) <-> ledger
tickets -> redact -> rules gate (regex, FAQ-rule keywords)
        -> Triage agent (Gemini Decision 1, structured output)
        -> Draft agent (Gemini 3.8 Flash, FAQ-grounded)  [only if action=reply]
        -> Verifier (Gemini 3.5 Flash-Lite): back-translate Dutch reply to English,
           check every claim against cited FAQ text; fail -> handoff
        -> policy merge (most conservative wins) -> post -> log
```
- Agno `Agent`s with Pydantic `output_schema`; FAQ loaded as the only knowledge; no tools that can act on the helpdesk (the runner does the POSTs, so the model cannot send anything by itself).
- Agno Skills (`skills/`): `faq-grounding`, `handoff-style`, `dutch-check`, each with SKILL.md, references and templates; scripts for the eval and the Dutch back-translation check.
- Model routing: Decision 1 for the judgement-heavy triage, 3.8 Flash for drafting, Flash-Lite for cheap verification. Model ids are env-configurable. Note: the brief names "3.8 Flash Lite"; the account's model list only shows a 3.8 lite TTS model, so the generation lite model used is `gemini-3.5-flash-lite` (to confirm).

## 7. Dutch handling
Work in Dutch directly for triage (modern models read it well; translating first loses nuance and costs a call), draft in the customer's language, and verify without reading Dutch: back-translate the draft to English with a separate model call and check it against the FAQ English meaning. Menu names stay exactly as the FAQ writes them (e.g. "Medewerker toevoegen"). Assertions: language of the reply equals the ticket language; every UI term in the reply appears in the FAQ; no unsupported numbers or prices.

## 8. Eval
- Source of truth: the 10 labelled examples from `GET /examples`. Labels come from Nordavo, never from a model.
- Metrics: language, category, priority, action accuracy; safety (must-handoff recall, target 100%); over-escalation rate; reply grounding pass rate (verifier plus string checks).
- Fix loop: run baseline, pick the largest failure class, fix it, rerun, report before and after in the README.
- Cost per 100 tickets: tokens from logs x public per-token prices; calculation shown in README.

## 9. Deliverables
Repo `quick-response-agent` (private until the final check, then public or shared with `nordavo-hiring`): code, eval, logs, `.env.example`, README <= 1 page with the five required headings (How to run, Decisions, Reliability, Eval and fix, What I would improve). Commit as we work; Nordavo reads the history.

## 10. Milestones (deadline Mon 23 Nov, 6:59 PM; target done by Mon midday)
1. Scaffold, API client, ledger, tests against the live helpdesk (today).
2. Rules gate + triage + eval baseline.
3. Draft + verifier + fix + eval after.
4. Logs, README, clean run (`reset` then `run`), repo public, reply to Judith.

## 11. Risks
- `gemini-decision-1` behaviour is unknown: test it on the 10 examples before committing to it; fall back to 3.8 Flash.
- Over-escalation: tune the confidence threshold against the eval, not by feel.
- Hand-typed answers disqualify: no ticket-specific text anywhere in the code.
- The shared API key appears in the email; it never enters the repo.
