# Token usage

This page explains where the Decision tokens go, what we tried, and what we changed. All numbers come from the run telemetry and from `python -m qra experiment` (results in `eval/results/token-experiments.json`).

## Where the tokens go

One ticket costs one triage call and, when a reply is possible, a coverage call and a verification call. Decision 1 bills the whole input on every call and has no context cache, so a prefix that never changes is paid for again on every ticket.

| Part of the triage request | Share of the request |
|---|---|
| 12 help-centre questions (entry text plus repeated criteria) | 65% |
| 6 hard rules | 16% |
| Injection, spam, asks-for-help | 7% |
| Language and category | 6% |
| 2 urgency questions | 5% |
| The ticket itself | 2% |

About 98% of the request is identical for every ticket, and more than half of the help-centre share was the same criteria sentence repeated 12 times.

## Why it matters

The free tier allows about 45,000 Decision tokens a minute. At roughly 3,300 tokens per triage call, that is about 13 tickets a minute. The lane chart shows the effect: a worker waiting on the token budget looks like a long "Plan" bar (95th percentile 48 s against a 2.1 s median). Money is not the constraint: Decision 1 costs $0.10 per million input tokens.

## The experiment

The same 36 labelled cases (Nordavo's 10 examples and our 26 authored cases) ran through three ways of asking the help-centre question. The 30 practice tickets were never used.

| Variant | Tokens per ticket | vs current | Unsafe replies | Action, Nordavo | Action, authored | Handoff recall |
|---|---|---|---|---|---|---|
| `per_entry` (previous design) | 3,759 | 0% | 0 | 10/10 | 24/26 | 18/18 |
| `shared_definition` (adopted) | 2,956 | -21% | 0 | 10/10 | 26/26 | 18/18 |
| `choice` | 1,946 | -48% | 0 | 9/10 | 24/26 | 18/18 |

- **`per_entry`**: twelve yes/no questions, each carrying the entry text and its own long criteria.
- **`shared_definition`**: the same twelve questions, but the definition of an appropriate response is stated once in the shared input.
- **`choice`**: one choice question over short entry summaries. It is the cheapest, but it loses a Nordavo example (E02) because one question splits probability between entries.

## Decision

We adopted `shared_definition`. It cuts 21% of the tokens, fixes two authored cases that the previous design escalated unnecessarily, and does not weaken any safety measure. The change is one constant (`DEFAULT_FAQ_MODE` in `qra/decision/questions.py`), so it is easy to reverse.

Caveats, stated plainly:

- We chose among three variants using these 36 cases, so the two authored cases that flipped are weaker evidence than the token saving, which is structural.
- One authored case (A26) moved from P2 to P1 under the new wording. Both priorities go to a human, so the action is unchanged.
- The `choice` variant stays available behind the same switch if a tighter token budget ever matters more than one example.

## A bug the experiment exposed

The decision cache was silently disabled: an empty cache is falsy in Python because it defines `__len__`, so `if cache:` skipped every write. The fix and a regression test are in `tests/test_caching.py`. Cache hits now show on the dashboard.
