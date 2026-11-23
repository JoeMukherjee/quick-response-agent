"""Per-sentence verification asks one thing per sentence: does it state something outside the help centre and the ticket?"""
from qra.core.config import THRESHOLDS as T
from qra.decision.judge import Answers
from qra.safety.verify import split_sentences, verify_questions, verify_reply
from test_policy import FAQ
from test_resample import TICKET

REPLY = "Hoi, ga naar Team en klik op Uitnodigen. Daarnaast geven wij 20% korting. Groet."


class Scripted:
    def __init__(self, unsupported):
        self.unsupported = unsupported

    def ask(self, state, questions):
        noul = {k: self.unsupported.get(k, 0.03) for k in questions if k.startswith("u")}
        noul |= {"promise": 0.02, "responsive": 0.97}
        return Answers(noul=noul, choice={"reply_language": {"nl": 0.98, "en": 0.01, "de": 0.005, "other": 0.005}}, tokens=1)


def test_every_sentence_gets_exactly_one_unsupported_question_and_none_asks_two_things():
    q = verify_questions(REPLY)
    sentences = split_sentences(REPLY)
    assert [k for k in q if k.startswith("u")] == [f"u{i}" for i in range(len(sentences))]
    for k in (k for k in q if k.startswith("u")):
        assert " or is it " not in q[k]["instructions"]  # the old "supported OR greeting" double question


def test_an_invented_sentence_rejects_the_reply_and_greetings_do_not():
    entries = [FAQ["F02"]]
    ok = verify_reply(TICKET, entries, "Hoi, ga naar Team en klik op Uitnodigen. Groet.", "nl", Scripted({}))
    assert not [f for f in ok.failures if "sentence" in f]
    bad = verify_reply(TICKET, entries, REPLY, "nl", Scripted({"u1": T["verify_unsupported"]}))
    assert any("sentence 1" in f for f in bad.failures) and not bad.ok
