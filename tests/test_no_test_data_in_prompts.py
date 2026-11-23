"""Nothing from a test ticket may be written into the shipped prompts, criteria or rules.

The questions, criteria, thresholds and word lists must be written from the brief and the help centre, not from the tickets they are
scored on; otherwise the scores would measure memory of the test, not the agent. This fails if any run of six words from a
ticket in eval/*.jsonl (the 26 authored cases) appears in the agent's code, and if a ticket id is named in it.
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP = {"evaluation", "static"}  # the evaluation package and the built dashboard are allowed to name cases


def words(text):
    return re.findall(r"[a-zà-ÿ0-9']+", text.lower())


def shingles(text, n=6):
    w = words(text)
    return {" ".join(w[i:i + n]) for i in range(len(w) - n + 1)}


def shipped_sources():
    for path in (ROOT / "qra").rglob("*.py"):
        if not SKIP & set(path.relative_to(ROOT / "qra").parts):
            yield path, path.read_text(encoding="utf-8")


def test_no_run_of_six_words_from_a_test_ticket_appears_in_the_agent():
    tickets = []
    for name in ("authored_cases.jsonl",):
        tickets += [json.loads(line) for line in (ROOT / "eval" / name).read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(tickets) >= 20
    corpus = {}
    for path, text in shipped_sources():
        corpus[path] = " ".join(words(text))
    for t in tickets:
        for s in shingles(t["subject"] + " " + t["body"]):
            for path, flat in corpus.items():
                assert s not in flat, f"{path.name} contains text from test ticket {t['id']}: {s!r}"


def test_no_ticket_id_is_named_in_the_agent():
    pattern = re.compile(r"\b(?:T0\d\d|E(?:0\d|10)|H\d\d|A\d\d)\b")
    for path, text in shipped_sources():
        assert not pattern.search(text), f"{path.name} names a test case: {pattern.search(text).group()}"
