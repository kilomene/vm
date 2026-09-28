"""End-to-end wiring regression tests for vm-agent fixes 1-6.

Each test drives the real AgentRuntime / Supervisor path (not a direct
call into a helper module). Written test-first: every test failed before
its fix and passes after.
"""
import json
import os
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.state import Store
from src import timecheck as timecheckmod


def make_home(tmp_path, name="vm-agent"):
    base = str(tmp_path / name)
    for d in ("state/journal", "config", "checkpoints", "logs", "run"):
        os.makedirs(os.path.join(base, d), exist_ok=True)
    return base


def make_cfg(base):
    return {"base_dir": base,
            "state_db": os.path.join(base, "state", "state.db"),
            "journal_dir": os.path.join(base, "state", "journal"),
            "checkpoint_dir": os.path.join(base, "checkpoints"),
            "log_dir": os.path.join(base, "logs"),
            "run_dir": os.path.join(base, "run"),
            "heartbeat_interval_s": 60,
            "heartbeat_timeout_s": 30,
            "no_progress_timeout_s": 60,
            "pause_verify_s": 5,
            "tool_timeout_default_s": 10,
            "tool_timeouts": {}}


def make_agent_cfg(base):
    cfg = make_cfg(base)
    cfg.update({"heartbeat_interval_s": 60,
                "pause_verify_s": 5,
                "tool_timeout_default_s": 10,
                "tool_timeouts": {}})
    return cfg


# ---------------------------------------------------------------- fix 1
def _make_supervisor(tmp_path, monkeypatch):
    from src.supervisor import Supervisor
    base = make_home(tmp_path)
    cfg = make_cfg(base)
    # hermetic clock: heartbeat freshness is not what this test probes
    monkeypatch.setattr(timecheckmod, "check_sync", lambda: (True, {}))
    sup = Supervisor(cfg)
    sup._start_ts = time.time()
    proc = subprocess.Popen(["sleep", "120"])
    sup.agent_proc = proc
    return sup, proc


def test_fix1_hang_detection_fires_on_wedged_agent(tmp_path, monkeypatch):
    """A wedged agent (live PID, stale heartbeat, step never changes) must
    be reported 'hung' once idle exceeds no_progress_timeout_s."""
    sup, proc = _make_supervisor(tmp_path, monkeypatch)
    try:
        sup.store.heartbeat("agent", pid=proc.pid, task_id="t-hang",
                            step=7, operation="wedged")
        base = time.time()
        now = [base]
        monkeypatch.setattr(time, "time", lambda: now[0])
        states = []
        for i in range(1, 26):          # 5s ticks, 125s simulated
            now[0] = base + i * 5
            states.append(sup.agent_health()[0])
        assert "hung" in states, f"never hung: {states}"
        first_hung = states.index("hung")
        # idle starts accumulating at the first stale tick (~t=30-35s);
        # 60s of no progress => hung no earlier than ~t=90s (tick 18),
        # and everything before the first hung verdict is "ok".
        assert first_hung >= 18, f"hung too early: {states}"
        assert all(s == "ok" for s in states[:first_hung])
    finally:
        proc.terminate()
        proc.wait()


def test_fix1_step_progress_resets_idle_baseline(tmp_path, monkeypatch):
    """If the step advances between ticks, the agent is progressing and
    must stay 'ok' even past no_progress_timeout_s of wall time."""
    sup, proc = _make_supervisor(tmp_path, monkeypatch)
    try:
        sup.store.heartbeat("agent", pid=proc.pid, task_id="t-hang",
                            step=1, operation="working")
        base = time.time()
        now = [base]
        monkeypatch.setattr(time, "time", lambda: now[0])
        states = []
        for i in range(1, 26):
            now[0] = base + i * 5
            if i % 3 == 0:
                # step advances in the real world; only the heartbeat is stale
                sup.store._conn.execute(
                    "UPDATE heartbeats SET step=? WHERE component='agent'",
                    (i,))
                sup.store._conn.commit()
            states.append(sup.agent_health()[0])
        assert "hung" not in states, f"falsely hung: {states}"
    finally:
        proc.terminate()
        proc.wait()


def test_fix1_fresh_heartbeat_clears_idle_baseline(tmp_path, monkeypatch):
    """When the heartbeat becomes fresh again, the accumulated idle
    baseline is dropped."""
    sup, proc = _make_supervisor(tmp_path, monkeypatch)
    try:
        sup.store.heartbeat("agent", pid=proc.pid, task_id="t-hang",
                            step=7, operation="wedged")
        base = time.time()
        now = [base]
        monkeypatch.setattr(time, "time", lambda: now[0])
        # 10 stale ticks (50s): baseline recorded, still ok
        for i in range(1, 11):
            now[0] = base + i * 5
            assert sup.agent_health()[0] == "ok"
        assert sup._last_progress, "expected an idle baseline to be tracked"
        # heartbeat goes fresh again
        now[0] = base + 55
        sup.store.heartbeat("agent", pid=proc.pid, task_id="t-hang",
                            step=7, operation="recovered")
        state, _ = sup.agent_health()
        assert state == "ok"
        assert not sup._last_progress, "stale baseline was not cleared"
    finally:
        proc.terminate()
        proc.wait()


# ---------------------------------------------------------------- fix 2
def _crash_window(store, task_id, op_id, counter):
    """Simulate the crash window: op left RUNNING, its side effect already
    applied to the world, task left RUNNING, no checkpoint written."""
    store.op_set(op_id, task_id, "shell", "RUNNING")
    store.update_task(task_id, status="RUNNING")
    with open(counter, "a") as f:
        f.write("line\n")


def test_fix2_ambiguous_op_pauses_instead_of_rerunning(tmp_path):
    """Crash after the tool ran but before op_set(COMPLETED), with no
    idempotent_check: resume must NOT re-execute the step. The op is
    ambiguous -> task pauses/fails and one intervention opens."""
    from src.agent import AgentRuntime
    base = make_home(tmp_path)
    cfg = make_agent_cfg(base)
    store = Store(cfg["state_db"], cfg["journal_dir"])
    counter = str(tmp_path / "counter.txt")
    op_id = "op-crash-ambiguous"
    spec = {"steps": [{"name": "append", "tool": "shell",
                       "args": {"command": f"echo line >> '{counter}'"},
                       "op_id": op_id}]}
    store.create_task("t-amb", spec)
    _crash_window(store, "t-amb", op_id, counter)
    store.close()

    rt = AgentRuntime(cfg)
    try:
        rt.run_task("t-amb")
        with open(counter) as f:
            lines = f.read().splitlines()
        assert lines == ["line"], f"duplicate side effect executed: {lines}"
        task = rt.store.get_task("t-amb")
        assert task["status"] in ("PAUSED", "FAILED"), task["status"]
        ivs = rt.store.interventions_open()
        assert len(ivs) == 1, ivs
        assert "ambiguous" in ivs[0]["reason"].lower()
        assert op_id in ivs[0]["reason"]
    finally:
        rt.store.close()


