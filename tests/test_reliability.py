"""vm-agent reliability layer tests (phases 26-60).

Runs against the source tree directly — no root, no live install needed.
PYTHONPATH=src python3 -m pytest tests/test_reliability.py -v
"""
import json
import os
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.state import Store
from src.locks import WaitGraph, detect_and_recover
from src.txn import TxnRunner
from src.net import classify as net_classify, should_retry, backoff_delay
from src.integrity import record as integrity_record, verify as integrity_verify
from src.deps import check_all as deps_check_all
from src.resources import pressure_level, sample
from src.model import validate_action, classify_model_failure
from src.world import record_observation, record_claim, reconcile as world_reconcile
from src.recovery import RecoveryManager


@pytest.fixture()
def tmp_home(tmp_path):
    home = tmp_path / "vm-agent"
    for d in ("state/journal", "config", "checkpoints", "logs", "run"):
        (home / d).mkdir(parents=True)
    return str(home)


@pytest.fixture()
def store(tmp_home):
    cfg = {"state_db": os.path.join(tmp_home, "state", "state.db"),
           "journal_dir": os.path.join(tmp_home, "state", "journal")}
    s = Store(cfg["state_db"], cfg["journal_dir"])
    yield s
    s.close()


# ---- locks (27) ----
def test_deadlock_cycle_detected():
    g = WaitGraph()
    g.acquire("T2", "L1")
    g.acquire("T1", "L2")
    g.wait_for("T1", "L1")  # T1 waits for T2's lock
    g.wait_for("T2", "L2")  # T2 waits for T1's lock
    assert g.find_cycle() is not None


def test_no_deadlock_no_cycle():
    g = WaitGraph()
    g.acquire("T2", "L1")
    g.wait_for("T1", "L1")
    g.wait_for("T3", "L1")
    assert g.find_cycle() is None


def test_stale_lock_reaped(store):
    store.acquire_lock("res-A", owner="dead-worker", pid=99999999,
                       operation="test", ttl_s=1)
    assert len(store.list_locks()) == 1
    time.sleep(1.2)
    reaped = store.reap_stale_locks()
    assert reaped == ["res-A"]
    assert store.list_locks() == []


def test_live_lock_not_reaped(store):
    store.acquire_lock("res-B", owner="me", pid=os.getpid(),
                       operation="test", ttl_s=300)
    assert store.reap_stale_locks() == []
    store.release_lock("res-B", owner="me")


def test_deadlock_detector_recovery(store):
    g = WaitGraph()
    g.acquire("w2", "L1")
    g.acquire("w1", "L2")
    g.wait_for("w1", "L1")
    g.wait_for("w2", "L2")
    events = []
    result = detect_and_recover(store, g,
                                lambda event, **kw: events.append((event, kw)))
    assert result is not None and "victim" in result
    assert events[0][0] == "DEADLOCK_DETECTED"


# ---- transactions + op registry (29/30/31) ----
class FakeEx:
    def run(self, tool, args=None, task_id=None):
        class R:
            ok = True
            stdout = "ok"
            stderr = ""
        return R()


class FakeVerifier:
    def verify_step(self, step):
        class V:
            passed = True

            def to_dict(self):
                return {"passed": True}
        return V()


def test_op_registry_idempotent_skip(store):
    txn = TxnRunner(store, FakeEx(), FakeVerifier())
    store.op_set("op-1", "t1", "shell", "COMPLETED", result={"x": 1})
    run, reason = txn.should_run("op-1")
    assert run is False
    assert "already completed" in reason


def test_op_registry_unknown_reconciles_true(store):
    txn = TxnRunner(store, FakeEx(), FakeVerifier())
    store.op_set("op-2", "t1", "shell", "UNKNOWN")
    run, reason = txn.should_run("op-2",
                                 reconcile_fn=lambda: (True, {"evidence": 1}))
    assert run is False  # reconciled as done -> skip
    op = store.op_get("op-2")
    assert op["status"] == "COMPLETED"


def test_op_registry_unknown_reconciles_false_reruns(store):
    txn = TxnRunner(store, FakeEx(), FakeVerifier())
    store.op_set("op-3", "t1", "shell", "UNKNOWN")
    run, _ = txn.should_run("op-3",
                            reconcile_fn=lambda: (False, {}))
    assert run is True
    op = store.op_get("op-3")
    assert op["status"] == "PENDING"  # marked retryable


def test_op_registry_unknown_no_reconcile_pauses(store):
    txn = TxnRunner(store, FakeEx(), FakeVerifier())
    store.op_set("op-4", "t1", "shell", "UNKNOWN")
    run, reason = txn.should_run("op-4")
    assert run is False
    assert "paused" in reason or "ambiguous" in reason


