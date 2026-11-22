"""The dashboard server: locked down, and unable to take itself (or a run) down."""
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from qra.core.config import Settings
from qra.server.app import QraServer
from qra.server.jobs import JobManager


class StubService:
    """Stands in for qra.runtime.service so no network or model is touched."""
    def __init__(self, boom=False):
        self.boom, self.calls = boom, []

    def execute_run(self, s, **kw):
        self.calls.append(("run", {**kw, "retrieval": s.retrieval}))
        if self.boom:
            raise RuntimeError("kaboom")
        from qra.runtime.service import RunResult
        return RunResult(kw["run_id"], 0, "", {"tickets": 3})

    def execute_reset(self, s):
        self.calls.append(("reset", {}))
        from qra.runtime.service import RunResult
        return RunResult("r", 0, "helpdesk reset")

    def execute_eval(self, s, out=None):
        from qra.runtime.service import RunResult
        return RunResult("r", 0, "", {"results": {}, "live_calls": 0, "cache_hits": 0})


@pytest.fixture
def srv(tmp_path):
    s = Settings(helpdesk_url="https://h.test", helpdesk_key="k", candidate_email="me@example.com",
                 state_dir=tmp_path / "state", log_dir=tmp_path / "logs")
    stub = StubService()
    server = QraServer(("127.0.0.1", 0), s, token="TESTTOKEN", jobs=JobManager(s, runner=stub))
    server.stub = stub
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def call(srv, path, *, method="GET", cookie=True, host=None, body=None, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{srv.port}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    if cookie:
        req.add_header("Cookie", "qra_token=TESTTOKEN")
    if host:
        req.add_header("Host", host)
    for k, v in (headers if headers is not None else {"X-QRA": "1"}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_the_api_needs_the_token_cookie(srv):
    assert call(srv, "/api/health", cookie=False)[0] == 403
    assert call(srv, "/api/health")[0] == 200


def test_a_foreign_host_header_is_refused_dns_rebinding_guard(srv):
    assert call(srv, "/api/health", host="evil.example.com")[0] == 403


def test_the_one_time_token_url_sets_a_cookie_and_redirects(srv):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    try:
        opener.open(f"http://127.0.0.1:{srv.port}/?t=TESTTOKEN", timeout=5)
        raise AssertionError("expected a redirect")
    except urllib.error.HTTPError as e:
        assert e.code == 302 and "HttpOnly" in e.headers["Set-Cookie"] and "SameSite=Strict" in e.headers["Set-Cookie"]
    assert call(srv, "/?t=wrong", cookie=False)[0] == 403


def test_posts_need_the_custom_header_and_a_same_origin(srv):
    assert call(srv, "/api/job", method="POST", body={"kind": "eval"}, headers={})[0] == 403
    assert call(srv, "/api/job", method="POST", body={"kind": "eval"},
                headers={"X-QRA": "1", "Origin": "http://evil.example.com"})[0] == 403


def test_a_real_run_and_a_reset_need_the_typed_confirmation(srv):
    assert call(srv, "/api/job", method="POST", body={"kind": "run", "dry_run": False})[0] == 400
    assert call(srv, "/api/job", method="POST", body={"kind": "reset"})[0] == 400
    assert srv.stub.calls == []
    assert call(srv, "/api/job", method="POST", body={"kind": "run", "dry_run": False, "confirm": "RUN"})[0] == 200


def test_dry_run_is_the_default_and_needs_no_confirmation(srv):
    code, _ = call(srv, "/api/job", method="POST", body={"kind": "run"})
    assert code == 200
    time.sleep(0.3)
    assert srv.stub.calls[0][1]["dry_run"] is True


def test_only_one_job_at_a_time(srv):
    gate = threading.Event()
    orig = srv.stub.execute_eval

    def slow(s, out=None):
        gate.wait(2)
        return orig(s)
    srv.stub.execute_eval = slow
    assert call(srv, "/api/job", method="POST", body={"kind": "eval"})[0] == 200
    assert call(srv, "/api/job", method="POST", body={"kind": "eval"})[0] == 409
    gate.set()


def test_a_crashing_job_is_reported_and_the_server_keeps_serving(srv):
    srv.stub.boom = True
    assert call(srv, "/api/job", method="POST", body={"kind": "run"})[0] == 200
    deadline = time.time() + 3
    while time.time() < deadline and srv.jobs.status()["status"] != "idle":
        time.sleep(0.05)
    last = srv.jobs.status()["last"]
    assert last["exit_code"] == 1 and "still running" in last["message"]
    assert call(srv, "/api/health")[0] == 200  # still up


def test_unknown_routes_and_path_traversal_are_refused(srv):
    assert call(srv, "/api/nope")[0] == 404
    assert call(srv, "/static/../app.py", cookie=False)[0] == 404
    assert call(srv, "/static/%2e%2e/app.py", cookie=False)[0] == 404


def test_an_empty_telemetry_store_gives_an_empty_snapshot_not_an_error(srv):
    code, body = call(srv, "/api/snapshot")
    assert code == 200 and json.loads(body)["run"] is None


def test_cache_stats_and_clear_are_guarded_and_work(srv):
    from qra.infra.cache import KVCache
    cache = KVCache(srv.settings.state_dir / "cache.db", "decisions")
    cache.put("k", {"v": 1})
    code, body = call(srv, "/api/cache")
    assert code == 200 and json.loads(body)["namespaces"]["decisions"]["entries"] == 1
    assert call(srv, "/api/cache", method="POST", body={"namespace": "decisions"}, headers={})[0] == 403  # needs the custom header
    assert call(srv, "/api/cache", method="POST", body={"namespace": "bogus"})[0] == 400
    code, body = call(srv, "/api/cache", method="POST", body={"namespace": "decisions"})
    assert code == 200 and json.loads(body)["removed"] == {"decisions": 1}
    assert cache.get("k") is None


def _raw(srv, path, headers):
    req = urllib.request.Request(f"http://127.0.0.1:{srv.port}{path}")
    req.add_header("Cookie", "qra_token=TESTTOKEN")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_built_assets_are_compressed_cached_forever_and_revalidated(srv):
    import gzip
    from qra.server.app import STATIC
    name = next(p.name for p in (STATIC / "assets").glob("*.js"))
    code, h, body = _raw(srv, f"/static/assets/{name}", {"Accept-Encoding": "gzip"})
    assert code == 200 and h["Content-Encoding"] == "gzip" and "immutable" in h["Cache-Control"]
    assert len(body) < (STATIC / "assets" / name).stat().st_size / 2
    assert gzip.decompress(body) == (STATIC / "assets" / name).read_bytes()
    code, _, body = _raw(srv, f"/static/assets/{name}", {"Accept-Encoding": "gzip", "If-None-Match": h["ETag"]})
    assert code == 304 and body == b""
    code, h2, plain = _raw(srv, f"/static/assets/{name}", {})  # a client that cannot gunzip gets the plain file
    assert code == 200 and "Content-Encoding" not in h2 and plain == (STATIC / "assets" / name).read_bytes()


def test_the_page_itself_is_revalidated_not_cached_blindly(srv):
    code, h, _ = _raw(srv, "/", {"Accept-Encoding": "gzip"})
    assert code == 200 and h["Cache-Control"] == "no-cache" and h["ETag"]
    assert _raw(srv, "/", {"If-None-Match": h["ETag"]})[0] == 304


def test_api_json_is_compressed_when_large_and_asked_for(srv):
    code, h, _ = _raw(srv, "/api/snapshot", {"Accept-Encoding": "gzip"})
    assert code == 200 and h["Cache-Control"] == "no-store"  # live data is never cached


def test_trace_and_fault_lookups_are_validated_and_answer_from_the_indexes(srv):
    from qra.runtime.service import telemetry_path
    from qra.telemetry.events import Telemetry
    t = Telemetry(telemetry_path(srv.settings), "r1")
    t.emit("ticket", ticket="K7", outcome="sent", latency_ms=4)
    t.emit("fault", stage="draft", ticket="K7", error_class="draft_error", outcome="retried")
    code, body = call(srv, "/api/trace?ticket=K7")
    assert code == 200 and [e["kind"] for e in json.loads(body)["events"]] == ["ticket", "fault"]
    code, body = call(srv, "/api/faults?class=draft_error")
    assert code == 200 and json.loads(body)["events"][0]["ticket"] == "K7"
    assert call(srv, "/api/trace?ticket=" + "x" * 100)[0] == 400
    assert call(srv, "/api/trace?ticket=a%27%3Bdrop")[0] == 400
    assert call(srv, "/api/trace")[0] == 400
    assert call(srv, "/api/trace?ticket=K7", cookie=False)[0] == 403


def test_the_retrieval_method_can_be_chosen_per_job_and_defaults_to_the_original(srv):
    code, body = call(srv, "/api/job", method="POST", body={"kind": "run", "dry_run": True, "retrieval": "cascade"})
    assert code == 200
    deadline = time.time() + 5
    while srv.stub.calls == [] and time.time() < deadline:
        time.sleep(0.05)
    assert srv.stub.calls[0][1]["retrieval"] == "cascade"
    while json.loads(call(srv, "/api/job")[1])["status"] == "running" and time.time() < deadline:
        time.sleep(0.05)
    call(srv, "/api/job", method="POST", body={"kind": "run", "dry_run": True})
    while len(srv.stub.calls) < 2 and time.time() < deadline:
        time.sleep(0.05)
    assert srv.stub.calls[1][1]["retrieval"] == "exhaustive"
    assert srv.jobs.s.retrieval == "exhaustive"  # the server's own default is never changed by a job


def test_an_unknown_retrieval_method_is_refused(srv):
    code, body = call(srv, "/api/job", method="POST", body={"kind": "run", "dry_run": True, "retrieval": "magic"})
    assert code == 400 and srv.stub.calls == []


def test_the_final_run_needs_a_typed_confirmation_and_resets_before_it_runs(srv):
    assert call(srv, "/api/job", method="POST", body={"kind": "final", "dry_run": False})[0] == 400
    assert call(srv, "/api/job", method="POST", body={"kind": "final", "dry_run": False, "confirm": "RUN"})[0] == 400
    assert srv.stub.calls == []  # nothing was touched by the refused requests
    code, _ = call(srv, "/api/job", method="POST", body={"kind": "final", "dry_run": False, "confirm": "FINAL"})
    assert code == 200
    deadline = time.time() + 5
    while len(srv.stub.calls) < 2 and time.time() < deadline:
        time.sleep(0.05)
    assert [c[0] for c in srv.stub.calls] == ["reset", "run"]  # the helpdesk is reset first, then every ticket is answered
    assert srv.stub.calls[1][1]["dry_run"] is False


def test_the_decision_model_can_be_chosen_per_job_and_needs_its_key(srv):
    assert call(srv, "/api/job", method="POST", body={"kind": "run", "dry_run": True, "judge": "magic"})[0] == 400
    code, body = call(srv, "/api/job", method="POST", body={"kind": "run", "dry_run": True, "judge": "typesafe"})
    assert code == 409 and "TYPESAFE_API_KEY" in json.loads(body)["error"]  # no key in the test settings: refused, nothing started
    assert srv.stub.calls == []
