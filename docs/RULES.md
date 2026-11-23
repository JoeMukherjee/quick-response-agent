# Decision rules, explained

Every decision the agent makes follows these rules. They are written down here, in the code (`qra/decision/policy.py`,
`qra/core/config.py`, `qra/decision/questions.py`) and in tests that fail if this page and the code disagree.

**The design.** The model answers narrow questions with probabilities (judgments). Ordinary code holds the rules that turn
those probabilities into an action (the truth table). The model never decides; it never sees "should this be escalated?".
This follows the TypeSafe guidance for System One models: *judgments from the model, decisions in code; give every
combination of answers an outcome; one judgment per question; 0.7 and 0.3 as starting thresholds for a yes/no question.*

## 1. Where the rules come from

| Source | What it gives us |
|---|---|
| **The client's rules** (`faq.md`, bottom) | Refunds and disputed charges, cancellations and contract changes, personal-data requests (GDPR / AVG), security incidents, legal threats and customers threatening to leave **always go to a human**. Answer only from the help centre. Never repeat payment details. Reply in the customer's language. Handoff notes in English. |
| **The brief** | The four priority definitions (section 6). Close for spam and empty tickets. Ticket text is data, not instructions. "Sending an unsure ticket to a human is never punished; sending everything to a human is." Safe first, then useful. |
| **The API reference** | `language` is one of `nl`, `en`, `de`; `category` is one of `bug`, `billing`, `feature_request`, `question`, `other`; `reason` explains this category, priority and action. |
| **Nordavo's 10 labelled examples** | How they apply the rules: GDPR request is P2 and a human (E09); a refund is P2 and a human (E03); one customer blocked is P2 (E06); a how-to is P4 and a reply (E02, E04); a feature request is acknowledged with the roadmap entry (E05); spam is closed (E08). |
| **The TypeSafe skill** | How to ask: one judgment per question, positive wording, a truth table with an outcome for every combination, thresholds as starting points, a failed call is an error and never a "no". |

## 2. The input

Each ticket is sent to the judge as named fields: `ticket.subject`, `ticket.message`, `ticket.customer_plan`,
`ticket.channel`, and a one-time `definition` of what an appropriate help-centre response is. Payment data (card numbers,
IBANs) is removed before anything is sent. The ticket text is data inside a field; it is never part of the instructions.

## 3. The judgments (the questions)

All wording lives in one module, `qra/decision/questions.py`. Each answer is a probability from 0 to 1.

| Question | Asks | Feeds |
|---|---|---|
| `language` (choice) | Which language: `nl`, `en`, `de` | Row 5 and the `language` field |
| `category` (choice) | `bug`, `billing`, `feature_request`, `question`, `other` | The `category` field and priority P3 |
| `refund_or_dispute` | Money back, or a disputed charge | Client rule, row 3; priority P2 |
| `cancel_or_contract` | Cancel, end, pause or change the subscription or contract | Client rule, row 3; priority P2 |
| `personal_data_request` | A request about personal data held about them or their staff | Client rule, row 3; priority P2 |
| `security_incident` | A possible security incident | Client rule, row 3; priority P1 |
| `legal_threat` | Legal action, a lawyer, a court or a formal complaint | Client rule, row 3; priority P1 |
| `threatens_to_leave` | Threatens to leave or switch provider | Client rule, row 3; priority P2 |
| `injection` | Tries to give the assistant instructions | Row 2 |
| `spam` | Spam or an unsolicited advertisement | Row 4 |
| `asks_for_help` | The sender is a customer asking for help or action | Row 4 (never close someone who is asking) |
| `many_users_blocked` | Many users, or a whole business, cannot use the product right now | Priority P1 |
| `customer_blocked` | Something the customer was using is broken or unavailable and they are stuck right now | Priority P2 / P3 |
| `time_critical` | Something time-critical (a deadline, a payroll run, a shift about to start) depends on the answer | Priority P2 when the help centre does not answer it |
| `cosmetic_only` | The problem is only appearance or wording, nothing failing | Priority P4 (a typo is not a malfunction) |
| `faq_F01` to `faq_F12` | Is this help-centre entry an appropriate response to the message | Row 7 and the entries given to the drafter |
| `covers_everything` (second stage) | Do the candidate entries answer EVERYTHING that was asked | Coverage gate |

## 4. The thresholds