def test_fix2_reconciled_done_op_skips_without_rerun(tmp_path):
    """Mirror case: with an idempotent_check that passes, the crashed op
    is verified done -> skipped, task completes, no duplicate effect."""
    from src.agent import AgentRuntime
    base = make_home(tmp_path)
    cfg = make_agent_cfg(base)
    store = Store(cfg["state_db"], cfg["journal_dir"])
    counter = str(tmp_path / "counter2.txt")
    op_id = "op-crash-reconciled"
    spec = {"steps": [{"name": "append", "tool": "shell",
                       "args": {"command": f"echo line >> '{counter}'"},
                       "op_id": op_id,
                       "idempotent_check": {"check": "file_contains",
                                            "path": counter,
                                            "text": "line"}}]}
    store.create_task("t-rec", spec)
    _crash_window(store, "t-rec", op_id, counter)
    store.close()

    rt = AgentRuntime(cfg)
    try:
        result = rt.run_task("t-rec")
        with open(counter) as f:
            lines = f.read().splitlines()
        assert lines == ["line"], f"step re-executed: {lines}"
        assert result == "COMPLETED", result
        assert rt.store.interventions_open() == []
        op = rt.store.op_get(op_id)
        assert op["status"] == "COMPLETED"
    finally:
        rt.store.close()


# ---------------------------------------------------------------- fix 3
def _make_supervisor_noproc(tmp_path, monkeypatch):
    """Supervisor without a child process: enough to drive _clock_check."""
    from src.supervisor import Supervisor
    base = make_home(tmp_path)
    sup = Supervisor(make_cfg(base))
    sup._start_ts = time.time()
    return sup


def _scripted_clock(monkeypatch, results):
    it = iter(results)

    def _fake():
        try:
            return next(it)
        except StopIteration:
            return (True, {})
    monkeypatch.setattr(timecheckmod, "check_sync", _fake)


def test_fix3_single_clock_blip_does_not_enter_safe_mode(tmp_path,
                                                         monkeypatch):
    """One failed check_sync (e.g. VM suspend/resume) followed by healthy
    checks must NOT latch the system in safe mode."""
    sup = _make_supervisor_noproc(tmp_path, monkeypatch)
    _scripted_clock(monkeypatch, [(False, {"reason": "clock jumped ~300s"}),
                                  (True, {}), (True, {}), (True, {})])
    for _ in range(4):
        ok, _ = sup._clock_check()
    assert not sup.recovery.in_safe_mode()
    sup.store.close()


def test_fix3_consecutive_clock_failures_enter_and_auto_clear(tmp_path,
                                                              monkeypatch):
    """3 consecutive failures -> safe mode tagged reason_kind='clock';
    3 consecutive good checks -> auto-cleared."""
    sup = _make_supervisor_noproc(tmp_path, monkeypatch)
    _scripted_clock(monkeypatch, [(False, {"reason": "jump"})] * 3)
    for _ in range(3):
        sup._clock_check()
    assert sup.recovery.in_safe_mode()
    sm = sup.store.kv_get("safe_mode")
    assert sm["reason_kind"] == "clock", sm
    _scripted_clock(monkeypatch, [(True, {})] * 3)
    for _ in range(3):
        sup._clock_check()
    assert not sup.recovery.in_safe_mode()
    sup.store.close()


def test_fix3_escalation_safe_mode_never_auto_clears(tmp_path, monkeypatch):
    """Escalation-caused safe mode (no reason_kind='clock') must survive
    any number of good clock checks: only an operator clears it."""
    sup = _make_supervisor_noproc(tmp_path, monkeypatch)
    sup.recovery.enter_safe_mode("t1", "escalated to L7: step_failed")
    assert sup.recovery.in_safe_mode()
    _scripted_clock(monkeypatch, [(True, {})] * 6)
    for _ in range(6):
        sup._clock_check()
    assert sup.recovery.in_safe_mode()
    sup.store.close()


def _load_cli():
    """Load src/cli.py as vmagent.cli (the installed layout names the
    package vmagent; the repo names it src)."""
    import importlib
    import importlib.util
    import types
    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if "vmagent" not in sys.modules:
        pkg = types.ModuleType("vmagent")
        pkg.__path__ = [os.path.join(repo, "src")]
        sys.modules["vmagent"] = pkg
    for sub in ("config", "state"):
        mod = importlib.import_module(f"src.{sub}")
        sys.modules.setdefault(f"vmagent.{sub}", mod)
    if "vmagent.cli" in sys.modules:
        return sys.modules["vmagent.cli"]
    spec = importlib.util.spec_from_file_location(
        "vmagent.cli", os.path.join(repo, "src", "cli.py"))
    cli = importlib.util.module_from_spec(spec)
    sys.modules["vmagent.cli"] = cli
    spec.loader.exec_module(cli)
    return cli


def test_fix3_cli_safe_mode_exit_clears_escalation(tmp_path, monkeypatch,
                                                   capsys):
    """vm-agent safe-mode exit clears an escalation-caused safe mode;
    safe-mode status reports the record."""
    import argparse
    cli = _load_cli()
    base = make_home(tmp_path)
    cfg = make_cfg(base)
    monkeypatch.setattr(cli, "_cfg", lambda: cfg)
    from src.recovery import RecoveryManager
    store = Store(cfg["state_db"], cfg["journal_dir"])
    rec = RecoveryManager(store, cfg)
    rec.enter_safe_mode("t9", "escalated to L8: db_corrupt")
    assert rec.in_safe_mode()
    cli.cmd_safe_mode(argparse.Namespace(safe_mode_cmd="exit", note="op-ok"))
    assert not rec.in_safe_mode()
    out = capsys.readouterr().out
    assert "cleared" in out.lower()
    cli.cmd_safe_mode(argparse.Namespace(safe_mode_cmd="status"))
    out = capsys.readouterr().out
    assert "active" in out.lower()
    store.close()