def test_txn_success_path(store):
    txn = TxnRunner(store, FakeEx(), FakeVerifier())
    res = txn.run_txn("op-tx", "t1", "shell",
                      prepare_fn=lambda: {"ok": True},
                      execute_fn=lambda: {"ok": True},
                      verify_fn=lambda r: {"passed": True},
                      rollback_fn=lambda r: None)
    assert res.ok is True and res.phase == "commit"
    assert store.op_get("op-tx")["status"] == "COMPLETED"


def test_txn_verify_fail_rolls_back(store):
    rolled = []
    txn = TxnRunner(store, FakeEx(), FakeVerifier())
    res = txn.run_txn("op-tx2", "t1", "shell",
                      prepare_fn=lambda: {"ok": True},
                      execute_fn=lambda: {"ok": True, "partial": 1},
                      verify_fn=lambda r: {"passed": False},
                      rollback_fn=lambda r: rolled.append(r))
    assert res.ok is False and res.rolled_back is True
    assert rolled and rolled[0]["partial"] == 1
    assert store.op_get("op-tx2")["status"] == "FAILED"


# ---- leases (32) ----
def test_lease_claim_and_heartbeat(store):
    me = f"agent:{os.getpid()}"
    assert store.claim_lease("task-9", worker_id="w1", owner=me) is True
    assert store.heartbeat_lease("task-9", worker_id="w1") is True
    # another worker cannot steal a live lease from a live owner
    assert store.claim_lease("task-9", worker_id="w2",
                             owner="agent:99999999") is False
    store.release_lease("task-9")
    assert store.claim_lease("task-9", worker_id="w2",
                             owner="agent:99999999") is True


# ---- network classification (43) ----
@pytest.mark.parametrize("text,kind", [
    ("Name or service not known", "dns_failure"),
    ("Network is unreachable", "no_internet"),
    ("Connection timed out", "connection_timeout"),
    ("certificate verify failed", "tls_failure"),
    ("HTTP 429 too many requests", "rate_limited"),
    ("503 Service Unavailable", "remote_server_error"),
    ("HTTP 401 Unauthorized", "auth_failure"),
    ("some weird new error", "unknown"),
])
def test_net_classify(text, kind):
    assert net_classify(text) == kind


@pytest.mark.parametrize("text,kind", [
    # loose numeric substrings must not misfire on PIDs / byte counts
    ("process 40123 exited with code 1", "unknown"),
    ("wrote 429 bytes to /tmp/x", "unknown"),
    ("command failed with status 500", "unknown"),
    ("error 5003 in module foo", "unknown"),
    # genuine HTTP-context failures still classify
    ("HTTP 401 Unauthorized", "auth_failure"),
    ("HTTP Error 403 Forbidden", "auth_failure"),
    ("HTTP 429 Too Many Requests", "rate_limited"),
    ("HTTP 500 Internal Server Error", "remote_server_error"),
    ("HTTP Error 503 Service Unavailable", "remote_server_error"),
])
def test_net_classify_no_false_positives(text, kind):
    assert net_classify(text) == kind


def test_net_retry_policy_shape():
    assert should_retry("connection_timeout", 0) is True
    assert backoff_delay("connection_timeout", 0) > 0
    # auth failures must never be retried blindly
    assert should_retry("auth_failure", 0) is False
    assert backoff_delay("auth_failure", 0) is None
    # backoff grows
    assert backoff_delay("connection_timeout", 2) >= \
        backoff_delay("connection_timeout", 0)


# ---- integrity (45) ----
def test_file_integrity_record_verify(tmp_path, store):
    cfg = tmp_path / "app.json"
    cfg.write_text('{"a": 1}')
    recorded = integrity_record(store, str(tmp_path), paths=["app.json"])
    assert recorded
    assert integrity_verify(store, str(tmp_path), paths=["app.json"]) == []
    cfg.write_text('{"a": 2}')
    bad = integrity_verify(store, str(tmp_path), paths=["app.json"])
    assert len(bad) == 1 and bad[0]["issue"] == "modified"


# ---- deps (26) ----
def test_deps_check_python3(store, tmp_home):
    results = deps_check_all(store, {"base_dir": tmp_home})
    ok, detail = results["python3"]
    assert ok is True


def test_deps_disk_writable(store, tmp_home):
    results = deps_check_all(store, {"base_dir": tmp_home})
    assert "disk_writable" in results
    ok, _ = results["disk_writable"]
    assert ok is True