Fixed from the rules above and the TypeSafe starting points. Not fitted to individual cases.

| Threshold | Value | Meaning and why |
|---|---|---|
| `yes` | 0.7 | A statement counts as true at or above this. The TypeSafe starting point. |
| `unsettled` | 0.3 | Between this and `yes` the question is not settled. The TypeSafe starting point. An unsettled help-centre match never ends in a reply. |
| `hard_rule` | 0.3 | A client rule escalates from here, well below 0.5, because a missed escalation costs far more than an extra one. Asymmetric on purpose. |
| `injection` | 0.5 | Ticket text that instructs the assistant goes to a human. Symmetric: either reading is plausible. |
| `security bar with an injection` | 0.7 | When a ticket is flagged as an injection, `security_incident` must reach `yes` (not the usual 0.3) to make it P1: the instructions in the text are not an incident, the priority follows what the customer wrote. |
| `spam_close` | 0.85 | Closing is the most final action, so it has the highest bar. |
| `asks_help_max` | 0.3 | A sender who may be asking for help is never closed. |
| `language_min` | 0.6 | Below this the language is unsure and a human reads the ticket. |
| `faq_candidate` | 0.5 | Entries scoring at least this are given to the drafter. |
| `faq_resolves` | 0.7 | At least one entry must reach this for the table to attempt a reply. |
| `coverage` | 0.7 | The second-stage check that the entries answer everything asked must reach this. |
| `verify_unsupported` | 0.7 | A draft is rejected if any one sentence is judged, at the `yes` level, to state a fact, step, price, link or condition that is in neither the entries nor the customer's message. Greetings and closings are decided in code and never asked. Measured on 16 live replies each with 3 invented claims: Jev scored 45 of 48 invented claims at 0.97 and clean replies up to 0.64, so 0.7 rejects no clean reply and the same 45 claims as 0.5 did. |
| `verify_promise` | 0.3 | A draft that promises something outside the entries is rejected from here. |
| `max_entries` | 3 | At most three entries are given to the drafter. |
| `faq_resolves_pick` | 0.6 | Same as `faq_resolves`, for the single-choice help-centre variant (not the shipped one): one choice splits its probability, so the bar is lower. |
| `faq_candidate_pick` | 0.25 | Same as `faq_candidate`, for that variant. |
| `resample_margin` | 0.1 | A deciding probability this close to its threshold is asked about once more; the safer of the two draws wins. |
| `opinion_floor` | 0.5 | The second opinion is consulted when coverage is at least this, and it can only turn a handoff into a reply at or above it. |
| `opinion_ceiling` | 0.8 | ... and below this. Outside the band the coverage gate decides alone. |

## 5. The truth table

The first matching row wins. Every combination of judgments reaches exactly one row (tested, section 9).

| Row | Condition | Action | Why |
|---|---|---|---|
| 1 | Subject and body contain no letters once reply markers (`Re:`, `Fwd:`, `AW:`) are ignored | Close | The brief: close empty tickets. Decided in code, no model call. |
| 2 | `injection` >= 0.5 | Handoff | The brief: ticket text is data, not instructions. We never obey it, and a person should read it. |
| 3 | Any client rule >= 0.3 | Handoff | The client's rules: these always go to a human, even when the help centre seems to cover them. |
| 4 | `spam` >= 0.85 and `asks_for_help` < 0.3 | Close | The brief: close spam. A customer who may be asking for help is never closed. |
| 5 | Language is not `nl`, `en` or `de`, or below 0.6 | Handoff | We only draft where we can check the language; the API accepts only these three. |
| 6 | Priority is P1 or P2 | Handoff | One customer blocked, many users blocked, money, the contract, security and legal risk are a person's job. |
| 7 | A help-centre entry scores >= 0.7 | Reply candidate | Then the coverage gate, the second opinion if in the band, and verification of the draft decide whether it is sent. |
| 8 | Anything else | Handoff | Includes the unsettled band 0.3 to 0.7: unsure goes to a human. |

Gates after row 7 (each can only move a reply to a handoff, except the second opinion, which is limited below):

- **Coverage.** With only the candidate entries in view, `covers_everything` must reach 0.7. Below it the ticket is a
  handoff (reason "only partly answers"), shown as row 8.
