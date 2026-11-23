# Architecture

This document explains how the Quick Response Agent is built, why each choice was made, what went wrong along the
way, and what each change measurably did. Every number below was measured in this repository; where a number comes
from a single run, the run is named. Companion pages: [`PRD.md`](PRD.md) (requirements), [`TOKENS.md`](TOKENS.md)
(token experiment), [`SCALING.md`](SCALING.md) (scale and security), [`LIVE_FAULTS.md`](LIVE_FAULTS.md) (faults
provoked on the real helpdesk).

## 1. The problem and the three things that decide the design

Roosterly's helpdesk has open support tickets, some in Dutch. The agent must give each ticket exactly one outcome:
reply in the customer's language, hand off to a human with an English note, or close. It runs against a real API
that returns 503s, rate-limits at 5 requests a second, and accepts one answer per ticket.

Three facts drive every decision:

1. **A wrong reply is worse than a handoff.** A reply that invents a price, a link or a policy is a harm; a handoff
   costs a human a minute. The system is built so that every doubt resolves toward a human.
2. **The model is a judge, not a decider.** `gemini-decision-1` returns calibrated probabilities for narrow
   questions and nothing else (no text, no tools, zero output tokens). That is a feature: it can be audited. So the
   model answers questions and ordinary code makes the decision.
3. **Two external ceilings, not the CPU, set throughput.** The helpdesk allows 5 requests a second and the free model
   tier allows about 50,000 Decision tokens a minute. More threads cannot beat either.

## 2. System at a glance

```
                       +---------------------------- telemetry (events, spans, faults) ----------------------------+
                       |                                                                                           v
 helpdesk  --GET-->  queue  -->  worker threads (N)  -->  redact -->  JUDGE  -->  policy  -->  draft + verify  -->  deliver  --POST--> helpdesk
 (Roosterly)         bounded     one ticket at a time      payment      Decision 1   truth       Flash writes,         ledger:      (409 never
                                 shared limiter 4 rps      data out     ~30 typed    table in    Decision 1 checks     decided,     retried)
                                 shared token budget                    questions    code        every sentence        then sent
                                                                        (+cache)     (pure)
                                                                           |
                                                                    borderline? ask once more,
                                                                    keep the safer answer
```

Package layout (`qra/`): `core` (config, errors, FAQ, models), `decision` (questions, judge, policy), `agents`
(drafting), `safety` (redaction, checks, verification), `infra` (helpdesk client, ledger, cache, rate limiter, work
queue), `runtime` (pipeline, runner, service), `evaluation` (eval, cost, experiments, noise), `telemetry` (events,
analytics, failure taxonomy), `server` (dashboard API and the built React app). `frontend/` holds the React source.

## 3. Decision architecture: judgments from the model, decisions in code

**Problem.** Asking a model "should this be escalated?" gives an answer nobody can audit and that moves with the
wording. **Choice.** The judge answers about thirty narrow questions per ticket in one call (language, category, is it
spam, six client rules, five urgency inputs, a prompt-injection check, twelve help-centre coverage questions). A pure
truth table in `qra/decision/policy.py` turns the probabilities into an action.

| Rule | Behaviour | Why |
|---|---|---|
| Client rules (refund, cancellation, personal data, security, legal, churn) | Escalate from p >= 0.3; they can only escalate | Asymmetric cost: a missed escalation is expensive, an extra one is cheap |
| P1 and P2 priority | Always a handoff | A human owns urgent and high-impact tickets |
| Probability between 0.3 and 0.7 | "Unsettled": never ends in a reply | Doubt resolves to a human |
| Prompt-injection probability >= 0.5 | Handoff | Ticket text is data, never instructions |
| Spam probability >= 0.85 | Close | Closing is the one irreversible-feeling action, so it has the highest bar |
| Language outside Dutch, English, German | Handoff | The agent only drafts where it can verify |