# ---------------------------------------------------------------- fix 4
def _run_task_with_spec(tmp_path, task_id, steps):
    """Build a fresh home + AgentRuntime, run one task, return (rt, result)."""
    from src.agent import AgentRuntime
    base = make_home(tmp_path)
    cfg = make_agent_cfg(base)
    store = Store(cfg["state_db"], cfg["journal_dir"])
    store.create_task(task_id, {"steps": steps})
    store.close()
    rt = AgentRuntime(cfg)
    result = rt.run_task(task_id)
    return rt, result


def test_fix4_nonexistent_command_runs_exactly_once_and_fails_fast(tmp_path):
    """'nosuchcmd_xyz' -> 'command not found' is PERMANENT: exactly one
    attempt, no 35s of doomed retries."""
    counter = str(tmp_path / "attempts.txt")
    steps = [{"name": "bad-cmd", "tool": "shell",
              "args": {"command":
                       f"echo attempt >> '{counter}'; nosuchcmd_xyz"}}]
    t0 = time.time()
    rt, result = _run_task_with_spec(tmp_path, "t-badcmd", steps)
    elapsed = time.time() - t0
    try:
        assert result == "FAILED", result
        with open(counter) as f:
            attempts = f.read().splitlines()
        assert attempts == ["attempt"], f"retried: {attempts}"
        assert elapsed < 5, f"took {elapsed:.1f}s (doomed retries?)"
    finally:
        rt.store.close()


def test_fix4_missing_file_tool_error_runs_once(tmp_path):
    """'No such file or directory' on a tool path is PERMANENT too."""
    counter = str(tmp_path / "attempts2.txt")
    missing = str(tmp_path / "nope.txt")
    steps = [{"name": "missing", "tool": "shell",
              "args": {"command":
                       f"echo attempt >> '{counter}'; cat '{missing}'"}}]
    rt, result = _run_task_with_spec(tmp_path, "t-missing", steps)
    try:
        assert result == "FAILED", result
        with open(counter) as f:
            assert f.read().splitlines() == ["attempt"]
    finally:
        rt.store.close()


def test_fix4_transient_timeout_still_retries_with_backoff(tmp_path,
                                                           monkeypatch):
    """A genuine transient error ('Connection timed out') still retries
    with the classified backoff schedule."""
    from src import agent as agentmod
    counter = str(tmp_path / "attempts3.txt")
    sleeps = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    steps = [{"name": "flaky", "tool": "shell",
              "args": {"command": f"echo attempt >> '{counter}'; "
                                  "echo 'Connection timed out' >&2; "
                                  "exit 1"}}]
    rt, result = _run_task_with_spec(tmp_path, "t-flaky", steps)
    try:
        assert result == "FAILED", result
        with open(counter) as f:
            attempts = f.read().splitlines()
        # TRANSIENT connection_timeout: 5 retries => 6 attempts.
        # (Filter out subprocess's internal polling sleeps < 1s; only the
        # agent's retry backoffs matter here.)
        assert len(attempts) == 6, attempts
        retry_sleeps = [s for s in sleeps if s >= 1]
        assert retry_sleeps == [2, 4, 8, 16, 30], retry_sleeps
    finally:
        rt.store.close()


def test_fix4_classifier_branches_reachable():
    """The malformed-model / browser / tool branches after the net guard
    are reachable again."""
    from src import classify as classifymod
    d, n, _, _ = classifymod.classify("model", "malformed_response")
    assert d == classifymod.RETRYABLE and n == 2
    d, n, _, _ = classifymod.classify_tool_error("command not found", 127)
    assert (d, n) == (classifymod.PERMANENT, 0)
    d, n, _, _ = classifymod.classify_tool_error(
        "sh: 1: nosuchcmd_xyz: not found", 127)
    assert (d, n) == (classifymod.PERMANENT, 0)
    d, n, _, _ = classifymod.classify_tool_error("Permission denied", 1)
    assert (d, n) == (classifymod.PERMANENT, 0)
    d, n, _, _ = classifymod.classify_tool_error(
        "cat: /x: No such file or directory", 1)
    assert (d, n) == (classifymod.PERMANENT, 0)


# ---------------------------------------------------------------- fix 5
def test_fix5_per_step_retries_zero_runs_once(tmp_path):
    """A RETRYABLE failure (exit 3) with step retries: 0 is attempted
    exactly once — the step cap wins over the classification."""
    counter = str(tmp_path / "attempts5.txt")
    steps = [{"name": "no-retry", "tool": "shell", "retries": 0,
              "args": {"command": f"echo attempt >> '{counter}'; exit 3"}}]
    rt, result = _run_task_with_spec(tmp_path, "t-noretry", steps)
    try:
        assert result == "FAILED", result
        with open(counter) as f:
            assert f.read().splitlines() == ["attempt"]
    finally:
        rt.store.close()


def test_fix5_same_step_twice_in_a_row_opens_intervention(tmp_path):
    """The same step failing twice in a row opens a human intervention
    (retry_exhausted -> human required) instead of a third blind retry
    loop."""
    from src.agent import AgentRuntime
    base = make_home(tmp_path)
    cfg = make_agent_cfg(base)
    store = Store(cfg["state_db"], cfg["journal_dir"])
    steps = [{"name": "flaky-step", "tool": "shell", "retries": 0,
              "args": {"command": "exit 3"}}]
    store.create_task("t-repeat", {"steps": steps})
    store.close()
    rt = AgentRuntime(cfg)
    try:
        assert rt.run_task("t-repeat") == "FAILED"
        assert rt.store.interventions_open() == []
        rt.store.update_task("t-repeat", status="PENDING", current_step=0)
        assert rt.run_task("t-repeat") == "FAILED"
        ivs = rt.store.interventions_open()
        assert len(ivs) == 1, ivs
        assert "flaky-step" in ivs[0]["reason"], ivs[0]["reason"]
        assert "retry_exhausted" in ivs[0]["reason"], ivs[0]["reason"]
    finally:
        rt.store.close()


def test_fix5_retry_exhausted_escalation_policy():
    """The escalation table maps retry_exhausted -> HUMAN_REQUIRED."""
    from src import classify as classifymod
    assert classifymod.escalate_on_repeat("retry_exhausted") == \
        classifymod.HUMAN_REQUIRED


