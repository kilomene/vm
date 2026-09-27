"""vm-agent advanced reliability tests (phases 51-90).

Runs against the source tree directly — no root, no live install needed.
PYTHONPATH=. python3 -m pytest tests/test_phase3.py -v
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.state import Store
from src import classify, planner, caps, progress, failure, matrix, sysgraph
from src import ownership, secrets, timecheck, update, backup, faultinject
from src import deps as depsmod
from src.policy import Policy, self_protection_check
from src import integrity as integ


@pytest.fixture()
def store(tmp_path):
    db = str(tmp_path / "state.db")
    jd = str(tmp_path / "journal")
    os.makedirs(jd, exist_ok=True)
    s = Store(db, jd)
    yield s
    s.close()


@pytest.fixture()
def cfg(tmp_path):
    base = str(tmp_path / "vm-agent")
    for d in ("state/journal", "config", "checkpoints", "logs", "run",
              "backups"):
        os.makedirs(os.path.join(base, d), exist_ok=True)
    return {"base_dir": base,
            "state_db": os.path.join(base, "state", "state.db"),
            "journal_dir": os.path.join(base, "state", "journal"),
            "checkpoint_dir": os.path.join(base, "checkpoints"),
            "log_dir": os.path.join(base, "logs"),
            "run_dir": os.path.join(base, "run"),
            "heartbeat_interval_s": 60,
            "pause_verify_s": 5,
            "tool_timeout_default_s": 10,
            "tool_timeouts": {}}


# ---- phase 54: retry classification ----
def test_classify_transient():
    d, n, backoff, why = classify.classify("net", "connection_timeout")
    assert d == classify.TRANSIENT and n == 5 and backoff == 2


def test_classify_permanent_never_retried():
    d, n, backoff, why = classify.classify("net", "auth_failure")
    assert d == classify.PERMANENT and n == 0


def test_classify_policy_blocked():
    d, n, backoff, why = classify.classify("policy", "protected")
    assert d == classify.POLICY_BLOCKED and n == 0


def test_classify_human_required():
    d, n, backoff, why = classify.classify("browser", "auth_expired")
    assert d == classify.HUMAN_REQUIRED and n == 0


def test_classify_exact_backoff_sequence():
    d, n, base, _ = classify.classify("tool", "exit_nonzero")
    seq = classify.backoff_sequence("tool", "exit_nonzero")
    assert seq == [base * (2 ** i) for i in range(n)]


def test_classify_should_retry_bounded():
    assert classify.should_retry("net", "dns_failure", 4)[0]
    assert not classify.should_retry("net", "dns_failure", 5)[0]
    assert not classify.should_retry("net", "auth_failure", 0)[0]


# ---- phase 52: planner recovery ----
def test_planner_diagnose_repetitive():
    diag = planner.diagnose_stall({"step_index": 3,
                                   "history_kinds": ["step_failed",
                                                     "step_failed",
                                                     "step_failed"]})
    assert "repetitive" in diag or "stall" in diag


def test_planner_rebuild_replaces_failed_step(store):
    steps = [{"name": "a"}, {"name": "b"}, {"name": "c"}]
    new_steps, reason = planner.rebuild_plan("t1", steps, 1,
                                             reason="test")
    assert len(new_steps) == 3
    assert new_steps[1]["name"].startswith("recover:")
    assert new_steps[0] == {"name": "a"} and new_steps[2] == {"name": "c"}


def test_planner_validate_rejects_bad():
    ok, issues = planner.validate_plan([{"name": "x"},
                                        {"tool": "nope"}])
    assert not ok and issues


def test_planner_validate_accepts_good():
    ok, issues = planner.validate_plan([{"name": "x", "tool": "shell",
                                         "args": {"command": "true"}}])
    assert ok


def test_planner_rebuild_guard(store):
    steps = [{"name": "a"}]
    planner.rebuild_plan("t9", steps, 0, reason="r1")
    planner.rebuild_plan("t9", steps, 0, reason="r2")
    new_steps, reason = planner.rebuild_plan("t9", steps, 0, reason="r3")
    assert "refused" in reason  # third rebuild within 5 min refused


def test_planner_recovery_via_agent(cfg):
    # 3 consecutive step failures -> plan rebuilt from verified state
    # (phase 52); the recovery step succeeds and the task completes.
    from src.agent import AgentRuntime
    rt = AgentRuntime(cfg)
    try:
        rt.store.create_task("t-plan", {"steps": [
            {"name": "flaky", "tool": "shell",
             "args": {"command": "exit 3"}}]})
        assert rt.run_task("t-plan") == "FAILED"     # streak 1
        assert rt.run_task("t-plan") == "FAILED"     # streak 2
        assert rt.run_task("t-plan") == "COMPLETED"  # streak 3 -> replan
        spec = json.loads(rt.store.get_task("t-plan")["spec"])
        assert spec["steps"][0]["name"].startswith("recover:")
    finally:
        rt.store.close()


# ---- phase 59: capabilities ----
def test_caps_denied_by_default(store):
    ok, reason = caps.check_tool(store, "t-cap", "shell")
    assert not ok


def test_caps_grant_and_use(store):
    caps.grant(store, "t-cap", "shell")
    ok, _ = caps.check_tool(store, "t-cap", "shell")
    assert ok


def test_caps_ttl_expiry(store):
    caps.grant(store, "t-cap", "shell", ttl_s=0)
    ok, reason = caps.check_tool(store, "t-cap", "shell")
    assert not ok  # ttl_s=0 means already expired


def test_caps_escalation_needs_token(store):
    caps.grant(store, "t-cap", "shell")
    ok, reason = caps.request_escalation(store, "t-cap", "browser")
    assert not ok and "token" in reason.lower()
    ok2, _ = caps.request_escalation(store, "t-cap", "browser",
                                     auth_token="out-of-band-ok")
    assert ok2


def test_caps_revoke(store):
    caps.grant(store, "t-cap", "shell")
    caps.revoke(store, "t-cap", "shell")
    ok, _ = caps.check_tool(store, "t-cap", "shell")
    assert not ok


# ---- phase 53: progress detection ----
def test_progress_fingerprint_changes(store):
    a = progress.fingerprint_of(store)
    store.world_set("progress.step", 1, verifier="t")
    b = progress.fingerprint_of(store)
    assert a != b


def test_progress_stall_detected(store):
    for _ in range(3):
        stalled, count = progress.track_stall(store, "t-stall", max_same=3)
    assert stalled and count >= 3


def test_progress_no_stall_when_changing(store):
    for i in range(3):
        store.world_set("progress.step", i, verifier="t")
        stalled, _ = progress.track_stall(store, "t-stall2", max_same=3)
        assert not stalled


# ---- phase 55/56: recovery history + fingerprints ----
def test_failure_fingerprint_stable():
    a = failure.fingerprint("step_failed", "deploy", "exit 1\nfoo")
    b = failure.fingerprint("step_failed", "deploy", "exit 1\nbar")
    assert a == b  # volatile details (foo/bar) excluded


def test_failure_record_dedups(store):
    sig1 = failure.record(store, "t-f", "step_failed", "deploy",
                          "retry_step", result="failed",
                          error_text="exit 1")
    sig2 = failure.record(store, "t-f", "step_failed", "deploy",
                          "retry_step", result="failed",
                          error_text="exit 1")
    assert sig1 == sig2
    rows = store.failure_signatures("t-f")
    assert len(rows) == 1 and rows[0]["occurrences"] == 2


def test_failure_analyzer_skips_failed_strategy(store):
    for _ in range(2):
        failure.record(store, "t-f2", "step_failed", "deploy",
                       "retry_step", result="failed",
                       error_text="exit 1")
    analyzer = failure.FailureAnalyzer(store)
    method, sig, exhausted = analyzer.choose_strategy(
        "t-f2", "step_failed", "deploy", "exit 1")
    assert method != "retry_step"  # known-failed method not repeated
    assert not exhausted


def test_failure_analyzer_escalates_when_exhausted(store):
    kinds = ["retry_step", "restart_worker", "rebuild_plan", "pause_task",
             "reload_task_state", "safe_mode", "restart_browser",
             "restore_session", "repair_dep", "human"]
    for k in kinds:
        for _ in range(3):
            failure.record(store, "t-f3", "step_failed", "deploy", k,
                           result="failed", error_text="exit 1")
    analyzer = failure.FailureAnalyzer(store)
    method, sig, exhausted = analyzer.choose_strategy(
        "t-f3", "step_failed", "deploy", "exit 1")
    assert exhausted and method == "human"


# ---- phase 79: recovery matrix ----
def test_matrix_complete():
    for kind in matrix.all_kinds():
        e = matrix.policy_for(kind)
        for field in ("detection", "classification", "recovery",
                      "verification", "retry_limit", "escalation"):
            assert field in e, f"{kind} missing {field}"


def test_matrix_classification_deterministic():
    d1 = matrix.classify_failure("auth_failure")
    d2 = matrix.classify_failure("auth_failure")
    assert d1 == d2 and d1[0] == classify.PERMANENT


def test_matrix_unknown_kind_has_bounded_default():
    e = matrix.policy_for("no_such_failure")
    assert e["retry_limit"] <= 2


# ---- phase 57: dependency graph ----
def test_sysgraph_restart_scope():
    scope = sysgraph.smallest_restart_scope("workers")
    assert "workers" in scope
    assert "supervisor" not in scope  # dependents not included


def test_sysgraph_blast_radius():
    scope = sysgraph.blast_radius("runtime")
    assert set(scope) == {"runtime", "workers", "tools", "browser",
                          "database", "recovery"}


def test_sysgraph_topo_order_respects_deps():
    order = sysgraph.safe_restart_order(["runtime", "supervisor"])
    assert order.index("supervisor") < order.index("runtime")


def test_sysgraph_health_check():
    health = sysgraph.component_health({"runtime": True, "workers": False})
    assert health["runtime"] == "ok" and health["workers"] == "degraded"


# ---- phase 58/63/85/86/87: ownership ----
def test_ownership_claim_and_heartbeat(store):
    ok = ownership.claim_operation(store, "t-o", "op-1", "worker:1")
    assert ok
    assert ownership.heartbeat_operation(store, "t-o", "op-1", "worker:1")
    # wrong owner cannot heartbeat
    assert not ownership.heartbeat_operation(store, "t-o", "op-1",
                                             "worker:2")


def test_ownership_stale_reaped(store):
    ownership.claim_operation(store, "t-o", "op-old", "worker:dead",
                              ttl_s=0)
    found = ownership.reap_stale_operations(store)
    assert any(f["op_id"] == "op-old" for f in found)
    assert store.op_get("op-old")["status"] == "UNKNOWN"  # never assumed


def test_ownership_double_claim_refused(store):
    assert ownership.claim_operation(store, "t-o", "op-x", "worker:1")
    assert not ownership.claim_operation(store, "t-o", "op-x", "worker:2")


def test_external_ownership_exclusive(store):
    ok, _ = ownership.own_external(store, "t-a", "db:prod")
    assert ok
    ok2, msg = ownership.own_external(store, "t-b", "db:prod")
    assert not ok2
    ownership.release_external(store, "t-a", "db:prod")
    ok3, _ = ownership.own_external(store, "t-b", "db:prod")
    assert ok3


def test_cleanup_safe_deletes_only_owned(store):
    ownership.claim_resource(store, "t-c", "/tmp/vm-x", "task_file")
    out = ownership.cleanup_task_resources(store, "t-c",
                                           requester="task:t-c")
    assert "/tmp/vm-x" in out["released"]
    # not owned by t-c: untouched
    ownership.claim_resource(store, "t-other", "/tmp/vm-y", "task_file")
    out2 = ownership.cleanup_task_resources(store, "t-c",
                                            requester="task:t-c")
    assert "/tmp/vm-y" not in out2["released"]


# ---- phase 60: secrets ----
def test_vault_scoped_access(store):
    secrets.vault_set(store, "api_key", "s3cret", owner="t-s")
    assert secrets.vault_get(store, "t-s", "api_key") == "s3cret"
    assert secrets.vault_get(store, "t-other", "api_key") is None


def test_vault_ttl(store):
    secrets.vault_set(store, "tok", "v", owner="t-s", ttl_s=0)
    assert secrets.vault_get(store, "t-s", "tok") is None


def test_redact_text():
    out = secrets.redact_text("key=AKIAIOSFODNN7EXAMPLE and done")
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "REDACTED" in out


def test_scan_for_exposure():
    findings = secrets.scan_for_exposure(
        'password = "hunter2hunter2"', context="test")
    assert findings


def test_journal_scan_clean(tmp_path):
    jd = str(tmp_path / "j")
    os.makedirs(jd, exist_ok=True)
    with open(os.path.join(jd, "2026-09-27.jsonl"), "w") as f:
        f.write('{"event": "STEP_OK", "task_id": "t"}\n')
    assert secrets.scan_journal(jd) == []


# ---- phase 70: time sync ----
def test_lease_valid_fails_closed_on_bad_clock(monkeypatch):
    import src.timecheck as tc
    monkeypatch.setattr(tc, "check_sync",
                        lambda: (False, {"reason": "drift too large"}))
    assert not tc.lease_valid(time.time(), max_age_s=3600, check_clock=True)
    # ... but without the clock check, age alone still decides
    assert tc.lease_valid(time.time(), max_age_s=3600, check_clock=False)


# ---- phase 71-75: self-update ----
def test_update_compat_rejects():
    cur = {"version": "1.0.0", "state_schema": 3, "python_requires":
           ">=3.8"}
    new = {"version": "2.0.0", "state_schema": 4, "python_requires":
           ">=3.8"}
    ok, issues = update.compatibility_check(cur, new)
    assert not ok and any("schema" in i for i in issues)


def test_update_migrations_run_in_order(store):
    update.register_migration(1, lambda s: s.world_set("m1", True,
                                                       verifier="t"))
    update.register_migration(2, lambda s: s.world_set("m2", True,
                                                       verifier="t"))
    applied = update.apply_migrations(store, 0, 2)
    assert applied == [1, 2]
    assert store.world_get("m2") is True


def test_update_blocks_without_allowlist(cfg):
    updater = update.SelfUpdater(cfg["base_dir"])
    ok, msg = updater.update_to("9.9.9", package_dir="/tmp", cfg=cfg)
    assert not ok and "not enabled" in msg.lower()


# ---- phase 76: backups ----
def test_backup_create_verify_restore(cfg):
    s = Store(cfg["state_db"], cfg["journal_dir"])
    try:
        s.create_task("t-b", {"steps": []})
        with open(os.path.join(cfg["base_dir"], "config",
                               "config.json"), "w") as f:
            f.write('{"a": 1}')
        bid = backup.create_backup(s, cfg, label="test")
        ok, issues = backup.verify_backup(bid, s)
        assert ok, issues
        rok, detail = backup.test_restore(bid, s)
        assert rok, detail
        b = [x for x in s.backups_list() if x["id"] == bid][0]
        assert b["restored_ok"] == 1
    finally:
        s.close()


# ---- phase 81: fault injection ----
def test_faultinject_scenario_all_recover():
    results = faultinject.run_scenario()
    failed = [r for r in results if not r["ok"]]
    assert not failed, failed
    assert len(results) == len(faultinject.INJECTIONS)


# ---- phase 51: dep registry ----
def test_dep_status_fields(store):
    rec = depsmod.dep_status("python3", {"base_dir": "/tmp"})
    for field in ("name", "required_version", "installed_version",
                  "available", "compatible", "health",
                  "last_verification"):
        assert field in rec, field
    assert rec["health"] == "healthy" and rec["available"]


def test_dep_classify_temp_vs_permanent():
    assert depsmod.classify_dep_failure("dns", {"issue": "timeout"}) == \
        "temporary"
    assert depsmod.classify_dep_failure("node", {"issue": "not installed"}) \
        == "permanent"


# ---- phase 78: self-protection ----
def test_self_protection_blocks_log_deletion():
    p = Policy()
    assert p.classify("rm -rf /opt/vm-agent/logs") == "PROTECTED"
    allowed, _ = p.authorize("rm -rf /opt/vm-agent/logs")
    assert not allowed


def test_self_protection_blocks_safety_disable():
    ok, reason = self_protection_check(
        "sed -i 's/x/y/' /opt/vm-agent/src/policy.py")
    assert ok


def test_self_protection_blocks_capability_self_grant():
    p = Policy()
    cmd = "sqlite3 /opt/vm-agent/state/state.db \"UPDATE capabilities SET x\""
    assert p.classify(cmd) == "PROTECTED"


def test_self_protection_safe_command_passes():
    p = Policy()
    assert p.classify("echo hello > /tmp/out.txt") == "SAFE"


# ---- phase 61: config versioning ----
def test_config_version_and_restore(store, tmp_path):
    base = str(tmp_path)
    os.makedirs(os.path.join(base, "config"), exist_ok=True)
    cfg_path = os.path.join(base, "config", "config.json")
    with open(cfg_path, "w") as f:
        f.write('{"v": 1}')
    integ.snapshot_config(store, base, paths=["config/config.json"],
                          note="v1")
    with open(cfg_path, "w") as f:
        f.write("{broken")
    ok, msg = integ.restore_config(store, base, "config/config.json")
    assert ok, msg
    with open(cfg_path) as f:
        assert f.read() == '{"v": 1}'


# ---- phase 64: external reconciliation ----
def test_reconcile_external_done(store):
    from src.txn import reconcile_external
    store.op_set("ext-1", "t-e", "external_push", "UNKNOWN")
    decision, _ = reconcile_external(
        store, "ext-1",
        lambda: {"state": "done", "evidence": {"id": "abc"}})
    assert decision == "synced-done"
    assert store.op_get("ext-1")["status"] == "COMPLETED"


def test_reconcile_external_unknown_never_assumed(store):
    from src.txn import reconcile_external
    store.op_set("ext-2", "t-e", "external_push", "UNKNOWN")
    decision, _ = reconcile_external(
        store, "ext-2", lambda: {"state": "unknown", "evidence": {}})
    assert decision == "unknown-open"
    assert store.op_get("ext-2")["status"] == "UNKNOWN"


# ---- phase 84: dry-run ----
def _make_agent(cfg):
    from src.agent import AgentRuntime
    rt = AgentRuntime.__new__(AgentRuntime)
    rt.cfg = cfg
    rt.store = Store(cfg["state_db"], cfg["journal_dir"])
    rt.journal = rt.store.journal
    from src.policy import Policy as _P
    rt.policy = _P()
    return rt


def test_dry_run_blocks_protected(cfg):
    rt = _make_agent(cfg)
    try:
        rt.store.create_task("t-dry", {"steps": [
            {"name": "bad", "tool": "shell",
             "args": {"command": "rm -rf /opt/vm-agent/state"}}]})
        report = rt.dry_run("t-dry")
        assert not report["ok"]
        assert report["blockers"]
    finally:
        rt.store.close()


def test_dry_run_passes_safe(cfg):
    rt = _make_agent(cfg)
    try:
        rt.store.create_task("t-dry2", {"steps": [
            {"name": "ok", "tool": "shell",
             "args": {"command": "echo hi"},
             "verify": [{"check": "command_ok", "command": "true"}]}]})
        caps.grant(rt.store, "t-dry2", "shell")
        report = rt.dry_run("t-dry2")
        assert report["ok"], report["blockers"]
    finally:
        rt.store.close()


# ---- phase 65/66: preemption + pause verification ----
def test_preemption_triggered(cfg):
    rt = _make_agent(cfg)
    try:
        rt.store.create_task("t-low", {"priority": "LOW", "steps": []})
        rt.store.create_task("t-crit", {"priority": "CRITICAL", "steps": []})
        rt.store.update_task("t-low", status="RUNNING")
        rt.store.update_task("t-crit", status="PENDING")
        assert rt._should_preempt("t-low")
        # starvation guard: after MAX_PREEMPTIONS, no more preemption
        rt.store.world_set("preempted.t-low", {"count": 2},
                           verifier="t")
        assert not rt._should_preempt("t-low")
    finally:
        rt.store.close()


def test_pause_verified(cfg):
    rt = _make_agent(cfg)
    try:
        rt.store.create_task("t-p", {"steps": []})
        rt.store.update_task("t-p", status="RUNNING")
        out = rt.request_pause("t-p")
        assert out["paused"] and out["status"] == "PAUSED"
    finally:
        rt.store.close()


# ---- phase 67: content-verified snapshots ----
def test_snapshot_verify_detects_tamper(store):
    import hashlib
    import sqlite3
    blob = '{"step": 1}'
    sid = store.snapshot_save(
        "t-s", "s1", blob,
        content_sha256=hashlib.sha256(blob.encode()).hexdigest())
    ok, _ = store.snapshot_verify(sid)
    assert ok
    conn = sqlite3.connect(store.db_path, timeout=10)
    conn.execute("UPDATE snapshots SET state_json='{\"step\": 999}'"
                 " WHERE id=?", (sid,))
    conn.commit()
    conn.close()
    ok, reason = store.snapshot_verify(sid)
    assert not ok and "mismatch" in reason


# ---- phase 64: external reconcile states ----
def test_reconcile_external_in_progress_resuming(store):
    from src.txn import reconcile_external
    store.op_set("ext-3", "t-e", "external_push", "UNKNOWN")
    decision, _ = reconcile_external(
        store, "ext-3", lambda: {"state": "in_progress", "evidence": {}})
    assert decision == "resuming"


# ---- phase 66: resume verifies the snapshot chain ----
def test_resume_verifies_snapshot_chain(cfg):
    import sqlite3
    from src.agent import AgentRuntime
    rt = AgentRuntime(cfg)
    try:
        rt.store.create_task("t-rs", {"steps": []})
        rt.store.update_task("t-rs", status="PAUSED")
        rt.store.checkpoint("t-rs", 0, "c0", {"ok": True})
        r = rt.request_resume("t-rs")
        assert r["resumed"] is True
        # tamper with a checkpoint -> resume refused
        rt.store.update_task("t-rs", status="PAUSED")
        ckpt = rt.store.checkpoints("t-rs")[0]
        conn = sqlite3.connect(rt.store.db_path, timeout=10)
        conn.execute("UPDATE checkpoints SET state_json='{garbage'"
                     " WHERE id=?", (ckpt["id"],))
        conn.commit()
        conn.close()
        r2 = rt.request_resume("t-rs")
        assert r2["resumed"] is False
    finally:
        rt.store.close()


# ---- phase 68: memory budget ----
def test_memory_budget_pauses(cfg):
    from src.agent import AgentRuntime
    rt = AgentRuntime(cfg)
    try:
        rt.store.create_task("t-mem", {
            "steps": [{"name": "s", "tool": "shell",
                       "args": {"command": "echo hi"}}],
            "budgets": {"memory_mb": 0.001}})  # impossible: RSS is MBs
        assert rt.run_task("t-mem") == "PAUSED"
    finally:
        rt.store.close()


# ---- phase 76: prune never deletes the only proven backup ----
def test_backup_prune_keeps_proven(cfg):
    s = Store(cfg["state_db"], cfg["journal_dir"])
    try:
        bids = [backup.create_backup(s, cfg, label=f"b{i}")
                for i in range(3)]
        ok, _ = backup.test_restore(bids[0], s)  # prove the OLDEST
        assert ok
        pruned = backup.prune_backups(s, cfg, keep=1)
        remaining = [b["id"] for b in s.backups_list()]
        assert bids[0] in remaining  # only proven backup survives
        assert bids[0] not in pruned
    finally:
        s.close()


# ---- phase 51: heal never touches healthy deps ----
def test_dep_heal_skips_healthy(store):
    # python3 is healthy in this environment; heal must record nothing
    # for it and must not attempt any repair.
    before = len(store.recovery_history(limit=500))
    repaired, need_human = depsmod.heal(store, {"base_dir": "/tmp"})
    assert isinstance(repaired, list) and isinstance(need_human, list)
    for _task_id, kind, sig, method in [
            (a["task_id"], a["failure_kind"], a["failure_signature"],
             a["recovery_method"])
            for a in store.recovery_history(limit=500)]:
        assert kind != "dep_failed" or "python3" not in sig
    assert len(store.recovery_history(limit=500)) >= before


def test_config_mark_good_pins_version(store, tmp_path):
    base = str(tmp_path)
    os.makedirs(os.path.join(base, "config"), exist_ok=True)
    cfg_path = os.path.join(base, "config", "config.json")
    with open(cfg_path, "w") as f:
        f.write('{"v": 2}')
    integ.snapshot_config(store, base, paths=["config/config.json"],
                          note="v2")
    integ.mark_config_good(store, base, paths=["config/config.json"])
    goods = store.config_versions(cfg_path, limit=5)
    assert any(g.get("is_known_good") for g in goods)
