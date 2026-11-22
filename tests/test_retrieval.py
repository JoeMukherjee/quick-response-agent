"""Cascade retrieval: sharded lexical + dense indexes, fusion, ingestion, the shortlister and the pipeline hook."""
import numpy as np
import pytest
from test_policy import FAQ, answers
from test_resample import TICKET, ScriptedJudge

from qra.core.models import Action, Ticket
from qra.decision.questions import faq_key
from qra.retrieval import Doc, Hit, HybridRetriever, Shortlister, ingest, rrf
from qra.retrieval.dense import HashingEmbedder, IVFIndex, ShardedDense
from qra.retrieval.lexical import ShardedLexical
from qra.retrieval.shortlist import doc_id
from qra.retrieval.text import match_query, tokens
from qra.retrieval.types import shard_of
from qra.runtime.pipeline import plan_ticket

WORDS = [f"term{i}" for i in range(400)]


def corpus(n, seed=1):
    rng = np.random.default_rng(seed)
    return [Doc(i + 1, " ".join(rng.choice(WORDS, size=12)) + f" unique{i}", plan="basis" if i % 2 else "plus") for i in range(n)]


def build(tmp_path, n=600, shards=4):
    lex, dense, emb = ShardedLexical(tmp_path, shards), ShardedDense(shards, 256, nlist=8), HashingEmbedder()
    stats = ingest(corpus(n), lex, dense, emb, batch_size=100, train_size=200)
    return lex, dense, emb, stats


# ---- text ------------------------------------------------------------------------------------------------------

def test_a_query_built_from_ticket_text_can_never_carry_search_operators():
    q = match_query(tokens('refund AND "x" OR NEAR(a b) * -secret col:body'))
    assert all(part.strip('"').isalnum() for part in q.split(" OR "))


def test_accents_are_folded_so_spellings_meet():
    assert tokens("Wachtwóórd")[0] == tokens("wachtwoord")[0]


# ---- lexical, sharding, ingestion ------------------------------------------------------------------------------

def test_every_document_is_found_by_its_unique_term_across_shards(tmp_path):
    lex, _, _, stats = build(tmp_path)
    assert stats["docs"] == 600 and lex.count() == 600
    for i in (0, 77, 311, 599):
        assert lex.search(f"unique{i}", k=3)[0].doc_id == i + 1


def test_documents_are_spread_over_all_shards(tmp_path):
    lex, *_ = build(tmp_path)
    assert all(s.count() > 50 for s in lex._shards)
    assert {shard_of(i, 4) for i in range(1, 100)} == {0, 1, 2, 3}


def test_ingesting_again_replaces_instead_of_duplicating(tmp_path):
    lex, dense, emb, _ = build(tmp_path)
    ingest(corpus(600), lex, None, None, batch_size=100)
    assert lex.count() == 600


def test_hostile_query_text_does_not_raise(tmp_path):
    lex, *_ = build(tmp_path)
    for q in ('"; DROP TABLE docs; --', "NEAR(a b)", "* AND OR", "", "   "):
        assert isinstance(lex.search(q, k=5), list)


def test_metadata_filters_apply_to_lexical_results(tmp_path):
    lex, *_ = build(tmp_path)
    hits = lex.search("term1 term2 term3 term4", k=20, filters={"plan": "plus"})
    assert hits and all(corpus(600)[h.doc_id - 1].plan == "plus" for h in hits)


# ---- dense -----------------------------------------------------------------------------------------------------

def test_ivf_search_has_high_recall_against_exact_search():
    rng = np.random.default_rng(3)
    centers = rng.standard_normal((20, 64)).astype(np.float32)
    vecs = (centers[rng.integers(0, 20, 4000)] + 0.25 * rng.standard_normal((4000, 64))).astype(np.float32)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
    index = IVFIndex(64, nlist=32)
    index.train(vecs[:1500])
    index.add(np.arange(4000, dtype=np.int64), vecs)
    hit = total = 0
    for q in vecs[rng.integers(0, 4000, 40)]:
        truth = {h.doc_id for h in index.exact(q, 10)}
        got = {h.doc_id for h in index.search(q, 10, nprobe=12)}
        hit, total = hit + len(truth & got), total + 10
    assert hit / total >= 0.9


def test_probing_fewer_cells_looks_at_fewer_vectors():
    rng = np.random.default_rng(4)
    vecs = rng.standard_normal((3000, 32)).astype(np.float32)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
    index = IVFIndex(32, nlist=30)
    index.train(vecs)
    index.add(np.arange(3000, dtype=np.int64), vecs)
    index.seal()
    sizes = sorted(len(i) for i in index._ids if i is not None)
    assert sum(sizes[-3:]) < 3000 / 3  # three cells hold a small fraction of the data: that is the saving


def test_search_before_any_data_returns_nothing():
    assert IVFIndex(8, nlist=4).search(np.zeros(8, dtype=np.float32), 5) == []


# ---- fusion and hybrid -----------------------------------------------------------------------------------------

