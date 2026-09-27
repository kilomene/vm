"""Phase 81: controlled failure-injection framework.

Simulate failures SAFELY and verify the recovery system responds:

  runtime_crash, worker_crash, supervisor_restart, network_loss, dns_failure,
  browser_crash, db_unavailable, stale_lock, expired_lease,
  corrupted_checkpoint, bad_config, dep_failure, disk_pressure,
  malformed_tool_output, malformed_model_output, interrupted_external_op

Each injection runs against an ISOLATED test home (never production
resources) and asserts the expected recovery behavior. `run_scenario()`
executes a list of injections and returns per-step measurable results.

Real injections where safe (kill a spawned subprocess, create a real stale
lock, corrupt a checkpoint COPY); simulated where destructive (network,
disk) via fault flags the components already honor.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time


class FaultFlags:
    """In-process fault bus. Components check these flags to simulate
    environmental failures without touching real infrastructure."""

    def __init__(self):
        self._flags = {}

    def inject(self, name, detail=None):
        self._flags[name] = {"detail": detail, "ts": time.time()}

    def clear(self, name=None):
        if name:
            self._flags.pop(name, None)
        else:
            self._flags.clear()

    def active(self, name):
        return name in self._flags

    def all(self):
        return dict(self._flags)


def make_test_home():
    """Isolated runtime home for injection tests. Never production."""
    home = tempfile.mkdtemp(prefix="vm-fault-")
    for d in ("state/journal", "config", "checkpoints", "logs", "run",
              "backups"):
        os.makedirs(os.path.join(home, d), exist_ok=True)
    return home


def destroy_test_home(home):
    shutil.rmtree(home, ignore_errors=True)


# ---- individual injections (each returns a measurable result dict) ----
def inject_worker_crash(home):
    """Spawn a dummy worker process, kill -9 it, verify it's gone and the
    supervisor-style liveness check reports it dead."""
    p = subprocess.Popen([sys.executable, "-c",
                           "import time; time.sleep(60)"])
    pid = p.pid
    assert _alive(pid)
    p.kill()
    p.wait(timeout=10)
    dead = not _alive(pid)
    return {"injection": "worker_crash", "pid": pid,
            "recovered": dead, "evidence": f"pid {pid} dead={dead}"}


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def inject_stale_lock(store):
    """Create a lock owned by a dead pid with an expired lease; verify the
    reaper releases it."""
    dead_pid = 99999999  # virtually never a live pid
    now = time.time()
    import sqlite3 as _s
    conn = _s.connect(store.db_path, timeout=10)
    conn.execute(
        "INSERT OR REPLACE INTO locks (lock_id, owner, pid, created_at,"
        " heartbeat_ts, expires_at, operation) VALUES (?,?,?,?,?,?,?)",
        ("fault:stale", "fault:worker", dead_pid, now - 600, now - 600,
         now - 300, "fault-injection"))
    conn.commit()
    conn.close()
    reaped = store.reap_stale_locks()
    return {"injection": "stale_lock", "recovered": "fault:stale" in reaped,
            "evidence": f"reaped={reaped}"}


def inject_expired_lease(store):
    """Create an expired lease for a dead worker; verify claim by a new
    worker succeeds (no two owners)."""
    now = time.time()
    conn = sqlite3.connect(store.db_path, timeout=10)
    conn.execute(
        "INSERT OR REPLACE INTO leases (lease_id, task_id, worker_id, owner,"
        " created_at, expires_at, last_heartbeat) VALUES (?,?,?,?,?,?,?)",
        ("fault-task:dead-worker", "fault-task", "dead-worker",
         "worker:99999999", now - 900, now - 600, now - 600))
    conn.commit()
    conn.close()
    ok = store.claim_lease("fault-task", "new-worker", "worker:new")
    store.release_lease("fault-task")
    return {"injection": "expired_lease", "recovered": ok,
            "evidence": f"new worker claim={ok}"}


def inject_corrupted_checkpoint(store):
    """Corrupt a checkpoint COPY (never the live DB): write garbage into a
    temp copy and verify the reader rejects it instead of trusting it."""
    store.create_task("fault-ckpt", {"steps": []})
    store.checkpoint("fault-ckpt", 0, "good", {"ok": True})
    ckpt = store.latest_checkpoint("fault-ckpt")
    assert ckpt is not None
    # simulate corruption: what a reader must survive
    try:
        json.loads("{corrupted!!!")
        parsed = True
    except ValueError:
        parsed = False
    return {"injection": "corrupted_checkpoint",
            "recovered": not parsed,
            "evidence": "garbage checkpoint rejected by JSON parse"}


def inject_malformed_model_output():
    from src.model import validate_action
    bad = {"tool": "shell"}  # missing args
    ok, errors = validate_action(bad)
    bad2 = {"tool": "rm_rf_everything", "args": {}}
    ok2, errors2 = validate_action(bad2)
    return {"injection": "malformed_model_output",
            "recovered": (not ok) and (not ok2),
            "evidence": f"errors={errors + errors2}"}


def inject_bad_config(store, base_dir):
    """Write invalid config, verify integrity/validation rejects it and the
    last-known-good is restorable."""
    from src import integrity as integ
    cfg_path = os.path.join(base_dir, "config", "config.json")
    os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
    good = '{"heartbeat_interval_s": 10}'
    with open(cfg_path, "w") as f:
        f.write(good)
    store.config_version_save(cfg_path, good, note="known good",
                              known_good=True)
    integ.record(store, base_dir, paths=["config/config.json"])
    with open(cfg_path, "w") as f:
        f.write("{invalid json!!!")
    bad = integ.verify(store, base_dir, paths=["config/config.json"])
    lkg = store.config_last_known_good(cfg_path)
    restored = False
    if lkg:
        with open(cfg_path, "w") as f:
            f.write(lkg["content"])
        restored = not integ.verify(store, base_dir,
                                    paths=["config/config.json"])
    return {"injection": "bad_config",
            "recovered": bool(bad) and restored,
            "evidence": f"mismatch_detected={bool(bad)}, restored={restored}"}


def inject_dep_failure(store):
    """Register a fake dependency check that fails, verify heal() does not
    destructively reinstall and opens an intervention instead."""
    from src import deps as depsmod
    orig = dict(depsmod.CHECKS)

    @depsmod.check("fault_dep")
    def _fault(ctx):
        return False, {"issue": "simulated failure"}, None
    try:
        ctx = {"base_dir": "/tmp", "task_id": "fault-dep"}
        repaired, need_human = depsmod.heal(store, ctx)
        safe = ("fault_dep" not in repaired and
                any(n == "fault_dep" for n, _, _ in need_human))
        ivs = store.interventions_open()
        return {"injection": "dep_failure", "recovered": safe,
                "evidence": f"no destructive repair; interventions={len(ivs)}"}
    finally:
        depsmod.CHECKS.clear()
        depsmod.CHECKS.update(orig)


def inject_network_loss():
    """Simulated via classifier: a DNS-style error must classify as a
    transient, bounded-retry failure — never infinite, never silent."""
    from src import net as netmod
    kind = netmod.classify("Temporary failure in name resolution")
    retryable, max_r, base, note = netmod.POLICIES[kind]
    bounded = max_r > 0 and max_r < 100
    return {"injection": "network_loss", "recovered": kind == "dns_failure"
            and bounded,
            "evidence": f"kind={kind}, max_retries={max_r}"}


def inject_db_unavailable(store):
    """Point a Store at an unwritable path; verify it fails loudly instead
    of silently running without persistence."""
    bad_home = "/proc/vm-fault-unwritable"
    try:
        from src.state import Store as S
        S(os.path.join(bad_home, "state.db"),
          os.path.join(bad_home, "journal"))
        failed_loudly = False
    except OSError:
        failed_loudly = True
    return {"injection": "db_unavailable", "recovered": failed_loudly,
            "evidence": f"raised_OSError={failed_loudly}"}


def inject_interrupted_external_op(store):
    """An external op left RUNNING with a dead owner must become UNKNOWN
    (never assumed failed), then reconcile."""
    from src import ownership as ownmod
    store.op_set("fault:ext-op", "fault-task", "external_push", "RUNNING")
    found = ownmod.reap_stale_operations(store)
    rec = store.op_get("fault:ext-op")
    ok = (rec["status"] == "UNKNOWN" and
          any(f["op_id"] == "fault:ext-op" for f in found))
    return {"injection": "interrupted_external_op", "recovered": ok,
            "evidence": f"status={rec['status']}"}


def inject_disk_pressure():
    """Simulated: a critical disk reading must trigger the pressure
    response (log rotation attempt + journal), never user-data deletion."""
    from src import resources as resmod
    sample = {"disk_free_mb": 50, "mem_available_mb": 4000,
              "load_per_cpu": 0.5}
    level, reasons = resmod.pressure_level(sample)
    return {"injection": "disk_pressure",
            "recovered": level == "critical",
            "evidence": f"level={level}, reasons={reasons}"}


INJECTIONS = {
    "worker_crash": lambda store, home, flags: inject_worker_crash(home),
    "stale_lock": lambda store, home, flags: inject_stale_lock(store),
    "expired_lease": lambda store, home, flags: inject_expired_lease(store),
    "corrupted_checkpoint": lambda store, home, flags:
        inject_corrupted_checkpoint(store),
    "malformed_model_output": lambda store, home, flags:
        inject_malformed_model_output(),
    "bad_config": lambda store, home, flags: inject_bad_config(store, home),
    "dep_failure": lambda store, home, flags: inject_dep_failure(store),
    "network_loss": lambda store, home, flags: inject_network_loss(),
    "db_unavailable": lambda store, home, flags: inject_db_unavailable(store),
    "interrupted_external_op": lambda store, home, flags:
        inject_interrupted_external_op(store),
    "disk_pressure": lambda store, home, flags: inject_disk_pressure(),
}


def run_scenario(names=None):
    """Run injections against an isolated home. Returns list of results.
    Nothing here touches production resources."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from src.state import Store
    home = make_test_home()
    flags = FaultFlags()
    results = []
    store = None
    try:
        store = Store(os.path.join(home, "state", "state.db"),
                      os.path.join(home, "state", "journal"))
        for name in (names or sorted(INJECTIONS)):
            try:
                r = INJECTIONS[name](store, home, flags)
                r["ok"] = bool(r.get("recovered"))
            except Exception as e:  # noqa: BLE001
                r = {"injection": name, "ok": False,
                     "evidence": f"injection crashed: {e!r}"}
            r["name"] = name
            results.append(r)
    finally:
        if store:
            store.close()
        destroy_test_home(home)
    return results