# ---------------------------------------------------------------- fix 6
def test_fix6_vault_secret_redacted_from_stored_output(tmp_path):
    """A secret stored via vault_set must be redacted from the runtime's
    stored tool output: the STEP_FAILED journal error, the failure
    fingerprint rows, and last_verified_result. (The step spec itself is
    user-authored input, not tool output, and is out of scope here.)"""
    import glob
    import json as jsonlib
    from src.agent import AgentRuntime
    from src import secrets as secretsmod
    base = make_home(tmp_path)
    cfg = make_agent_cfg(base)
    store = Store(cfg["state_db"], cfg["journal_dir"])
    secret = "fix6-topsecret-" + "x" * 12
    secretsmod.vault_set(store, "tok", secret, owner="*")
    try:
        steps = [{"name": "leaky", "tool": "shell",
                  "args": {"command":
                           f"echo out-{secret}; echo err-{secret} >&2; "
                           f"exit 1"}}]
        store.create_task("t-leak", {"steps": steps})
        store.close()
        rt = AgentRuntime(cfg)
        try:
            assert rt.run_task("t-leak") == "FAILED"
            # failure fingerprints must not hold the raw secret
            fps = rt.store._conn.execute(
                "SELECT * FROM failure_fingerprints").fetchall()
            assert secret not in jsonlib.dumps([dict(r) for r in fps]), \
                "raw secret in failure_fingerprints"
            # last_verified_result must not hold the raw secret
            task = rt.store.get_task("t-leak")
            assert secret not in (task["last_verified_result"] or ""), \
                "raw secret in last_verified_result"
            # STEP_FAILED journal error field: redacted, never raw
            step_failed = []
            for path in glob.glob(os.path.join(cfg["journal_dir"],
                                               "*.jsonl")):
                with open(path) as f:
                    for line in f:
                        try:
                            evt = jsonlib.loads(line)
                        except ValueError:
                            continue
                        if evt.get("event") == "STEP_FAILED" and \
                                evt.get("task_id") == "t-leak":
                            step_failed.append(evt)
            assert step_failed, "no STEP_FAILED journaled"
            for evt in step_failed:
                assert secret not in jsonlib.dumps(evt), \
                    "raw secret in STEP_FAILED journal"
                assert "REDACTED" in jsonlib.dumps(evt), \
                    "expected redaction marker in STEP_FAILED journal"
        finally:
            rt.store.close()
    finally:
        # module vault is process-global: don't leak the fake secret
        secretsmod._VAULT.pop("tok", None)


# ---------------------------------------------------------------- fix 7
def _make_executor(default=5):
    from src.tools import Executor
    return Executor({"tool_timeout_default_s": default, "tool_timeouts": {}})


def test_fix7_zero_timeout_uses_default_not_instant_kill():
    """timeout_s: 0 must not kill the command instantly: it is clamped to
    the default timeout, so a quick command still succeeds."""
    ex = _make_executor(default=5)
    res = ex.run("shell", args={"command": "echo hello", "timeout_s": 0})
    assert res.ok, f"stderr={res.stderr!r} timed_out={res.timed_out}"
    assert "hello" in res.stdout


def test_fix7_zero_timeout_does_not_hang_on_sleep():
    """`sleep 30` with timeout_s: 0 returns via the default timeout
    instead of hanging 30s (and instead of an instant kill, which would
    prove 0 was passed straight through)."""
    ex = _make_executor(default=5)
    t0 = time.time()
    res = ex.run("shell", args={"command": "sleep 30", "timeout_s": 0})
    elapsed = time.time() - t0
    assert res.timed_out, "sleep 30 should hit the default timeout"
    assert elapsed < 30, f"hung for {elapsed:.1f}s"
    assert elapsed >= 2, (f"returned in {elapsed:.2f}s: timeout 0 was "
                          "passed through (instant kill), not clamped")


def test_fix7_timeout_for_clamps_nonpositive_config():
    """A configured tool_timeouts value of 0/None falls back to default."""
    from src.tools import Executor
    ex = Executor({"tool_timeout_default_s": 7,
                   "tool_timeouts": {"shell": 0, "http_get": None}})
    assert ex._timeout_for("shell") == 7
    assert ex._timeout_for("http_get") == 7
    assert ex._timeout_for("other") == 7


# ---------------------------------------------------------------- fix 8
def test_fix8_failure_history_persists_and_escalates(tmp_path):
    """Regression guard for the reported failure-history bug.

    Verified against the tree: failure.record() persists to
    failure_fingerprints with only real columns (no OperationalError),
    occurrences accumulate per signature, and choose_strategy escalates
    to ("human", sig, True) once every strategy for the kind has failed.
    """
    from src import failure as failuremod
    base = make_home(tmp_path)
    cfg = make_agent_cfg(base)
    store = Store(cfg["state_db"], cfg["journal_dir"])
    try:
        fa = failuremod.FailureAnalyzer(store)
        sig1 = failuremod.record(store, "t8", "step_failed", "op-x",
                                 "retry_step", result="failed")
        sig2 = failuremod.record(store, "t8", "step_failed", "op-x",
                                 "retry_step", result="failed")
        assert sig1 == sig2, "same kind/operation must share a signature"
        fp = store.fingerprint_get(sig1)
        assert fp is not None and fp["occurrences"] == 2, fp
        # burn through the remaining strategies -> escalate to human
        for method in ("rebuild_plan", "pause_task"):
            m, sig, esc = fa.choose_strategy("t8", "step_failed", "op-x",
                                             "")
            assert (m, esc) == (method, False), (m, esc)
            failuremod.record(store, "t8", "step_failed", "op-x", m,
                              result="failed")
        m, sig, esc = fa.choose_strategy("t8", "step_failed", "op-x", "")
        assert (m, esc) == ("human", True), (m, esc)
        assert store.fingerprint_get(sig)["escalated"] == 1
    finally:
        store.close()


# ---------------------------------------------------------------- fix 9
def test_fix9_budget_exact_fit_allowed_over_limit_denied(tmp_path):
    """Regression guard for the reported budget off-by-one.

    Verified against the tree: there is no budget.py and no token-budget
    check(); the budget API is Store.budget_consume, whose boundary is
    already correct — consuming exactly up to the limit is allowed, only
    going OVER the limit is denied.
    """
    base = make_home(tmp_path)
    cfg = make_agent_cfg(base)
    store = Store(cfg["state_db"], cfg["journal_dir"])
    try:
        store.budget_set("t9", "tool_calls", 2)
        assert store.budget_consume("t9", "tool_calls", 1) == (True, 1, 2)
        # exact fit: allowed
        assert store.budget_consume("t9", "tool_calls", 1) == (True, 2, 2)
        # over the limit: denied
        ok, used, limit = store.budget_consume("t9", "tool_calls", 1)
        assert (ok, used, limit) == (False, 3, 2)
    finally:
        store.close()


