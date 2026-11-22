# Scaling and security notes

The agent is deliberately small enough to run from a fresh clone with one command and to explain on a call.
This page says what is already built for scale, what the real limits are, and what changes at 100x.

## What is built

| Concern | Choice | Why |
|---|---|---|
| Durable state | SQLite (WAL, `synchronous=FULL`) ledger with `UNIQUE(epoch, ticket, state)` | The database itself refuses a second decision for a ticket, so two workers or two processes cannot both own it. No server to install. |
| Caching | SQLite key-value cache of Decision answers, keyed by `sha256(model, input, questions)` | Re-running the eval after a threshold or truth-table change costs nothing; only reworded questions spend tokens. |
| Concurrency | N worker threads (`--workers`, default 4) behind a `WorkQueue` seam | The work is I/O bound (HTTP and model calls). |
| Back-pressure | Bounded queue, a request limiter shared by all threads (4 req/s) and a token-budget window for the model (45k tokens/min) | Two external ceilings decide throughput, not the CPU. |
| Single runner | `O_CREAT|O_EXCL` lock file | Two processes cannot race on the same ledger. |
| Exactly once | Persist the decision, GET before any retry, never retry a 409 | See `qra/delivery.py`. |

## The honest throughput story

More workers do not make this faster after a point. The helpdesk allows 5 requests/second and returns 429 above
that, and the free Gemini tier allows about 50k tokens/minute (about 12 tickets/minute at ~3.3k tokens per ticket
for triage alone). Each ticket needs a handful of helpdesk calls and 2-4 model calls, so four workers already
saturate both limits; extra workers only buy 429s. The measured numbers for a full 30-ticket run are in the README.

## Queue seam, and what changes at 100x

`qra/workqueue.py` is a three-method interface (`put`, `get`, `close`). Today it is an in-process bounded queue.
To scale beyond one machine:

1. Replace `LocalQueue` with Redis Streams (consumer groups) or SQS; workers become separate processes or pods.
2. Move the ledger from SQLite to Postgres (same schema, same unique constraint); the constraint, not the queue,
   still owns the exactly-once guarantee, so at-least-once delivery from the queue is safe.
3. Share the decision cache in Redis (same key) so replicas do not pay twice.
4. Put the token budget in Redis too (a shared sliding window) so replicas together stay under the model quota.
5. Paid Gemini tier (higher TPM) and a helpdesk rate agreement; webhooks instead of polling `GET /tickets`.

Redis, a broker and Docker are intentionally not required to run this repo.

## Security

Implemented here:
- Secrets only from environment variables; `.env` is git-ignored, `.env.example` is committed.
- TLS verification on; the helpdesk URL must be https; redirects are never followed, so the bearer key cannot be
  replayed to another host; explicit timeouts on every call.
- Auth headers are never logged; logs contain no ticket text and are scrubbed for payment data.
- Payment data (IBAN, card numbers) is redacted before any model call, from every reply and note, and from the logs.
- Ticket text is data: it is passed as a JSON value, never concatenated into instructions, and no model has a tool
  that can reach the helpdesk. A manipulated model can write a bad draft; it cannot send anything.
- The decision is made in code (a pure truth table), with hard rules that can only escalate.

Belongs to deployment rather than this repo: network policy and egress allow-lists, secret manager and rotation,
mTLS or private networking between workers and the broker, a DPA with the model provider (the free Gemini tier may
use prompts to improve products, so production traffic needs the paid tier), and audit retention.