Thresholds live in one table (`THRESHOLDS` in `qra/core/config.py`) and were fixed before the eval ran, then not
tuned on the 30 practice tickets. Confidence is not used as a gate: it measures how concentrated an answer is, not
how likely it is correct, so the gates use the yes-probability of specific statements instead.

**Result.** Unsafe replies: **0** across all 36 labelled cases, in every variant and every run we measured. Handoff
recall on the labelled hard cases: 3/3 on Nordavo's examples and 15/15 on the authored cases.

## 4. Reply safety: draft, then prove it

**Problem.** A fluent reply can still state a number that is not in the help centre, answer in the wrong language,
or repeat a card number. **Choice.** `gemini-3.8-flash` (through an Agno agent with a typed output schema) drafts in
the customer's language from the matched help-centre entries only. Then three independent gates run:

1. **Deterministic checks** (`safety/checks.py`): every number, link and menu name in the reply must come from the
   help centre or the ticket; derived values (25 x EUR 6) are allowed; no payment data.
2. **Per-sentence support check**: the judge answers, for each sentence, whether the matched entries support it.
3. **Back-translation** to English, stored in the log for human review.

A reply that fails any gate becomes a handoff with the draft attached for the human. **Cost of the safety:** two extra
Decision calls per reply, a fraction of a cent at $0.10 per million input tokens. Payment data is redacted before any model call and
from every log line.

## 5. Delivery: exactly one answer per ticket, even through crashes

**Problem.** The helpdesk accepts one answer per ticket. A retry after a timeout can double-answer; a crash after
the model call can lose or regenerate the answer.

**Choice.** A crash-recovery ledger in SQLite (WAL, `synchronous=FULL`):

- The decision is persisted **before** any POST (`decided`), so a crash never regenerates a different answer.
- `UNIQUE(epoch, ticket, state)` makes the database itself refuse a second decision for a ticket.
- Before any retry the client does a GET: if the ticket is already handled it records `reconciled` and does not send.
- A 409 is never retried. A timeout after a POST is an *unknown outcome*, resolved by GET, never by resending.
- A lock file (`O_CREAT|O_EXCL`) allows one runner per ledger. Resets start a new epoch so earlier records do not
  skip reopened tickets.

**Evidence on the real helpdesk** (`LIVE_FAULTS.md`): a 409 was recorded as reconciled and not retried; a burst of 25
raw requests drew 14 responses of 429 and the client still completed all 25 by honouring `Retry-After`; with a 50 ms
timeout the server saved our answer but we never saw the reply, and the GET-first rule recorded it as reconciled with
no second answer; a hard kill after 10 of 30 tickets left a stale lock that was reported clearly, and the resume
handled the remaining 20 with 0 errors and exactly one answer per ticket.

## 6. Failure model: the service stops for one reason only

A failure taxonomy (`telemetry/taxonomy.py`) names every way a run can go wrong and what the system does about it.

| Class of failure | Handling | Visible in the dashboard |
|---|---|---|
| Invalid API key (`FatalAuthError`) | The only thing that stops a run, with a clean message | Run status "stopped (invalid key)" |
| Helpdesk 503 | Exponential backoff with jitter, 6 tries | Fault counter, retry events |
| Helpdesk 429 | Wait `Retry-After`; shared limiter keeps us under the ceiling | Fault counter, request-rate chart |
| Helpdesk 409 | Never retried; recorded as reconciled | Fault counter |
| Model transient error | Ticket left open, logged; a re-run finishes it | Fault counter |
| Safety block by the model | Permanent for that ticket: handoff | Fault counter |
| Lock held, stale lock, interrupted run | Detected and reported; stale runs show "interrupted", never "running" forever | Run status |
| Telemetry itself failing | Swallowed and counted; the service carries on | Dropped counter |

Unknown failure classes are never hidden: they appear in the table under their own name. The server never lets a
request handler take the process down.

## 7. Concurrency and back-pressure