# ---------------------------------------------------------------- fix 10
def test_fix10_stop_agent_sigterm_ignored_still_kills(tmp_path):
    """Regression guard: stop_agent must survive a SIGTERM-ignoring child.

    Verified against the tree: the first p.wait(timeout) is already inside
    try/except TimeoutExpired, so it falls through to SIGKILL instead of
    propagating and crashing the supervisor.
    """
    import subprocess as sp
    from src.supervisor import Supervisor
    base = make_home(tmp_path)
    sup = Supervisor(make_cfg(base))
    # ready-file handshake: the child installs its SIGTERM-ignoring
    # handler BEFORE we terminate, so the timeout path is really hit
    # (no race where SIGTERM lands before the handler exists).
    ready = os.path.join(str(tmp_path), "child-ready")
    if os.path.exists(ready):
        os.unlink(ready)
    p = sp.Popen([sys.executable, "-c",
                  "import signal, time; "
                  "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                  f"open({ready!r}, 'w').write('ready'); "
                  "time.sleep(30)"])
    try:
        for _ in range(200):
            if os.path.exists(ready):
                break
            time.sleep(0.05)
        assert os.path.exists(ready), "child never installed SIGTERM handler"
        assert p.poll() is None
        sup.agent_proc = p
        t0 = time.time()
        sup.stop_agent(timeout=1)  # must not raise TimeoutExpired
        elapsed = time.time() - t0
        assert elapsed >= 0.9, \
            f"returned in {elapsed:.2f}s: SIGTERM was not ignored?"
        assert p.poll() is not None, "child should be dead via SIGKILL"
        assert sup.agent_proc is None
        assert elapsed < 10, f"stop_agent took {elapsed:.1f}s"
    finally:
        if p.poll() is None:
            p.kill()
        sup.store.close()

# ---------------------------------------------------------------- fix 12
def test_fix12_task_retry_from_step_maps_1based_to_current_step(tmp_path,
                                                                monkeypatch,
                                                                capsys):
    """task-retry --from-step 2 rewinds a FAILED 3-step task to
    current_step=1 (user-facing 1-based step number -> count of completed
    steps), status PENDING."""
    import argparse
    cli = _load_cli()
    base = make_home(tmp_path)
    cfg = make_cfg(base)
    monkeypatch.setattr(cli, "_cfg", lambda: cfg)
    store = Store(cfg["state_db"], cfg["journal_dir"])
    steps = [{"name": f"s{i}", "tool": "shell", "args": {"command": "true"}}
             for i in range(3)]
    store.create_task("t-retry", {"steps": steps})
    store.update_task("t-retry", status="FAILED", current_step=3)
    store.close()
    cli.cmd_task_retry(argparse.Namespace(task_id="t-retry", from_step=2))
    out = capsys.readouterr().out
    assert "t-retry" in out
    s2 = Store(cfg["state_db"], cfg["journal_dir"])
    try:
        task = s2.get_task("t-retry")
        assert task["current_step"] == 1, dict(task)
        assert task["status"] == "PENDING", task["status"]
    finally:
        s2.close()


def test_fix12_task_retry_from_step_clamps_at_zero(tmp_path, monkeypatch,
                                                   capsys):
    """--from-step 0 (or negative) clamps current_step to 0, never -1."""
    import argparse
    cli = _load_cli()
    base = make_home(tmp_path)
    cfg = make_cfg(base)
    monkeypatch.setattr(cli, "_cfg", lambda: cfg)
    store = Store(cfg["state_db"], cfg["journal_dir"])
    store.create_task("t-retry0", {"steps": [{"name": "s0", "tool": "shell",
                                              "args": {"command": "true"}}]})
    store.update_task("t-retry0", status="FAILED", current_step=1)
    store.close()
    cli.cmd_task_retry(argparse.Namespace(task_id="t-retry0", from_step=0))
    capsys.readouterr()
    s2 = Store(cfg["state_db"], cfg["journal_dir"])
    try:
        task = s2.get_task("t-retry0")
        assert task["current_step"] == 0, dict(task)
        assert task["status"] == "PENDING", task["status"]
    finally:
        s2.close()

# ---------------------------------------------------------------- fix 13
def test_fix13_checkpoint_env_compatible_fails_on_mismatch(tmp_path):
    """Regression guard: checkpoint_env_compatible must return
    (False, issues) when the checkpoint's recorded env differs from the
    current runtime env — a broken environment must not sail through.

    Verified against the tree: no check_env_compatible with a
    result['compatible'] dict exists; the real functions already gate on
    len(issues) == 0.
    """
    import json as jsonlib
    from src.update import checkpoint_env_compatible, runtime_env
    base = make_home(tmp_path)
    cfg = make_agent_cfg(base)
    store = Store(cfg["state_db"], cfg["journal_dir"])
    try:
        store.create_task("t-env", {"steps": []})
        env = runtime_env()
        store.checkpoint("t-env", 0, "ck1", {"runtime_env": env})
        ok, issues = checkpoint_env_compatible(store, "t-env")
        assert ok is True and issues == []
        bad = dict(env, vm_agent_version="0.0.0-bogus",
                   python="2.7.0-bogus")
        store.checkpoint("t-env", 0, "ck1",
                         {"runtime_env": bad})
        ok, issues = checkpoint_env_compatible(store, "t-env")
        assert ok is False, "mismatched env must not be compatible"
        assert issues, "issues must describe the mismatch"
        blob = " ".join(issues)
        assert "vm_agent_version" in blob and "python" in blob
    finally:
        store.close()


def test_fix13_check_compatibility_fails_on_unmet_requirement():
    """Regression guard: check_compatibility returns (False, issues)
    when the environment does not meet a requirement."""
    from src.update import check_compatibility
    ok, issues = check_compatibility({"python": "3.8"},
                                     {"python": ["3.9", "3.10"]})
    assert ok is False
    assert issues and "python" in issues[0]
    ok, issues = check_compatibility({"python": "3.10"},
                                     {"python": ["3.9", "3.10"]})
    assert ok is True and issues == []


# ---------------------------------------------------------------- fix 14
def test_fix14_time_sleep_is_real():
    """Regression guard: time.sleep must really sleep at test scope.

    Verified against the tree: there is no tests/conftest.py and no
    autouse fixture patching time.sleep globally (the only sleep
    patching is a per-test monkeypatch in this file), so
    timing-sensitive tests run against real sleeps.
    """
    t0 = time.time()
    time.sleep(0.2)
    elapsed = time.time() - t0
    assert elapsed >= 0.15, f"time.sleep looks patched out ({elapsed:.3f}s)"


