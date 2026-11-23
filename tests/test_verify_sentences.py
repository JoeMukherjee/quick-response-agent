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
    from qra.safety.verify import is_pleasantry
    sentences = split_sentences(REPLY)
    assert [k for k in q if k.startswith("u")] == [f"u{i}" for i, s in enumerate(sentences) if not is_pleasantry(s)]  # a bare "Groet." is decided in code
    for k in (k for k in q if k.startswith("u")):
        assert " or is it " not in q[k]["instructions"]  # the old "supported OR greeting" double question


def test_an_invented_sentence_rejects_the_reply_and_greetings_do_not():
    entries = [FAQ["F02"]]
    ok = verify_reply(TICKET, entries, "Hoi, ga naar Team en klik op Uitnodigen. Groet.", "nl", Scripted({}))
    assert not [f for f in ok.failures if "sentence" in f]
    bad = verify_reply(TICKET, entries, REPLY, "nl", Scripted({"u1": T["verify_unsupported"]}))
    assert any("sentence 1" in f for f in bad.failures) and not bad.ok


def test_greetings_and_closings_are_decided_in_code_and_never_sent_to_the_model():
    from qra.safety.verify import is_pleasantry
    for s in ("Have a great day!", "Hoi Jan,", "Met vriendelijke groet, Roosterly", "Kind regards, Roosterly", "Mit freundlichen Grüßen",
              "Hi!", "Bedankt voor uw bericht.", "Groet."):
        assert is_pleasantry(s), s
    for s in ("Prices exclude VAT.", "Hello, invoices are emailed on the 1st of every month.", "Go to app.roosterly.nl/login.",
              "Dear customer, the reset link is valid for 60 minutes.", "Thanks, we will refund you tomorrow, no problem at all.",
              "Thanks, we will refund you.", "Good luck with the start of your new employee!"):
        assert not is_pleasantry(s), s  # any fact, number, link or longer sentence still goes to the model
    q = verify_questions("Hi! You can upgrade at any time under Settings. Have a great day!")
    assert [k for k in q if k.startswith("u")] == ["u1"]  # only the sentence that states something is asked about