Work is I/O bound, so worker threads (default 4) pull tickets from a bounded `WorkQueue`. Two shared governors stand
between the workers and the outside world: a thread-safe request limiter (4 requests a second against the helpdesk's
5; it also backs off on a 429) and a token budget window (45,000 Decision tokens a minute against the tier's 50,000).

**Honest result.** Four workers already saturate both ceilings, so more workers only buy 429s. Observed in one 20-ticket
dry run with 4 workers (before the token reduction below): about 14.7 tickets a minute; per-stage medians of 2.1 s (plan), 3.7 s (draft), 3.6 s (note),
0.5 s (verify); judge calls 0.56 s median, 1.13 s at the 95th percentile. The long 95th-percentile "plan" bar (48 s) is
workers waiting on the token budget, which is exactly what the lane chart in the dashboard makes visible. The next
section is the response to that.

The queue is a seam: `LocalQueue` today, Redis or SQS tomorrow (`SCALING.md`), with the ledger moving to Postgres.

## 8. Cost and token engineering

Decision 1 bills the whole input on every call, has no context cache and no batch mode, and costs $0.10 per million
input tokens; Flash costs $0.75 in and $3.75 out. The money is small; the **tokens-per-minute ceiling is the real
constraint**, so cost work is throughput work.

**Where the tokens went.** About 98% of each triage request was identical for every ticket. The twelve help-centre
questions were 65% of it, and more than half of that share was the same criteria sentence repeated twelve times.

**Experiment.** The same 36 labelled cases ran through three phrasings of the help-centre question, three times each,
cache off, never touching the 30 practice tickets:

| Variant | Tokens per ticket | Change | Unsafe replies | Action: Nordavo / authored | Over-escalation (authored) |
|---|---|---|---|---|---|
| `per_entry` (original) | 3,779 | baseline | 0 | 9/10, 24/26 | 2/9 |
| `shared_definition` (**adopted**) | 2,979 | **-21%** | 0 | 9/10, 25/26 | 1/9 |
| `choice` (one question over entry summaries) | 1,966 | -48% | 0 | 8/10, 23/26 | 3/9 |

`choice` is the cheapest and the least accurate, because one question splits probability between entries, so it was
rejected. `shared_definition` states the definition once and was cheaper *and* slightly more accurate. At 2,979
tokens a ticket, the Decision share of a 100-ticket run is about $0.03 to $0.04; the Flash share is computed from
logged tokens by `qra/evaluation/cost.py` and reported in the README from the final run (never estimated).

**What the repeats taught us.** The three repeats of each variant were identical, so within one time window the judge
is deterministic. Runs 20 to 30 minutes apart did differ (an early run scored 10/10 on Nordavo's examples for two
variants, the later ones 9/10), so the service's answers drift over time. That motivated resampling (section 10) and
the policy of comparing variants only within one window.

## 9. A bug the experiment exposed, and the cache that came out of it

**Problem.** The decision cache was silently disabled. An empty cache is falsy in Python because it defines
`__len__`, so `if cache:` skipped every write. Nothing failed; it just never cached. Found while checking whether the
caches were really working. **Fix** (`is not None`) with a regression test.

Rebuilt as a bounded store (`qra/infra/cache.py`), because an unbounded cache is a slow outage:

- **Bounded** by bytes (64 MB) and entries (50,000).
- **Frequency-aware eviction**: the least-used entries go first, with time-based decay (an hour) so yesterday's hot
  entries cannot hog the cache forever. An earlier version decayed on every eviction pass and erased popularity; a
  test caught it, and the decay became time-based.
- **O(1)** hot path: a 256-entry in-memory dictionary in front of a `WITHOUT ROWID` SQLite table; hit counters are
  batched (flushed every 64) so a read does not become a write.
- **Reverse index by tag** (the model that produced the entry), so changing a model invalidates only its entries.
- **Operable**: `python -m qra cache stats|clear`, a Response Cache section in the dashboard's Bench dialog with a
  confirm-on-second-click Clear, and `GET/POST /api/cache`. A clear reaches the in-memory tier of every open cache.

The Decision answers are cached by `sha256(model, input, questions)`, so re-running the eval after a threshold or
truth-table change costs nothing; only reworded questions spend tokens.

## 10. Resampling borderline tickets and measuring noise

**Problem.** A probability within a hair of a threshold can land on either side between runs. **Choice.** When a
deciding probability is within 0.1 of its threshold, the pipeline asks the same questions once more with a slightly
different input and combines the draws toward the safer outcome: escalation signals take the larger probability;
signals that enable a reply or a close take the smaller. Clear tickets never pay for the second call. The Judge node
in the dashboard counts resampled tickets.

`python -m qra noise` asks every labelled case several times with the variation resampling uses (cache off) and
reports, per question, how much the probability moves, how often draws land on opposite sides of the threshold, and how
many of those crossings the 0.1 margin would have flagged from the first draw alone. See section 18 for the result.

## 11. Eval and the fix it found

**Honest eval.** 36 labelled cases: Nordavo's 10 examples plus 26 cases written from the client rules before anything
ran. The 30 practice tickets are never used for tuning.

| Metric | Baseline | After the fix |
|---|---|---|
| Unsafe replies | 0 | 0 |
| Action correct, Nordavo examples | 9/10 | **10/10** |
| Over-escalation, Nordavo examples | 1/6 | **0/6** |
| Help-centre match, Nordavo examples | 5/6 | **6/6** |
| Action correct, authored cases | 23/26 | 24/26 |

**Cause.** The help-centre questions asked whether an entry "answers" the message. A feature request cannot be
answered by any entry, so it was escalated. **Fix.** One wording change: ask whether the entry is the *appropriate
response*. No threshold changed. The remaining authored miss ("how do I upgrade" trips the contract rule at 0.51) was
left as is, because it errs toward a human.

## 12. Telemetry: every stage, call and failure is an event

Telemetry can never break the service: `emit` swallows every error and counts what it dropped. No ticket text or
payment data is stored, only ids, stages, outcomes, error classes, timings and token counts. A pipeline stage is a
*span* with a start event (so work still in flight is visible) and an end event with its latency. Event kinds:
`run_start`, `run_end`, `span_start`, `span`, `http`, `model`, `fault`, `ticket`.

The dashboard reads the same rows the CLI reads, so the numbers in the UI are the numbers in the logs.

## 13. Indexing: no lookup is a scan

| Store | Question | Structure | Cost |
|---|---|---|---|
| Ledger | Has ticket X reached state S? | `UNIQUE(epoch, ticket, state)` plus an in-memory map | O(1) memory, O(log n) disk |
| Ledger | Which tickets are sent; counts per state | In-memory reverse index state -> tickets; `(epoch, state, ticket)` on disk | O(1), no query |
| Cache | Entry for a key | `WITHOUT ROWID` primary key plus hot dictionary | O(1) hot, O(log n) cold |
| Cache | Which entry to evict | `(ns, hits, last_used)` | First index row |
| Cache | Entries by producing model | Partial index on tag | O(matching entries) |
| Telemetry | One run in order; only what is new | `(run_id, id)` | Range read of new rows |
| Telemetry | Counts by kind within a run | `(run_id, kind)` | Range count |
| Telemetry | Everything about one ticket | Partial reverse index `(ticket, id)` | 1.4 ms on 100,000 events |
| Telemetry | Recent occurrences of a failure class | Partial reverse index on faults | Newest first, bounded |
| Telemetry | List runs, latest run | `runs(seq, run_id)` | O(runs), independent of events |

Tests read `EXPLAIN QUERY PLAN` and fail if any of these lookups becomes a scan. The ledger's in-memory indexes are
rebuilt from the database on open, and a refused insert re-reads that ticket from disk, so a second process writing the
same file can never leave them wrong.

**Measured on a synthetic run of 100,000 events:**

| Operation | Before | After |
|---|---|---|
| `latest_run()` (called on every snapshot) | 87 ms | **1.5 ms** |
| `runs()` | 66 ms | **6 ms** |
| Snapshot with nothing new | 1,196 ms | **146 ms** |
| Trace one ticket across all runs | (full scan) | **1.4 ms** |

**The incremental reader.** The dashboard asks for a snapshot every 700 ms. The old reader re-read and re-parsed every
event of the run each time, twice. The new one keeps an in-memory index per run, loads only events added since its
last call, parses each event once, and files it by kind, stage, failure class and ticket. Output is identical to the
old implementation on the recorded runs (compared field by field). The remaining 146 ms at 100,000 events is payload size,
which is bounded to recent history (800 spans, 300 seconds of request rates, 90 minutes of token counts) while the
summary numbers still cover the whole run. Runs older than the newest 50 are pruned when a new run starts.

## 14. Web delivery: fast first paint, and a page that stays still

| Measure (same machine, same page) | Before | After |
|---|---|---|
| First contentful paint | 576 ms | **252 ms** |
| Page load | 638 ms | **234 ms** |
| JavaScript transferred | 257 KB | **79.5 KB** (gzip) |
| CSS transferred | 26 KB | 5.2 KB (gzip) |
| Repeat visit | re-downloaded | **0 bytes** (hashed files cached as immutable) |

How: built files carry content hashes, so they are served with `Cache-Control: immutable` for a year; they are held in
memory with a precomputed gzip body and an ETag; `index.html` is `no-cache` with an ETag, so a reload is a 304; live
JSON is compressed when large. The live stream (server-sent events) sends a snapshot only when something changed and a
three-second heartbeat otherwise. Live data is never cached (`no-store`).

**The flicker.** Hovered elements flickered and their animations replayed forever. Cause: React ViewTransitions on live
table rows snapshot the whole page on every data update, and an update arrives every 700 ms during a run. Fix: removed
view transitions from live data. Hover motion moved to an inner element with a stationary hit area (an element that
moves out from under the pointer un-hovers itself). Measured afterwards: zero running animations during a six-second
hover on the busiest component. A related fix removed an animated ring that left ghost frames outside its box.

**Interface.** React 19.3, Vite 8, TypeScript 7, all current. Entrance animation per component, a unique hover motion
per component, custom cursors by area, light and dark themes, a pause-motion control, `prefers-reduced-motion`
respected, table views of every chart, `aria-live` status regions, a skip link. The chart palette was checked with the
data-visualisation validator (colour-vision separation, contrast), and animation uses compositor-only properties.

## 15. Security

- **Network.** The dashboard binds to loopback. A one-time URL token becomes an HttpOnly, SameSite=Strict cookie and
  disappears from the address bar. The Host header is checked against an allow-list (blocks DNS rebinding). Every
  mutating request needs a custom header, so a cross-site form cannot post. Static paths are resolved and confined to
  the build directory. A strict Content-Security-Policy and `nosniff` ship on every response. Request parameters used
  in lookups are validated against a safe pattern.
- **Outbound.** The helpdesk client is HTTPS only and follows no redirects, so a redirect cannot carry the key to
  another host.
- **Secrets.** Keys come from the environment; `.env`, raw inputs and resources are git-ignored; a test greps the
  source, tests and docs for key patterns and for ticket identifiers.
- **Data.** Payment data is redacted before any model call and from every reply and log line. Ticket text is passed as
  data inside a JSON field, never concatenated into instructions, and no model has a tool that can post: the runner does
  every POST.

## 16. Quality: how we know it works

185 automated tests cover: the helpdesk client (retry, 429, 409, timeouts), delivery and crash recovery, the policy
truth table, redaction and checks, the runner (including a fatal key stopping the run), the cache (eviction, decay,
tags, legacy stores, clearing), resampling, telemetry and indexes (including query-plan assertions), the server
(guards, compression, ETags, 304s, cache API), and hygiene (no secrets, no ticket identifiers in code). The eval and
the experiments are scripts (`python -m qra eval|experiment|noise`) whose outputs are committed under `eval/results/`.

## 17. Scalability: what changes at 100 times the volume

Today: one process, SQLite, in-memory queue, four threads, bounded by two external ceilings. The seams for growth are
already in the code:

| Need | Change | Code that stays |
|---|---|---|
| More tickets than one process can pace | Swap `LocalQueue` for Redis or SQS; several worker processes | Pipeline, policy, delivery rules |
| Several workers writing decisions | Move the ledger to Postgres; keep `UNIQUE(epoch, ticket, state)` | The whole delivery protocol |
| A higher model quota | Raise the token budget constant; the limiter is shared | Everything |
| Cold start cost | Warm the cache from the shared store | Cache interface |
| Long history of events | Already bounded: 50 runs, indexed, incremental reads | Reader and dashboard |

What does not change at scale is the economics: judgments are cheap input tokens (about 3,000 a ticket), and every
safety gate is a handful of extra judge calls rather than a larger model.

## 18. Noise measurement

Measured with `python -m qra noise --draws 3` on the 36 labelled cases (315,180 tokens, cache off, adopted
`shared_definition` wording); raw output in `eval/results/noise.json`.

- Across the 36 cases and every gated question, **9 threshold crossings** occurred (draws of the same case landing on
  opposite sides of a gate). The 0.1 resample margin would have flagged **7 of the 9 (78%)** from the first draw alone.
- Typical movement is small: the mean standard deviation of every question is under 0.025.
- The noisy tail is where it matters. The questions that crossed a gate were `cancel_or_contract` (3 crossings, all
  caught), `personal_data_request` (2, both caught), `refund_or_dispute` (1, caught), `faq_F04` (1, caught; its
  worst-case standard deviation was 0.17) and `customer_blocked` (2, neither caught).
- The two misses are swings larger than the margin: the first draw was more than 0.1 from the 0.5 gate and a later
  draw crossed it. A wider margin would catch them at the price of more second calls; `customer_blocked` feeds the
  urgency estimate, which already escalates on its own side, so we kept 0.1 and recorded the limit here rather than
  tuning the margin to this one measurement.
- The hard rules (the ones that can only escalate) are quiet: mean standard deviation 0.009 to 0.018, and all of
  their crossings were caught.

Resampling therefore removes most, not all, run-to-run flips at the gates, and every flip it removes is resolved toward
the safer outcome.

## 19. What changed after the first eval, and what each change measured

The table in section 11 is the first fix. Everything below came later, in this order, each change measured before it was kept.
Rules in plain language, with every threshold, question and truth-table row: [`RULES.md`](RULES.md) (tests fail if any is undocumented).

| Change | Why | Result |
|---|---|---|
| Priority rewritten from the brief's four definitions (P1 many users or security/legal, P2 one customer blocked or money, P3 workaround, P4 no urgency) | priorities had been set by a rule of thumb, not by the brief | Nordavo examples priority 9/10 -> 10/10; a how-to the help centre answers is P4 |
| `customer_blocked` reworded to "something the customer was using is broken, so they are stuck now" | a how-to scored 0.62 "blocked" and became P3 | the how-to example moved to P4 |
| Sharper category, injection and spam criteria; `other` names security reports | a security report was filed as `question` | category 10/10 on the examples; authored action 23/26 -> 26/26 |
| Second-opinion agent (`gemini-3.8-flash`, thinking medium, can read any help-centre entry) for a borderline reply | a fixed threshold cannot read a message with two questions | fires only when a reply is borderline; 2 of 40 tickets in the A/B, 0 in the final run; on by default, drafting uses thinking low |
| Independent post-run audit (`python -m qra audit`) | a second look at finished decisions, read-only | final run: 8 of 30 listed for a person to glance at (advisory; it changes nothing) |
| Verification: one judgment per sentence (below) | good replies were rejected | see section 20 |

Held-out set (12 cases, never tuned on): action 10/12, both misses are handoffs for a multi-entry question.

## 20. Verification: one judgment per question

The first check asked, per reply sentence, "is it supported by the help centre, **or** is it only a greeting or closing?".
That is two judgments in one question. Decision 1 answered it well; Jev rated plain greetings 0.14 to 0.24 and rejected
every reply. The fix follows the TypeSafe guidance of one claim per question: "does this sentence state a fact, step,
price, link or condition that is written neither in the help centre nor in the customer's message?". A greeting is
simply "no". Measured on 16 freshly drafted replies, each also tested with 3 invented claims (a discount, a callback
time, a platform):

| | Good replies wrongly rejected | Invented claims caught |
|---|---|---|
| Old wording, Decision 1 | 1/16 | 45/48 |
| Old wording, Jev | 16/16 | 45/48 |
| New wording at 0.5, Decision 1 | **0/16** | 45/48 |
| New wording at 0.5, Jev | **2/16** | 45/48 |

Raw data: `eval/results/verify-ab.json`. The threshold is 0.5, the same as the injection gate.

## 21. Two decision models, one pipeline

`JUDGE_BACKEND` (or `--judge`, or the dashboard toggle) chooses `gemini-decision-1` or Jev 1.14 on TypeSafe. The two
settings objects differ in exactly that one field, and a test fails if the rule table, questions, pipeline, drafter,
opinion agent or verification ever branch on it. Both modes send byte-identical requests (40 of 40 first-request
fingerprints match). Results, full pipeline, Nordavo's API read-only:

| | Decision 1 | Jev |
|---|---|---|
| Nordavo examples: action / priority / FAQ | **10/10** / 10/10 / 6/6 | 7/10 / 8/10 / 4/6 |
| Unsafe replies | 0 | 0 |
| Practice tickets with the same action | 26/30 agree | |
| Wall time, 40 tickets (Gemini includes its free-tier token wait) | 245 s | 61 s |
| Judge input tokens, 40 tickets | 180,690 | 118,173 |
| Judge call latency p50 / p95 | 445 / 873 ms | 298 / 1,367 ms |
| **Full cost per 100 tickets** (judge + Flash) | **$0.126** | **$0.082** |
| Price per M input tokens | $0.10 | $0.042 (published rate, output free) |

The gap is calibration, not plumbing. On a plain VAT-number change Jev scores injection 0.60 and personal data 0.50
(Decision 1: 0.01 and 0.06); on an iOS-requirements question it scores the right entry 0.36 (Decision 1: 0.83). Both
errors send the ticket to a person. Thresholds were not retuned per backend so the comparison stays honest; Jev with its
own thresholds is the first thing to try next.

## 22. Retrieval and caching, measured and not adopted

**Retrieval.** With 12 entries every entry is asked about on every ticket. For a large help centre there is an optional
cascade (sharded SQLite FTS5 lexical search plus an IVF vector index, fused with reciprocal rank, asking the model only
about the shortlist); it is off by default and a dashboard switch turns it on. At 12 entries it was measured and was
not better than asking about all of them; the scale benchmark (`qra/retrieval/bench.py scale`) has only been run in tests,
so no billion-entry claim is made.

**Context caching.** Implicit caching needs 4,096 tokens on `gemini-3.8-flash`, explicit needs 1,024 and our prefix is 946;
the saving would be about $0.0006 per call. Decision 1 has no caching. Not adopted.

## 22a. The accuracy dial: what can still be traded for latency and cost

Everything below is a measured or estimated way to spend less by accepting more mistakes. None is on by default.

| Dial | Saves | Costs (measured) |
|---|---|---|
| One `choice` question over all help-centre entries | -48% decision tokens (section 8) | Nordavo action 8/10 (9/10 shipped then), authored 23/26 |
| Cascade retrieval (shortlist, then judge) | cost constant in knowledge-base size | Nordavo action 7/10, FAQ match 3/6 (`eval/results/retrieval-cascade.json`) |
| Jev instead of Decision 1 (`--judge typesafe`) | -35% judge tokens, $0.082 against $0.126 per 100 tickets, about 3x faster wall time | Nordavo action 7/10, priority 8/10; fails toward a human |
| `gemini-3.5-flash-lite` for drafts and notes (`MODEL_DRAFT`) | about 2.7x faster per call, $0.30 / $2.50 per M against $0.75 / $3.75 | measured on only 6 replies (0 rejected); not run on all 40 with Decision 1 |
| No repeat draw on borderline tickets | about -19% decision tokens in run 5 (7 of 30 tickets) | a ticket near a threshold can flip between runs; the safer-answer rule is lost |
| Skip the second-opinion agent (`SECOND_OPINION=0`) | one Flash call on borderline replies | multi-part questions the rule table cannot read |
| Rule questions first, help-centre questions only if no rule fires | about -11% tokens on rule handoffs | an extra sequential call (about 0.45 s) on every other ticket |

Free: a paid Decision 1 tier (1,000,000 tokens per minute instead of 50,000) removes the token wait without any accuracy cost.

## 22b. Try a ticket, and what was left unmeasured

The dashboard has a "Try a ticket" panel (`qra/runtime/tryout.py`, `POST /api/try`): pick a stored case (Nordavo's examples, the
written and held-out cases, the practice tickets) or enter a ticket as fields or as pasted JSON, run either decision model or both
side by side, and see the decision, reply or note, what the model judged, tokens and time. It never posts anything.

Measured but not adopted: `gemini-3.5-flash-lite` for drafting and notes ran about 2.7 times faster per call (1.1 to 1.4 s against
3.5 to 3.9 s) with no rejected replies on 6 samples; the 40-ticket comparison finished only for Jev (action 8/10 against 7/10 with
3.8 Flash) and not for Decision 1, so the shipped default stays `gemini-3.8-flash`. Set `MODEL_DRAFT` to try it; the second-opinion
agent has its own `MODEL_OPINION`. 3.8 Flash does not support thinking level `minimal`; Flash-Lite defaults to it. No temperature,
top_p or top_k is set anywhere. Decision 1's free-tier 50,000 tokens-per-minute cap is a wait, not model latency, and is reported
separately (`TokenBudget.waited`).

## 23. Final run

Reset, caches cleared, default configuration (Decision 1, second opinion on, exhaustive retrieval): 30 of 30 sent,
0 errors, 189.2 s, 17 handoff, 11 reply, 2 close, 7 borderline tickets resampled, ledger 30 decided and 30 sent.
Cost per 100 tickets $0.112 (Decision 1 $0.046, Flash $0.066); the arithmetic is in the README. The log is
`logs/run-20261123-095141.jsonl`.

## 24. No test data in the prompts

Questions, criteria and word lists are written from the brief and the help centre. `tests/test_no_test_data_in_prompts.py`
fails if any run of six words from an eval ticket appears in the agent code or if a test-case id is named in it, so the
scores measure the agent, not its memory of the test.

## 25. Limits, stated plainly

- The labelled set is small (10 examples, 26 authored, 12 held out). A one-case difference between variants is weak evidence; the token saving is
  structural and does not depend on it.
- The model's answers drift over time, so a measurement is a snapshot and variants are compared only within one window.
- The free tier's token ceiling, not the code, caps throughput at roughly a dozen tickets a minute.
- Thresholds were set from the client rules and the labelled cases, not from real traffic; the first improvement with
  real traffic is learning from human edits to handoff notes.
