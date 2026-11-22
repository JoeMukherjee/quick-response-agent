# Build plan (supersedes PRD sections 5-8 where they differ)

Checks against Nordavo's 10 labelled examples, not verdicts. Deadline: send the email by ~4 PM Monday 23 Nov, not at 6:58.

## 0. Spike first (20 min, commits nothing)
Run one real Agno `Agent` call with `gemini-3.8-flash` and a Pydantic `output_schema`, and one Decision call using `criteria` on a noul and a choice. The new SDK moved generation to the Interactions API and Agno's Gemini class may still call `generateContent`. If Agno fails, tell the user before building around it.

## 1. Decision logic
- Action never comes from category. E01 (bug), E05 (feature request), E07 (billing) all get a reply. Action = hard rules + FAQ coverage.
- "Money or contract" noul wording: "requests money back, disputes a charge, or asks to change or end the contract", never "mentions money" (E02/E07 are P4, E03 is P2). "Security or legal risk" (P1) means an incident or a threat, not a routine GDPR request (E09 is P2). Put these confusion cases into the criteria.
- Drop "exactly one FAQ entry > 0.7". Two stages: (1) triage call with no FAQ text in the state; each FAQ entry's text lives in its own coverage question's instructions. (2) Only if no hard rule fired and at least one entry scored >= 0.5: second Decision call, state = ticket + candidate entries' text, one noul "do these entries answer every question or request in the message?", reply only if >= 0.7.
- Language: API accepts nl/en/de only. If the language choice returns `other`, hand off and fill the field with the top-scoring of nl/en/de.
- Close only on empty (checked in code) or spam >= 0.85 with every hard-rule and FAQ noul low. Closing a real customer is a safety failure.
- A Decision or Flash call that fails after retries is an error, not a "no": leave the ticket open, log it, a re-run picks it up. No auto-handoff.

## 2. Idempotency (state the guarantee in the README)
- Append-only JSONL ledger, fsync per write. `decided` (full payload) before the POST, `sent` after a 2xx. Tolerate a truncated last line.
- For a ticket marked `decided`, GET /tickets/{id} before any POST; if handled, mark `sent`. After a crash re-post the stored payload, never regenerate.
- 503: safe to retry (nothing saved). Timeout/reset: outcome unknown, GET first. 409: never retry; GET, mark reconciled, log. 429: honour Retry-After; client limiter ~4 req/s.
- `reset` clears the ledger. Sequential processing (concurrency is the first cut).

## 3. Gemini limits
Token bucket keyed on each call's reported `usage`; catch RESOURCE_EXHAUSTED/429 with backoff. Check free vs paid tier (affects pacing and the README cost claim). Free tier: 50k TPM.

## 4. Redaction and verification
- Redact before any model and before logs: IBANs incl. spaced forms, card numbers (13-19 digits, spaces/dashes) with a Luhn check; pass a `contained_payment_data` flag into the state.
- Deterministic reply checks: no payment pattern; any quoted or arrowed UI path appears verbatim in faq.md; every number appears in the FAQ or the ticket or is a product of one of each (E07: 25 x EUR 6).
- Decision 1 verify nouls: reply supported by the entries (arithmetic allowed); reply in ticket language; no promise the entries do not make (F10: no delivery dates).
- 3.8 Flash back-translation for logs and human review only.

## 5. Layout (a live change should be one line)
`qra/` : config.py, helpdesk.py (typed errors), ratelimit.py, ledger.py, redact.py, questions.py (all Decision wording), judge.py (Decisions wrapper), policy.py (pure function, thresholds in one declarative table), drafting.py (Agno agents), verify.py, pipeline.py, __main__.py. Commands: `run`, `run --dry-run`, `eval`, `explain <id>` (every probability plus the truth-table row that fired), `status`, `reset`.

## 6. Eval honesty
- Headline eval = the 10 examples from GET /examples, dry-run, cached in state/. Report per-field accuracy, must-handoff recall (E03, E06, E09), close precision, grounding pass rate.
- The fix is a general wording or policy change, committed separately from the "after" result.
- Author-labelled safety unit tests (injection, IBAN, disguised refund, empty ticket, German ticket), labelled as ours.
- Never tune on the 30 practice tickets. A test fails if `T0\d\d` or `E\d\d` ids appear in `qra/`.
- Cost per 100 tickets = logged tokens x prices (Decision 1 $0.10/1M input; 3.8 Flash input $0.75/1M, output price from gemini-pricing.md).

## 7. Tests
Fake helpdesk on `httpx.MockTransport` injecting 503, 429+Retry-After, 409, timeout-after-save. Crash test: decide, kill before POST, resume, assert exactly one POST. Unit tests for policy.py and redact.py.

## 8. Build order (about 7-8 h, commit after each step)
1 spike 20m; 2 helpdesk client + limiter + ledger + fake-helpdesk tests 60m; 3 redaction 30m; 4 questions/judge/policy/explain 60m; 5 eval baseline 45m; 6 drafting + coverage + verify 60m; 7 fix then eval after 30m; 8 practice end-to-end with a deliberate Ctrl-C and resume 30m; 9 README 45m. Then final reset + run + commit logs; user flips repo public (or grants `nordavo-hiring`); email by ~4 PM Monday.

Cut: concurrency, flash-lite, Score questions, Agno skills unless step 6 shows a real need.