# ---------------------------------------------------------------- fix 6b (value-based redaction)
def test_fix6b_vault_ref_values_redacted_from_journal_and_db(tmp_path):
    """{"vault": ...} refs resolve at execution time only; the raw secret
    VALUES must not appear in the journal or in any SQLite row. The file
    written via write_file still holds the real value on disk."""
    import glob
    import sqlite3
    import json as jsonlib
    from src.agent import AgentRuntime
    from src import secrets as secretsmod
    base = make_home(tmp_path)
    cfg = make_agent_cfg(base)
    store = Store(cfg["state_db"], cfg["journal_dir"])
    wsecret = "fix6b-write-" + "z" * 16
    shcmd = "echo " + "fix6b-shell-" + "z" * 16
    ssecret = shcmd.split(" ", 1)[1]
    secretsmod.vault_set(store, "db_pass", wsecret, owner="*")
    secretsmod.vault_set(store, "sh_cmd", shcmd, owner="*")
    target = str(tmp_path / "secret.txt")
    secrets_to_check = (wsecret, shcmd, ssecret)
    try:
        steps = [
            {"name": "write-secret", "tool": "write_file",
             "args": {"path": target, "content": {"vault": "db_pass"}}},
            {"name": "run-secret-cmd", "tool": "shell",
             "args": {"command": {"vault": "sh_cmd"}}},
        ]
        store.create_task("t-vault", {"steps": steps})
        store.close()
        rt = AgentRuntime(cfg)
        try:
            result = rt.run_task("t-vault")
            assert result == "COMPLETED", result
            # the file on disk keeps the REAL value
            with open(target) as f:
                assert f.read() == wsecret
            # journal: zero hits for any secret value
            jfiles = glob.glob(os.path.join(cfg["journal_dir"], "*.jsonl"))
            assert jfiles, "no journal files written"
            for path in jfiles:
                with open(path) as f:
                    body = f.read()
                for s in secrets_to_check:
                    assert s not in body, \
                        f"secret value leaked in journal {path}"
            # every SQLite table/column: zero hits for any secret value
            conn = sqlite3.connect(cfg["state_db"])
            try:
                tables = [r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")]
                assert tables, "no tables in state db"
                for tbl in tables:
                    for row in conn.execute(f'SELECT * FROM "{tbl}"'):
                        blob = jsonlib.dumps([str(c) for c in row],
                                             default=str)
                        for s in secrets_to_check:
                            assert s not in blob, \
                                f"secret value leaked in table {tbl}"
            finally:
                conn.close()
        finally:
            rt.store.close()
    finally:
        # the module vault is process-global: don't leak the fake secrets
        secretsmod._VAULT.pop("db_pass", None)
        secretsmod._VAULT.pop("sh_cmd", None)


# ---------------------------------------------------------------- fix 7 (integrity baselines)
def _write_config(base, content):
    cfgpath = os.path.join(base, "config", "config.json")
    os.makedirs(os.path.dirname(cfgpath), exist_ok=True)
    with open(cfgpath, "w") as f:
        f.write(content)
    return cfgpath


def test_fix7b_reconcile_records_baseline_when_missing(tmp_path):
    """First supervisor start with no baseline: reconcile_on_boot must
    record one (not report a vacuous 'integrity check clean'), and the
    clean message must say how many files were verified."""
    import glob
    import json as jsonlib
    from src.recovery import RecoveryManager
    base = make_home(tmp_path)
    cfg = make_agent_cfg(base)
    _write_config(base, "{}")
    store = Store(cfg["state_db"], cfg["journal_dir"])
    rec = RecoveryManager(store, cfg, journal=store.journal)
    try:
        report = rec.reconcile_on_boot({"base_dir": base})
        assert any("verified 1 file" in f for f in report["fixed"]), \
            f"no verified-count clean message: {report}"
        assert not any(f == "integrity check clean"
                       for f in report["fixed"]), report["fixed"]
        hits = []
        for path in glob.glob(os.path.join(cfg["journal_dir"], "*.jsonl")):
            with open(path) as f:
                for line in f:
                    try:
                        evt = jsonlib.loads(line)
                    except ValueError:
                        continue
                    if evt.get("event") == "INTEGRITY_BASELINE_RECORDED":
                        hits.append(evt)
        assert hits, "baseline recording was not journaled"
    finally:
        store.close()


def test_fix7b_tampered_config_detected_after_install_baseline(tmp_path):
    """Install-time baseline (what install.sh now records), then tamper
    with config/config.json: reconcile_on_boot must report and journal
    an integrity mismatch — never 'clean'. The live state.db is not
    tracked, and the systemd unit path is not tracked either."""
    import glob
    import json as jsonlib
    from src.recovery import RecoveryManager
    from src import integrity as integmod
    assert "state/state.db" not in integmod.TRACKED, integmod.TRACKED
    assert "systemd/vm-agent.service" not in integmod.TRACKED, \
        integmod.TRACKED
    base = make_home(tmp_path)
    cfg = make_agent_cfg(base)
    _write_config(base, "{}")
    store = Store(cfg["state_db"], cfg["journal_dir"])
    # the install-time step: record a baseline for TRACKED files
    recorded = integmod.record(store, base)
    assert recorded, "baseline recorded nothing"
    store.close()
    # tamper with the config after install
    _write_config(base, '{"tampered": true}')
    store2 = Store(cfg["state_db"], cfg["journal_dir"])
    rec = RecoveryManager(store2, cfg, journal=store2.journal)
    try:
        report = rec.reconcile_on_boot({"base_dir": base})
        assert any("integrity mismatches" in w
                   for w in report["warnings"]), report
        assert not any("clean" in f for f in report["fixed"]), \
            report["fixed"]
        hits = []
        for path in glob.glob(os.path.join(cfg["journal_dir"], "*.jsonl")):
            with open(path) as f:
                for line in f:
                    try:
                        evt = jsonlib.loads(line)
                    except ValueError:
                        continue
                    if evt.get("event") == "INTEGRITY_MISMATCH":
                        hits.append(evt)
        assert hits, "INTEGRITY_MISMATCH not journaled"
    finally:
        store2.close()


def test_fix7b_snapshot_config_rerecords_baseline(tmp_path):
    """An authorized config change via snapshot_config re-records the
    baseline, so the next verify does not cry tamper."""
    from src import integrity as integmod
    base = make_home(tmp_path)
    cfg = make_agent_cfg(base)
    _write_config(base, '{"v": 1}')
    store = Store(cfg["state_db"], cfg["journal_dir"])
    try:
        integmod.record(store, base)
        _write_config(base, '{"v": 2}')
        integmod.snapshot_config(store, base, note="authorized change")
        assert integmod.verify(store, base) == [], \
            "authorized change flagged as tamper"
    finally:
        store.close()


# ---------------------------------------------------------------- fix 8 (recover --dry-run)
def test_fix8_recover_dry_run_uses_real_fingerprint_columns(tmp_path,
                                                           monkeypatch,
                                                           capsys):
    """cmd_recover --dry-run must not KeyError once failures exist: it
    uses the real failure_fingerprints columns (kind, last_method) and
    displays the recorded operation."""
    import argparse
    from src import failure as failuremod
    cli = _load_cli()
    base = make_home(tmp_path)
    cfg = make_agent_cfg(base)
    monkeypatch.setattr(cli, "_cfg", lambda: cfg)
    store = Store(cfg["state_db"], cfg["journal_dir"])
    store.create_task("t-rec", {"steps": [{"name": "s", "tool": "shell",
                                           "args": {"command": "true"}}]})
    store.update_task("t-rec", status="FAILED", current_step=0)
    failuremod.record(store, "t-rec", "step_failed", "my-operation",
                      "retry_step", result="failed", error_text="boom")
    store.close()
    cli.cmd_recover(argparse.Namespace(dry_run=True))  # must not raise
    out = capsys.readouterr().out
    assert "step_failed" in out, out
    assert "my-operation" in out, out
    assert "retry_step" in out, out


# ---------------------------------------------------------------- fix 9 (control token perms)
def test_fix9_token_created_atomically_0600(tmp_path, monkeypatch):
    """ensure_token creates control.token atomically with mode 0600 (no
    0644 window): with umask 022 the fd's mode is 0600 at creation, the
    file is created with O_EXCL, and run/ is 0700. A second call reads
    the existing token instead of replacing it."""
    import stat
    from src import remote as remotemod
    base = str(tmp_path / "vm9")
    old = os.umask(0o022)
    try:
        seen = {}
        real_open = os.open

        def spy_open(path, flags, mode=0o777):
            fd = real_open(path, flags, mode)
            if str(path).endswith("control.token"):
                seen["mode"] = stat.S_IMODE(os.fstat(fd).st_mode)
                seen["excl"] = bool(flags & os.O_EXCL)
            return fd

        monkeypatch.setattr(os, "open", spy_open)
        tok1 = remotemod.ensure_token(base)
        assert seen.get("excl") is True, "token not created with O_EXCL"
        assert seen.get("mode") == 0o600, \
            f"token mode at creation: {oct(seen.get('mode', 0))}"
        assert stat.S_IMODE(os.stat(os.path.join(base, "run")).st_mode) \
            == 0o700, "run/ is not 0700"
        tok2 = remotemod.ensure_token(base)
        assert tok1 == tok2 and tok1, \
            "second call must return the existing token"
    finally:
        os.umask(old)


# ---------------------------------------------------------------- fix 10 (process_running)
def test_fix10_process_running_no_shell_injection(tmp_path):
    """A hostile check pattern must have no side effect: process
    matching is done by scanning /proc in Python, never by
    interpolating the pattern into a shell string."""
    from src.verify import Verifier
    v = Verifier(None)
    marker = str(tmp_path / "pwned10")
    if os.path.exists(marker):
        os.unlink(marker)
    ok, ev = v._process_running("'; touch '" + marker + "'; '")
    assert not os.path.exists(marker), "shell injection executed!"
    assert ok is False
    # a benign pattern still matches a real process (this pytest worker)
    ok2, ev2 = v._process_running("pytest")
    assert ok2 is True, ev2
    assert ev2["pattern"] == "pytest"


# ---------------------------------------------------------------- fix 11 (policy gaps)
def test_fix11_policy_blocks_power_and_state_attacks():
    """Each newly-blocked shell form classifies PROTECTED and is not
    authorized; ordinary commands still pass."""
    from src.policy import Policy
    p = Policy()
    blocked = [
        "systemctl poweroff",
        "systemctl reboot",
        "systemctl halt",
        "systemctl kill vm-agent",
        "init 0",
        "init 6",
        "/sbin/shutdown -h now",
        "sudo /sbin/reboot",
        "env shutdown now",
        "(shutdown now)",
        "echo hi\nshutdown now",
        "bash -c 'shutdown now'",
        "find /opt/vm-agent/state -delete",
        "mv /opt/vm-agent/state /tmp/stash",
        "truncate -s 0 /opt/vm-agent/state/state.db",
        "echo x > lib/vmagent/policy.py",
        "sed -i s/x/y/ lib/vmagent/policy.py",
    ]
    for cmd in blocked:
        assert p.classify(cmd) == "PROTECTED", cmd
        allowed, _ = p.authorize(cmd)
        assert not allowed, cmd
    assert p.classify("echo hello") == "SAFE"
    assert p.classify("ls /tmp") == "SAFE"
    assert p.classify("grep shutdown /tmp/log.txt") == "SAFE"


def test_fix11_write_file_to_protected_tree_refused(tmp_path):
    """Integration through AgentRuntime: write_file aimed at
    <prefix>/lib/vmagent/policy.py is refused and the file is untouched."""
    from src.agent import AgentRuntime
    base = make_home(tmp_path)
    cfg = make_agent_cfg(base)
    libdir = os.path.join(base, "lib", "vmagent")
    os.makedirs(libdir, exist_ok=True)
    policypath = os.path.join(libdir, "policy.py")
    with open(policypath, "w") as f:
        f.write("# original")
    steps = [{"name": "evil-write", "tool": "write_file",
              "args": {"path": policypath, "content": "# pwned"}}]
    rt, result = _run_task_with_spec(tmp_path, "t-evil", steps)
    try:
        assert result == "FAILED", result
        with open(policypath) as f:
            assert f.read() == "# original", "protected file was modified!"
    finally:
        rt.store.close()


def test_fix11_write_file_dotdot_escape_refused(tmp_path):
    """A '..' traversal aiming at the state dir is resolved before the
    check and refused."""
    from src.policy import Policy
    p = Policy()
    base = str(tmp_path / "vm11")
    allowed, reason = p.authorize_path(
        base, os.path.join(base, "sub", "..", "state", "x.db"))
    assert not allowed, reason
    allowed, _ = p.authorize_path(base, "/tmp/ordinary.txt")
    assert allowed


# ---------------------------------------------------------------- fix 12 (stale task snapshot)
def test_fix12_failed_steps_accumulate_not_overwrite(tmp_path):
    """Two step failures sharing one task snapshot (as run_task passes
    a single snapshot for a long task) must accumulate in failed_steps
    with retry_count 2 — the second failure must re-read the row, not
    overwrite the first failure's values."""
    import json as jsonlib
    from src.agent import AgentRuntime
    from src import caps as capsmod
    base = make_home(tmp_path)
    cfg = make_agent_cfg(base)
    store = Store(cfg["state_db"], cfg["journal_dir"])
    steps = [
        {"name": "s0", "tool": "shell", "retries": 0,
         "args": {"command": "exit 3"}},
        {"name": "s1", "tool": "shell", "retries": 0,
         "args": {"command": "exit 3"}},
    ]
    store.create_task("t-12", {"steps": steps})
    capsmod.grant_all_basic(store, "t-12", {"shell"})
    store.close()
    rt = AgentRuntime(cfg)
    try:
        task = rt.store.get_task("t-12")  # one snapshot for the whole task
        assert rt._run_step("t-12", 0, steps[0], task) is False
        assert rt._run_step("t-12", 1, steps[1], task) is False
        fresh = rt.store.get_task("t-12")
        assert jsonlib.loads(fresh["failed_steps"]) == [0, 1], \
            fresh["failed_steps"]
        assert fresh["retry_count"] == 2, fresh["retry_count"]
    finally:
        rt.store.close()


def test_fix12_two_failing_then_resumed_steps(tmp_path):
    """End to end: step 0 fails, the task is resumed past it, step 1
    fails — failed_steps is [0, 1] and retry_count is 2."""
    import json as jsonlib
    from src.agent import AgentRuntime
    base = make_home(tmp_path)
    cfg = make_agent_cfg(base)
    store = Store(cfg["state_db"], cfg["journal_dir"])
    steps = [
        {"name": "s0", "tool": "shell", "retries": 0,
         "args": {"command": "exit 3"}},
        {"name": "s1", "tool": "shell", "retries": 0,
         "args": {"command": "exit 3"}},
    ]
    store.create_task("t-12b", {"steps": steps})
    store.close()
    rt = AgentRuntime(cfg)
    try:
        assert rt.run_task("t-12b") == "FAILED"      # s0 fails
        t = rt.store.get_task("t-12b")
        assert jsonlib.loads(t["failed_steps"]) == [0]
        assert t["retry_count"] == 1
        rt.store.update_task("t-12b", status="PENDING", current_step=1)
        assert rt.run_task("t-12b") == "FAILED"      # s1 fails
        t = rt.store.get_task("t-12b")
        assert jsonlib.loads(t["failed_steps"]) == [0, 1], \
            t["failed_steps"]
        assert t["retry_count"] == 2, t["retry_count"]
    finally:
        rt.store.close()


# ---------------------------------------------------------------- fix 13 (re-queue stuck RUNNING)
def test_fix13_reconcile_requeues_stuck_running(tmp_path):
    """A task stuck RUNNING with no live agent is actually re-queued as
    PENDING, its stale lease released, and TASK_REQUEUED_ON_BOOT
    journaled — so a later run_task can claim_lease and resume."""
    import json as jsonlib
    import glob
    from src.recovery import RecoveryManager
    base = make_home(tmp_path)
    cfg = make_agent_cfg(base)
    store = Store(cfg["state_db"], cfg["journal_dir"])
    steps = [{"name": "s0", "tool": "write_file",
              "args": {"path": str(tmp_path / "fix13_out.txt"),
                       "content": "hello"}}]
    store.create_task("t-13", {"steps": steps})
    store.update_task("t-13", status="RUNNING", current_step=0)
    assert store.claim_lease("t-13", "dead-worker", "agent:99999999",
                             ttl_s=3600)
    # note: no agent heartbeat written -> dead agent
    store2 = Store(cfg["state_db"], cfg["journal_dir"])
    rm = RecoveryManager(store2, cfg)
    try:
        report = rm.reconcile_on_boot({"base_dir": base})
        t = rm.store.get_task("t-13")
        assert t["status"] == "PENDING", t["status"]
        # stale lease really released (not merely stealable later)
        n = rm.store._conn.execute(
            "SELECT COUNT(*) FROM leases WHERE task_id=?",
            ("t-13",)).fetchone()[0]
        assert n == 0, f"stale lease still held: {n}"
        # ...so a new worker can claim it
        assert rm.store.claim_lease("t-13", "new-worker",
                                    f"agent:{os.getpid()}", ttl_s=60)
        events = []
        for path in glob.glob(os.path.join(cfg["journal_dir"], "*.jsonl")):
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        events.append(jsonlib.loads(line).get("event"))
        assert "TASK_REQUEUED_ON_BOOT" in events
        assert any("re-queued" in f for f in report["fixed"])
    finally:
        rm.store.close()


# ---------------------------------------------------------------- fix 14 (timeout cap + control error type)
def test_fix14_timeout_s_capped_at_config_max():
    """A spec-supplied timeout_s above tool_timeout_max_s is clamped to
    the max; smaller values pass through; the non-positive fallback to
    the default is unchanged."""
    from src.tools import Executor
    ex = Executor({"tool_timeout_max_s": 30, "tool_timeout_default_s": 10})
    assert ex._sane_timeout(9999) == 30
    assert ex._sane_timeout(20) == 20
    assert ex._sane_timeout(0) == 10
    assert ex._sane_timeout(None) == 10


def test_fix14_control_failed_logs_exception_type(tmp_path, monkeypatch):
    """When the control server fails to start, the journal records the
    exception type explicitly — not just repr(e)."""
    import json as jsonlib
    import glob
    from src.agent import AgentRuntime
    import src.remote as remotemod
    base = make_home(tmp_path)
    cfg = make_agent_cfg(base)
    rt = AgentRuntime(cfg)
    try:
        def boom(*a, **k):
            raise ValueError("port in use")
        monkeypatch.setattr(remotemod, "ControlServer", boom)
        assert rt._start_control() is None
        events = []
        for path in glob.glob(os.path.join(cfg["journal_dir"], "*.jsonl")):
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        events.append(jsonlib.loads(line))
        failed = [e for e in events if e.get("event") == "CONTROL_FAILED"]
        assert failed, "no CONTROL_FAILED journaled"
        assert failed[0].get("error_type") == "ValueError", failed[0]
    finally:
        rt.store.close()