# ---- resources (42) ----
def test_pressure_levels():
    lvl, _ = pressure_level({"disk_free_mb": 50000, "mem_available_mb": 4000,
                             "load_per_cpu": 0.5})
    assert lvl == "ok"
    lvl, reasons = pressure_level({"disk_free_mb": 10,
                                   "mem_available_mb": 4000,
                                   "load_per_cpu": 0.5})
    assert lvl == "critical" and reasons
    lvl, _ = pressure_level({"disk_free_mb": 50000, "mem_available_mb": 10,
                             "load_per_cpu": 0.5})
    assert lvl == "critical"


def test_sample_shape():
    s = sample()
    assert {"disk_free_mb", "mem_available_mb", "load_per_cpu"} <= set(s)


# ---- model adapter (36/37/38) ----
def test_action_validation_ok():
    ok, errs = validate_action({"tool": "shell",
                                "args": {"command": "echo hi"}})
    assert ok and not errs


def test_action_validation_bad():
    ok, errs = validate_action({"tool": "nope"})
    assert not ok and errs
    ok, errs = validate_action({"tool": "shell", "args": {}})
    assert not ok  # missing command


def test_model_failure_classify():
    assert classify_model_failure("request timed out") == "timeout"
    assert classify_model_failure("429 rate limit") == "rate_limit"
    assert classify_model_failure("invalid api key") == "auth_failure"


# ---- world state (34/35) ----
def test_world_verifier_only(store):
    record_observation(store, "svc.nginx", {"running": True},
                       verifier="health_probe")
    got = store.world_get("svc.nginx")
    assert got["running"] is True
    full = store.world_all()["svc.nginx"]
    assert full["verifier"] == "health_probe"


def test_claim_vs_observation(store):
    record_claim(store, "t1", {"svc": "up"})
    result = world_reconcile(store, "t1", {"svc": "up"},
                             {"svc": ("down", "probe")})
    assert result["mismatches"]
    assert result["mismatches"][0]["claimed"] == "up"
    assert result["mismatches"][0]["observed"] == "down"


# ---- recovery escalation (40/41) ----
def _rm_cfg(tmp_home):
    return {"base_dir": tmp_home, "safe_mode_max_l3_retries": 1,
            "lock_ttl_s": 120, "intervention_ttl_hours": 24}


def test_recovery_escalates_to_safe_mode(store, tmp_home):
    rm = RecoveryManager(store, _rm_cfg(tmp_home))
    assert rm.in_safe_mode() is False
    # level 7+ enters safe mode: 10+ consecutive failures -> L7
    for _ in range(11):
        rm.record_failure("t1", "shell")
    level = rm.recover("t1", "shell", detail="boom")
    assert level >= 7
    assert rm.in_safe_mode() is True
    rm.exit_safe_mode("test")
    assert rm.in_safe_mode() is False


def test_recovery_l1_first_failure(store, tmp_home):
    rm = RecoveryManager(store, _rm_cfg(tmp_home))
    rm.record_failure("t1", "shell")
    level = rm.recover("t1", "shell", detail="Connection timed out")
    assert level == 1
    assert rm.in_safe_mode() is False


def test_recovery_success_resets_counter(store, tmp_home):
    rm = RecoveryManager(store, _rm_cfg(tmp_home))
    rm.record_failure("t1", "shell")
    rm.record_failure("t1", "shell")
    rm.record_success("t1", "shell")
    rm.record_failure("t1", "shell")
    assert rm.level_for("t1", "shell") == 1


# ---- budgets (39) ----
def test_budget_consume_and_exhaust(store):
    store.budget_set("t1", "tool_calls", 2)
    assert store.budget_consume("t1", "tool_calls", 1)[0] is True
    assert store.budget_consume("t1", "tool_calls", 1)[0] is True
    # third consume exceeds: returns (False, used_including_this, limit)
    ok, used, limit = store.budget_consume("t1", "tool_calls", 1)
    assert ok is False and used == 3 and limit == 2
    # no budget configured = unlimited
    assert store.budget_consume("t1", "other_kind", 1) == (True, 0, None)


# ---- interventions (46) ----
def test_intervention_open_resolve(store):
    iid = store.intervention_open("t1", reason="disk full",
                                  required_action="free space",
                                  last_verified_step="step 3")
    open_ivs = store.interventions_open()
    assert any(i["id"] == iid for i in open_ivs)
    store.intervention_resolve(iid, note="freed 2GB")
    assert not any(i["id"] == iid for i in store.interventions_open())


# ---- audit (47) ----
def test_audit_trail(store):
    store.audit(actor="t1", action="op-1", task_id="t1",
                args={"command": "echo hi"}, result="ok")
    rows = store._conn.execute("SELECT * FROM audit").fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0]["args_json"])["command"] == "echo hi"