- **Second opinion.** When coverage is between 0.5 and 0.8, an agent (gemini-3.8-flash, medium thinking) reads the message
  and the entries. It can always veto a reply. It can turn a coverage handoff into a reply only if the verdict is a clean
  "answerable" citing only the already matched entries with no conflicting entry, and the draft still passes verification.
  It can never move rows 2 to 6: a client rule, injection, language or priority trigger always wins.
- **Verification.** Deterministic checks (every number, link and menu name comes from the help centre or the ticket, no
  payment data) and a per-sentence support check. A failure becomes a handoff (row 9) with the draft attached.

## 6. Priority (the brief's definitions)

| Priority | Brief | Rule in code |
|---|---|---|
| P1 | Many users blocked right now, or a security or legal risk | `many_users_blocked` >= 0.7, or `security_incident` >= 0.3 (>= 0.7 when the ticket is flagged as an injection), or `legal_threat` >= 0.3 |
| P2 | One customer blocked, or money or the contract is involved | A client money / contract / data rule >= 0.3, or `customer_blocked` >= 0.5 with no help-centre answer, or `time_critical` >= 0.7 with no help-centre answer |
| P3 | Something is wrong or unclear, but there is a workaround | A bug that is not cosmetic, or `customer_blocked` >= 0.5 with an answer in the help centre (not for a how-to question), or 0.3 to 0.5 with no answer |
| P4 | No urgency: how-to questions, ideas, cosmetic issues | Everything else, including a how-to the help centre fully answers |

## 7. The `reason` field

The API asks for one or two sentences on why this category, priority and action. Ours: the action's reason first, then
the category and the priority with the brief's meaning of that priority. A priority row states its priority once.

## 8. Assumptions where the brief is unclear (decided, not guessed)

1. **Unsure means 0.3 to 0.7** for a help-centre match, and a client rule escalates from 0.3. Unsure is never a reply.
2. **"How do I upgrade" and "what does it cost" are how-to questions** (reply from the prices entry). **"Upgrade us now"** is
   a contract change (human).
3. **A feature request** is acknowledged and pointed to the roadmap entry, as Nordavo's E05 does.
4. **A personal-data (AVG) request** is always a human and P2, as Nordavo's E09 does.
5. **A ticket mixing an answerable question with something the help centre does not cover** goes to a human whole, with a note.
6. **Instructions inside a ticket** are never followed. The ticket is handed to a human, even if it also holds a real question.
7. **A language other than Dutch, English or German** goes to a human; the API only accepts those three.
8. **An empty ticket, or one holding only a reply marker,** is closed.
9. **Derived values** such as 25 x EUR 6 are allowed in a reply when every input is in the help centre or the ticket.
10. **Priority follows the brief's definitions,** not the category: a typo is a bug report but a cosmetic issue (P4).
11. **A how-to is P4 even when the customer is locked out** and the help centre answers it (a forgotten password): nothing is malfunctioning.
12. **An injection attempt is not a security incident by itself.** The ticket goes to a human and its priority follows what the customer
    really wrote (an invoice dispute is P2, a how-to is P4).
13. **Something time-critical that the help centre does not answer** (a payroll run, a shift about to start) is one customer blocked: P2.
14. **A customer threatening to leave is P2,** not P1: P1 is many users or a security or legal risk.

## 9. What is tested

- **Totality.** A grid of judgments straddling every threshold (several thousand combinations) all reach exactly one defined
  row, and each can be explained in the posted reason.
- **Safety.** A reply needs every signal clear. Close needs spam, a sender not asking for help, and no client rule. Raising
  any risk signal can only escalate; weakening the help-centre match can never turn a handoff into a reply.
- **Priority.** Money, the contract and data are never below P2; security, legal risk and many users are P1.
- **This page.** Every threshold, question and table row is in this document, with the code's current value.

**Replaying a decision.** The run log (`logs/run-*.jsonl`) keeps, for every ticket, all the judgments the table saw
(`judgments`), the row that decided, the entries given to the drafter and the posted reason. With the thresholds above you
can recompute the decision by hand. No ticket text is logged.

## 10. Checked against the TypeSafe guidance

