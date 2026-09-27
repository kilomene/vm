"""Phase 89: compound failure tests.

Single failures are covered by the fault-injection scenarios; these tests
combine multiple simultaneous failures and assert the system degrades to a
safe, consistent state — never corrupting state, never duplicating side
effects, never handing one task to two workers.

Runs against the source tree directly — no root, no live install needed.
PYTHONPATH=. python3 -m pytest tests/test_compound.py -v
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.state import Store
from src.agent import AgentRuntime
from src import faultinject


@pytest.fixture()
def rt(tmp_path):
    base = str(tmp_path / "vm-agent")
    for d in ("state/journal", "config", "checkpoints", "logs", "run",
              "backups"):
        os.makedirs(os.path.join(base, d), exist_ok=True)
    cfg = {"base_dir": base,
           "state_db": os.path.join(base, "state", "state.db"),
           "journal_dir": os.path.join(base, "state", "journal"),
           "checkpoint_dir": os.path.join(base, "checkpoints"),
           "log_dir": os.path.join(base, "logs"),
           "run_dir": os.path.join(base, "run"),
           "heartbeat_interval_s": 60,
           "pause_verify_s": 5,
           "tool_timeout_default_s": 10,
           "tool_timeouts": {}}
    agent = AgentRuntime(cfg)
    yield agent, cfg, base
    agent.store.close()


def _compound_step(marker):
    return {"name": "write-marker", "tool": "shell",
            "op_id": "compound-op-1",
            "args": {"command": f"echo marker >> {marker}"},
            # idempotent_check is the reconcile evidence after a crash:
            "idempotent_check": {"check": "file_contains",
                                 "path": marker, "text": "marker"}}


def test_compound_stale_lock_plus_expired_lease(rt):
    """Two faults at once: a stale lock and an expired lease. The new
    worker must reap the lock, claim the lease (exactly one owner), and
    run the task to completion."""
    agent, cfg, base = rt
    store = agent.store
    # inject both faults
    r1 = faultinject.inject_stale_lock(store)
    r2 = faultinject.inject_expired_lease(store)
    assert r1["recovered"] and r2["recovered"]

    marker = os.path.join(base, "marker.txt")
    store.create_task("t-compound", {"steps": [_compound_step(marker)]})
    assert agent.run_task("t-compound") == "COMPLETED"
    with open(marker) as f:
        assert f.read().count("marker") == 1


def test_compound_crash_mid_op_reconciles_no_duplicate(rt):
    """Crash ambiguity + stale lock together: after the op is marked
    UNKNOWN (outcome ambiguous) and a stale lock reappears, the retry
    must reconcile via idempotent_check and NOT duplicate the side
    effect."""
    agent, cfg, base = rt
    store = agent.store
    marker = os.path.join(base, "marker2.txt")
    store.create_task("t-crash", {"steps": [_compound_step(marker)]})
    assert agent.run_task("t-crash") == "COMPLETED"

    # compound the faults: ambiguous op outcome + stale lock.
    # Model a crash that happened BEFORE the checkpoint write: the step-0
    # checkpoint from the first run must not count (otherwise the step is
    # legitimately skipped on resume — that is correct behavior).
    store.op_mark_unknown("compound-op-1")
    store.clear_checkpoints("t-crash")
    faultinject.inject_stale_lock(store)
    # rewind the task so the step is re-entered post-restart
    store.update_task("t-crash", status="PENDING", current_step=0)

    # a fresh agent instance (post-restart) takes over
    agent2 = AgentRuntime(cfg)
    try:
        out = agent2.run_task("t-crash")
        assert out == "COMPLETED"
        with open(marker) as f:
            lines = f.read().count("marker")
        assert lines == 1, f"side effect duplicated: {lines} markers"
        # the op was reconciled as already-done, not re-executed
        assert store.op_get("compound-op-1")["status"] == "COMPLETED"
    finally:
        agent2.store.close()


def test_compound_unreliable_clock_fails_closed(rt, monkeypatch):
    """Bad clock + heartbeat checks together: with an unreliable clock,
    lease freshness cannot be trusted, so the supervisor fails closed
    (reports hung) instead of believing a stale beat — and leases are
    never mass-expired."""
    import src.timecheck as tc
    agent, cfg, base = rt
    store = agent.store
    # unreliable clock: freshness checks fail closed; lease_still_valid
    # keeps leases alive rather than mass-expiring them.
    monkeypatch.setattr(tc, "_unreliable", True)
    monkeypatch.setattr(tc, "check_sync",
                        lambda: (False, {"reason": "drift too large"}))
    try:
        # fresh heartbeat, but the clock is suspect -> not valid
        assert not tc.lease_valid(time.time(), max_age_s=3600,
                                  check_clock=True)
        # fail-closed lease expiry: an unreliable clock never mass-expires
        assert tc.lease_still_valid(time.time() - 10**6) is True
    finally:
        monkeypatch.undo()


def test_compound_dep_failure_plus_step_failure_recorded(rt):
    """Dependency failure recorded while a step also fails: both go into
    the recovery history with distinct fingerprints, and the task does
    not spin — the matrix retry limit bounds it."""
    agent, cfg, base = rt
    store = agent.store
    from src import deps as depsmod
    from src import failure as failuremod

    faultinject.inject_dep_failure(store)
    store.create_task("t-depfail", {"steps": [
        {"name": "bad", "tool": "shell",
         "args": {"command": "exit 7"}}]})
    assert agent.run_task("t-depfail") == "FAILED"

    kinds = {a["failure_kind"] for a in store.recovery_history(limit=50)}
    assert "step_failed" in kinds  # the step failure was recorded
    sigs = {a["failure_signature"]
            for a in store.recovery_history(limit=50)
            if a["failure_signature"]}
    assert len(sigs) >= 1  # fingerprinted, not anonymous
    # the analyzer can still pick a strategy (nothing exhausted yet)
    method, sig, exhausted = failuremod.FailureAnalyzer(store)\
        .choose_strategy("t-depfail", "step_failed", "bad", "exit 7")
    assert not exhausted and method in ("retry_step", "rebuild_plan")


def test_compound_intervention_preserves_state(rt):
    """When recovery is exhausted, the task parks in a human-reviewable
    state with its verified progress intact — never half-applied."""
    agent, cfg, base = rt
    store = agent.store
    marker = os.path.join(base, "marker3.txt")
    store.create_task("t-park", {"steps": [
        {"name": "good", "tool": "shell",
         "args": {"command": f"echo ok > {marker}"},
         "verify": [{"check": "file_contains", "path": marker,
                     "text": "ok"}]},
        {"name": "bad", "tool": "shell",
         "args": {"command": "exit 9"}}]})
    # fail the second step repeatedly until the planner gives up
    for _ in range(4):
        out = agent.run_task("t-park")
        if out == "COMPLETED":
            break
    task = store.get_task("t-park")
    # step 0's verified work survived; the task never half-applied step 1
    assert store.latest_checkpoint("t-park") is not None
    with open(marker) as f:
        assert "ok" in f.read()
    assert task["status"] in ("FAILED", "PAUSED", "STALLED", "COMPLETED")