def test_a_document_both_lists_like_outranks_one_only_one_list_likes():
    lex = [Hit(1, 9.0, "lexical"), Hit(2, 8.0, "lexical")]
    den = [Hit(3, 0.9, "dense"), Hit(2, 0.8, "dense")]
    assert rrf([lex, den])[0].doc_id == 2
    assert {h.doc_id for h in rrf([lex, den])} == {1, 2, 3}


def test_fusion_is_deterministic_on_ties():
    a = rrf([[Hit(5, 1, "lexical")], [Hit(4, 1, "dense")]])
    assert [h.doc_id for h in a] == [4, 5]


def test_the_hybrid_retriever_finds_a_document_by_its_words_and_respects_filters(tmp_path):
    lex, dense, emb, _ = build(tmp_path)
    hybrid = HybridRetriever(lex, dense, emb, candidates=50, nprobe=4)
    docs = corpus(600)
    assert hybrid.search(docs[250].text, k=5)[0].doc_id == docs[250].id
    plus_only = hybrid.search(docs[250].text, k=10, filters={"plan": "plus"})
    assert plus_only and all(docs[h.doc_id - 1].plan == "plus" for h in plus_only)


# ---- the shortlister on the real help centre -------------------------------------------------------------------

@pytest.fixture(scope="module")
def shortlister(tmp_path_factory):
    return Shortlister.from_faq(FAQ, tmp_path_factory.mktemp("kb"), max_entries=6)


def ticket(text):
    return Ticket("Q", "vraag", text, "email", "Basis")


@pytest.mark.parametrize("text,expected", [
    ("Ik ben mijn wachtwoord vergeten, hoe kom ik weer in mijn account?", "F01"),
    ("Hoe voeg ik een nieuwe medewerker toe?", "F02"),
    ("Waar kan ik de factuur van deze maand downloaden?", "F06"),
    ("Werkt de app op Android en iOS? Welke versie heb ik nodig?", "F12"),
    ("Welke salarisadministratie koppelingen zijn er, bijvoorbeeld Nmbrs?", "F04"),
])
def test_the_right_entry_is_in_the_shortlist_for_clear_questions(shortlister, text, expected):
    cut = shortlister.for_ticket(ticket(text), FAQ)
    assert expected in cut.ids and not cut.exhaustive and len(cut.ids) <= 6


def test_a_knowledge_base_no_bigger_than_the_limit_is_passed_whole():
    whole = Shortlister(None, {}, max_entries=24).for_ticket(TICKET, FAQ)
    assert whole.exhaustive and whole.faq is FAQ and whole.ids == FAQ.ids()


def test_payment_data_never_reaches_the_index_query(shortlister, monkeypatch):
    seen = []
    real = shortlister.retriever.search
    monkeypatch.setattr(shortlister.retriever, "search", lambda q, **kw: (seen.append(q), real(q, **kw))[1])
    shortlister.for_ticket(ticket("Mijn kaart 4111 1111 1111 1111 werkt niet, hoe betaal ik de factuur?"), FAQ)
    assert "4111" not in seen[0]


# ---- the pipeline hook -----------------------------------------------------------------------------------------

def test_in_cascade_mode_the_judge_is_asked_only_about_the_shortlist(shortlister):
    asked = []

    class Spy(ScriptedJudge):
        def ask(self, state, questions):
            if "covers_everything" not in questions:
                asked.append([k for k in questions if k.startswith("faq_")])
            return super().ask(state, questions)

    judge = Spy([answers(faq={"F02": 0.97}), answers(faq={"F02": 0.97})])
    out = plan_ticket(ticket("Hoe voeg ik een nieuwe medewerker toe?"), judge, FAQ, shortlister=shortlister)
    assert 0 < len(asked[0]) <= 6 and faq_key("F02") in asked[0]
    assert out.shortlist and "F02" in out.shortlist


def test_without_a_shortlister_the_judge_is_asked_about_every_entry():
    asked = []

    class Spy(ScriptedJudge):
        def ask(self, state, questions):
            if "covers_everything" not in questions:
                asked.append(len([k for k in questions if k.startswith("faq_")]))
            return super().ask(state, questions)

    plan_ticket(TICKET, Spy([answers(faq={"F02": 0.97})] * 2), FAQ)
    assert asked == [12]


def test_a_missed_entry_is_a_safe_failure_not_a_wrong_reply():
    class Blind:  # a retriever that returns the wrong entries entirely
        def for_ticket(self, ticket, faq):
            from qra.retrieval.shortlist import Shortlist
            return Shortlist(faq.subset(["F11", "F09"]), ["F11", "F09"], False)

    judge = ScriptedJudge([answers(faq={"F11": 0.05, "F09": 0.05})] * 2)
    out = plan_ticket(ticket("Hoe voeg ik een nieuwe medewerker toe?"), judge, FAQ, shortlister=Blind())
    assert out.plan.action is Action.HANDOFF  # nothing matched, so a person gets it


def test_entry_ids_map_to_stable_integers():
    assert doc_id("F02") == doc_id("F02") and doc_id("F02") != doc_id("F03") and doc_id("F02") < 2 ** 62