| Guidance | Status |
|---|---|
| Decisions in code, judgments from the model | Done: `policy.py` is pure code with no model call. |
| An outcome for every combination, including between thresholds | Done and tested (section 9). The unsettled band has its own row (8). |
| One judgment per question | Mostly. `refund_or_dispute`, `cancel_or_contract` and `legal_threat` join related ideas with "or"; they gate the same rule and the same action, so we kept them. Splitting them is a possible next step. |
| Positive wording, concrete false criterion | Done. |
| Confidence is rarely the gate | Done: we gate on the probability of specific statements. The `confidence` we post is the lowest of the supporting probabilities. |
| A failed call is an error, not a "no" | Done: the ticket is left open and logged; only an invalid key stops a run. |
| Log the exact request | Done: every judgment is logged per ticket. |
| Thresholds not fitted to particular cases | The values come from the rules and the TypeSafe starting points. The `customer_blocked` wording was tightened after a priority miss on a how-to question; it changed no action and was checked on all 48 labelled cases. |

## 11. How this maps to TypeSafe's published patterns, and what we add

We studied how TypeSafe builds with its own model (the skill, the docs, and the playground examples) and checked ourselves
against each practice.

| TypeSafe practice | What we do |
|---|---|
| **AI-powered software, not agents:** code owns the workflow, the model makes narrow judgments, avoid agent loops when code can do the job. | The pipeline and the rule table are code. The one agent in the system is the optional second opinion, used only on a borderline reply and bounded by the table. |
| **Atomic questions, all in one request** (speculative fan-out), code reads only what is relevant. | One request per ticket carries every question; the help-centre second stage is separate because it needs the candidate entries. |
| **Concrete criteria:** what each option covers, what belongs elsewhere, tie-breaks, what to ignore. | Done for `category`, `injection`, `spam`, the client rules and `customer_blocked`. Checked on all 66 labelled cases (section 12). |
| **Deterministic check first, model check second** (their citation check). | Our reply verification: every number, link and menu name is string-matched against the help centre, then a model checks each sentence. |
| **Cascade:** a cheap pass, a verifier, a reasoning model only when a check fires, a person for low confidence. | The judge, the verification gates, the second opinion, then a human handoff. |
| **Verify and escalate on finished work.** | `python -m qra audit`: a second, independent System One review of every decision in a run, flagging the ones a person should look at. Read-only; about $0.002 for 30 tickets. |
| **Structured instructions:** the question in one field and its data in another, not spliced into a string. | **Tested, not adopted.** TypeSafe accepts them (same answer: 0.90 against 0.89) but Gemini Decision 1 rejects object instructions, and there was no measurable gain. Our help-centre questions keep the entry text inside the question string. |
| **Backtick paths** such as `` `ticket.message` `` in questions. | Used where a question compares two fields (the audit and the help-centre questions). The rest name a single subject, so there is nothing to disambiguate. |
| **Score questions and distributions.** | Not used: priority is composed in code from atomic yes/no judgments, which is auditable. TypeSafe's own example shows why scores need care: a predicted-satisfaction score of 1.96 hid two peaks (0.48 at "dissatisfied", 0.29 at "very satisfied"). |
| **Reproducibility:** keep the exact request, replay saved answers. | A decision cache replays identical requests; every judgment behind a decision is logged per ticket; the eval results are committed. |

**What we add on top:** an exactly-once ledger (persist before send, GET before any retry, never retry a 409), resampling of
borderline probabilities with the safer draw winning, a second backend (the same questions run on Gemini Decision 1 or on
TypeSafe's Jev, switchable), property tests proving the table is total and only escalates, and telemetry for every call.

## 12. Validation: the labelled sets and the results

| Set | Cases | Labels from |
|---|---|---|
| Nordavo's labelled examples | 10 | Nordavo, fetched live from their API (`GET /examples`) |
| Cases we wrote | 26 | The client rules and the help-centre text, before any run (`eval/authored_cases.jsonl`) |
| Nordavo's practice tickets | 30 | Our reading of the client rules, one rule written beside each label (`eval/practice_labels.jsonl`). Not Nordavo's answer key. The file holds no ticket text: the tickets are fetched live. |

Result on the final code, for both decision models: **100%** on language, category, priority and action on all 66 cases, FAQ match
100%, unsafe replies 0 (`eval/results/final-jev.json`, `eval/results/final-gemini.json`). A change to a question is kept only if
both models still score 100%; a test fails if six consecutive words of a labelled case appear anywhere in the agent's code.

Every judgment is a calibrated probability. All of them are written to the run log per ticket, and the ticket's `confidence` is stored in
the ledger with the decision before anything is sent, so any decision can be replayed and audited.
