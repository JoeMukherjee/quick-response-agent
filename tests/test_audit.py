"""The post-run audit: advisory, read-only, and it flags what a person should look at."""
import json

from test_policy import FAQ

from qra.core.models import Answers
from qra.evaluation.audit import DOUBT, QUESTIONS, audit_run, audit_state, flags_for, read_log

CLEAN = {"rules_followed": 0.95, "reply_supported": 0.95, "reply_answers_everything": 0.95, "help_centre_could_answer": 0.1,
         "priority_matches": 0.9, "category_matches": 0.9, "was_spam_or_empty": 0.02}


class FakeClient:
    def __init__(self):
        self.reads = []

    def get_ticket(self, tid):
        self.reads.append(tid)
        return {"id": tid, "subject": "Facturen", "body": "Waar vind ik mijn factuur?", "customer_plan": "Basis"}


class ScriptedJudge:
    provider, model, tokens = "Test", "scripted", 0

    def __init__(self, per_ticket):
        self.per_ticket, self.asked = per_ticket, []

    def ask(self, state, questions):
        s = json.loads(state)
        self.asked.append((s, questions))
        self.tokens += 100
        return Answers(noul={**CLEAN, **self.per_ticket.get(s["decision"]["reason"], {})})


def write_log(tmp_path, rows):
    p = tmp_path / "run-x.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return p


ROWS = [{"ticket_id": "T001", "action": "reply", "priority": "P4", "category": "question", "reason": "ok-reply", "en_gloss": "Invoices are emailed on the 1st."},
        {"ticket_id": "T002", "action": "handoff", "priority": "P2", "category": "billing", "reason": "refund"},
        {"ticket_id": "T003", "action": "handoff", "priority": "P3", "category": "question", "reason": "over-escalated"},
        {"ticket_id": "T004", "action": "reply", "priority": "P4", "category": "question", "reason": "bad-reply", "en_gloss": "We refund everything."},
        {"ticket_id": "T005", "action": "reply", "priority": "P4", "category": "billing", "reason": "gdpr-answered"},
        {"ticket_id": "-", "action": "", "reason": "run summary row"}]


def test_a_clean_decision_is_not_flagged_and_each_flag_names_what_to_look_at():
    assert flags_for("reply", CLEAN, "P4", "question") == []
    assert flags_for("handoff", {**CLEAN, "help_centre_could_answer": 0.92}, "P3", "question") == ["could_have_replied 0.92"]
    assert flags_for("reply", {**CLEAN, "reply_supported": 0.2}, "P4", "question") == ["reply_unsupported 0.20"]
    assert flags_for("reply", {**CLEAN, "rules_followed": 0.1}, "P4", "other") == ["rules_followed 0.10"]
    assert flags_for("handoff", {**CLEAN, "was_spam_or_empty": 0.9}, "P4", "other") == ["should_have_closed 0.90"]
    assert any("priority P1 doubted" in f for f in flags_for("handoff", {**CLEAN, "priority_matches": 0.2}, "P1", "bug"))


def test_a_reply_only_question_never_flags_a_handoff_and_the_reverse():
    weak = {**CLEAN, "reply_supported": 0.0, "reply_answers_everything": 0.0}
    assert flags_for("handoff", weak, "P2", "billing") == []  # there is no reply to doubt
    assert flags_for("reply", {**CLEAN, "help_centre_could_answer": 0.99}, "P4", "question") == []  # a reply is not "could have replied"


def test_the_audit_reads_every_ticket_once_flags_the_right_ones_and_skips_summary_rows(tmp_path):
    client = FakeClient()
    judge = ScriptedJudge({"over-escalated": {"help_centre_could_answer": 0.95}, "bad-reply": {"reply_supported": 0.1},
                           "gdpr-answered": {"rules_followed": 0.2}})
    report = audit_run(write_log(tmp_path, ROWS), client, judge, FAQ)
    assert client.reads == ["T001", "T002", "T003", "T004", "T005"] and report["audited"] == 5 and report["flagged"] == 3
    flagged = {r["id"]: r["flags"] for r in report["rows"] if r["flags"]}
    assert set(flagged) == {"T003", "T004", "T005"} and report["input_tokens"] == 500
    assert report["judge"] == "Test scripted"


def test_the_report_holds_ids_flags_and_probabilities_but_no_ticket_text(tmp_path):
    report = audit_run(write_log(tmp_path, ROWS), FakeClient(), ScriptedJudge({}), FAQ)
    dumped = json.dumps(report)
    assert "Waar vind ik mijn factuur" not in dumped and "Facturen" not in dumped and "Invoices are emailed" not in dumped
    assert set(report["rows"][0]) == {"id", "action", "priority", "category", "flags", "judgments"}


def test_the_judge_is_shown_the_policy_the_help_centre_and_the_decision_and_every_question_is_a_noul():
    state = audit_state({"subject": "s", "body": "b", "customer_plan": "Plus"}, ROWS[0], FAQ)
    assert set(state) == {"ticket", "client_rules", "priority_definitions", "category_definitions", "help_centre", "decision"}
    assert len(state["help_centre"]) == len(FAQ.entries) and state["decision"]["reply_gloss"]
    assert all(q["type"] == "noul" and "true_description" in q["criteria"] for q in QUESTIONS.values())
    assert 0 < DOUBT < 1


def test_the_audit_is_read_only(tmp_path):
    client = FakeClient()
    assert not hasattr(client, "act") and not hasattr(client, "reset")  # the audit is handed no way to post, hand off or reset
    audit_run(write_log(tmp_path, ROWS), client, ScriptedJudge({}), FAQ)
    assert read_log(tmp_path / "run-x.jsonl")[0]["ticket_id"] == "T001"
