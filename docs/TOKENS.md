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

The same 36 labelled cases (Nordavo's 10 examples and our 26 authored cases) ran through three ways of asking the help-centre question, three times each with the cache off. The 30 practice tickets were never used.

| Variant | Tokens per ticket | vs previous | Unsafe replies | Action, Nordavo | Action, authored | Authored over-escalation |
|---|---|---|---|---|---|---|
| `per_entry` (previous design) | 3,779 | 0% | 0 | 9/10 | 24/26 | 2/9 |
| `shared_definition` (adopted) | 2,979 | -21% | 0 | 9/10 | 25/26 | 1/9 |
| `choice` | 1,966 | -48% | 0 | 8/10 | 23/26 | 3/9 |

- **`per_entry`**: twelve yes/no questions, each carrying the entry text and its own long criteria.
- **`shared_definition`**: the same twelve questions, but the definition of an appropriate response is stated once in the shared input.
- **`choice`**: one choice question over short entry summaries. It is the cheapest and the least accurate, because one question splits probability between entries.

## What the repeats showed

The three repeats of every variant were identical, so inside one time window Decision 1 is deterministic: the same input returns the same probabilities. Runs made 20 to 30 minutes apart did differ (an early run scored 10/10 on Nordavo's examples for two variants, the later runs 9/10), so the API's answers drift over time. Two consequences:

- A single measurement is a snapshot. We compare variants measured in the same window, as above.
- Borderline probabilities can move between a test run and the scored run. That is what resampling addresses (see below), and `python -m qra noise` measures how much each question moves.

## Decision

We adopted `shared_definition`. It cuts 21% of the tokens, answers one more authored case correctly, over-escalates one fewer, and does not weaken any safety measure. The change is one constant (`DEFAULT_FAQ_MODE` in `qra/decision/questions.py`), so it is easy to reverse.

Caveats, stated plainly:

- We chose among three variants using these 36 cases, so the one authored case that flipped is weaker evidence than the token saving, which is structural.
- The `choice` variant stays available behind the same switch if a tighter token budget ever matters more than accuracy.

## Resampling borderline tickets

When a deciding probability lands within 0.1 of its threshold, the pipeline asks the same question once more with a slightly different input and combines the two draws toward the safer outcome: escalation signals take the larger probability, and signals that enable a reply or a close take the smaller. Clear tickets never pay for the second call. The dashboard's Judge node shows how many tickets were resampled.

## A bug the experiment exposed

The decision cache was silently disabled: an empty cache is falsy in Python because it defines `__len__`, so `if cache:` skipped every write. The fix and a regression test are in `tests/test_caching.py`. Cache hits now show on the dashboard.
