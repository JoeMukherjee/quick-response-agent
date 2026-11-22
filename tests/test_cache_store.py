"""The cache is bounded, keeps what is used, and every lookup path is indexed."""
import sqlite3

from qra.infra.cache import ROW_OVERHEAD, KVCache


def make(tmp_path, max_bytes=6000, max_entries=1000, ns="decisions", name="c.db"):
    return KVCache(tmp_path / name, ns, max_bytes=max_bytes, max_entries=max_entries)


def fill(c, n, size=120, prefix="k"):
    for i in range(n):
        c.put(f"{prefix}{i}", {"v": "x" * size})


def test_total_size_never_exceeds_the_budget(tmp_path):
    c = make(tmp_path, max_bytes=6000)
    fill(c, 200)
    assert c.stats()["bytes"] <= 6000 and c.stats()["evictions"] > 0 and len(c) < 200
    actual = sqlite3.connect(tmp_path / "c.db").execute("SELECT SUM(size) FROM kv").fetchone()[0]
    assert actual == c.stats()["bytes"]  # the in-memory accounting matches the database


def test_the_entry_budget_is_enforced_too(tmp_path):
    c = make(tmp_path, max_bytes=10_000_000, max_entries=50)
    fill(c, 300, size=10)
    assert len(c) <= 50


def test_frequently_hit_entries_survive_while_cold_ones_are_evicted(tmp_path):
    c = make(tmp_path, max_bytes=6000)
    c.put("popular", {"v": "p" * 120})
    for _ in range(40):
        assert c.get("popular") is not None  # earns its place
    fill(c, 120, prefix="cold")
    assert c.get("popular") is not None  # still there after many evictions
    assert c.stats()["evictions"] > 50
    survivors = [k for k in (f"cold{i}" for i in range(120)) if c.get(k) is not None]
    assert len(survivors) < 120  # the cold tail was evicted


def test_hit_counts_decay_over_time_so_old_popularity_does_not_squat_forever(tmp_path):
    now = [1000.0]
    c = KVCache(tmp_path / "c.db", "decisions", max_bytes=6000, clock=lambda: now[0])
    c.put("was_popular", {"v": "p" * 120})
    for _ in range(40):
        c.get("was_popular")
    c.flush()
    db = sqlite3.connect(tmp_path / "c.db")
    assert db.execute("SELECT hits FROM kv WHERE key='was_popular'").fetchone()[0] == 40
    fill(c, 80, prefix="a")  # many eviction passes inside one hour: no decay, the popular entry is protected
    assert c.get("was_popular") is not None
    for day in range(1, 9):  # then it is never used again; every few hours the counts are halved
        now[0] += 3 * 3600
        fill(c, 60, prefix=f"d{day}_")
    survivors = db.execute("SELECT hits FROM kv WHERE key='was_popular'").fetchone()
    assert survivors is None or survivors[0] < 40  # faded, and eventually evictable


def test_a_hot_hit_issues_no_sql(tmp_path):
    c = make(tmp_path)
    c.put("k", {"v": 1})
    c.get("k")  # first read may touch the database and promotes the entry into memory
    statements = []
    c._db.set_trace_callback(statements.append)
    for _ in range(10):
        assert c.get("k") == {"v": 1}
    assert statements == []  # O(1): a hash-map read


def test_hit_counts_are_batched_not_written_per_read_and_survive_a_restart(tmp_path):
    c = make(tmp_path)
    c.put("k", {"v": 1})
    statements = []
    c._db.set_trace_callback(statements.append)
    for _ in range(20):
        c.get("k")
    assert not any(s.startswith("UPDATE") for s in statements)  # nothing written yet
    c.close()  # flushes
    again = make(tmp_path)
    hits = sqlite3.connect(tmp_path / "c.db").execute("SELECT hits FROM kv WHERE key='k'").fetchone()[0]
    assert hits == 20 and again.get("k") == {"v": 1}


def test_lookups_and_eviction_use_indexes_not_table_scans(tmp_path):
    c = make(tmp_path)
    c.put("k", {"v": 1}, tag="m1")
    plans = {
        "get": ("SELECT value FROM kv WHERE ns=? AND key=?", ("decisions", "k")),
        "evict": ("SELECT key, size FROM kv WHERE ns=? AND key<>? ORDER BY hits ASC, last_used ASC LIMIT 32", ("decisions", "k")),
        "tag": ("SELECT key, size FROM kv WHERE ns=? AND tag=?", ("decisions", "m1")),
    }
    for name, (sql, args) in plans.items():
        plan = " ".join(r[3] for r in c._db.execute("EXPLAIN QUERY PLAN " + sql, args)).upper()
        assert "USING" in plan, f"{name}: {plan}"
        assert "USE TEMP B-TREE" not in plan, f"{name} sorts at query time: {plan}"


def test_invalidating_a_tag_drops_exactly_those_entries(tmp_path):
    c = make(tmp_path, max_bytes=1_000_000)
    for i in range(5):
        c.put(f"old{i}", {"v": i}, tag="model-old")
    for i in range(3):
        c.put(f"new{i}", {"v": i}, tag="model-new")
    assert c.tags() == {"model-old": 5, "model-new": 3}
    assert c.invalidate("model-old") == 5 and len(c) == 3
    assert c.get("old0") is None and c.get("new0") == {"v": 0}
    c.put("x", {"v": 1}, tag="other")
    assert c.invalidate_except({"model-new"}) == 1 and c.tags() == {"model-new": 3}


def test_one_oversized_value_cannot_crowd_out_the_cache(tmp_path):
    c = make(tmp_path, max_bytes=4000)
    assert c.put("huge", {"v": "x" * 3000}) is False and len(c) == 0
    assert c.put("fine", {"v": "x" * 100}) is True


def test_an_older_store_without_hit_counts_is_upgraded_in_place(tmp_path):
    db = sqlite3.connect(tmp_path / "old.db")
    db.execute("CREATE TABLE kv (ns TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY (ns, key))")
    db.execute("INSERT INTO kv VALUES ('decisions', 'legacy', '{\"v\": 7}')")
    db.commit(); db.close()
    c = KVCache(tmp_path / "old.db", "decisions")
    assert c.get("legacy") == {"v": 7} and c.stats()["bytes"] >= ROW_OVERHEAD


def test_namespaces_have_independent_budgets(tmp_path):
    a = make(tmp_path, max_bytes=4000, ns="decisions")
    b = KVCache(tmp_path / "c.db", "drafts", max_bytes=4000)
    fill(a, 100)
    b.put("only", {"v": 1})
    assert b.get("only") == {"v": 1} and a.stats()["evictions"] > 0 and b.stats()["evictions"] == 0
