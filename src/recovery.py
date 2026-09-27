"""Phase 40/41/33: escalating recovery manager, safe mode, startup reconciliation.

Recovery levels (least destructive first):
  L1 retry operation
  L2 restart affected worker
  L3 restart browser/tool subsystem
  L4 restart agent runtime
  L5 repair dependency
  L6 reload task state
  L7 enter safe mode
  L8 request human intervention

Safe mode: preserve data, stop dangerous actions, keep logs/diagnostics,
allow inspection, no repeated destructive retries.

Startup reconciliation (phase 33): after boot, compare persistent state
against the actual environment (processes, filesystem, locks, leases,
network, deps) and resolve inconsistencies BEFORE resuming tasks.
"""
import os
import time

from . import deps as depsmod
from . import integrity as integ
from . import net as netmod
from . import resources as resmod
from .state import _pid_alive


LEVELS = {
    1: "retry operation",
    2: "restart worker",
    3: "restart browser/tool subsystem",
    4: "restart agent runtime",
    5: "repair dependency",
    6: "reload task state",
    7: "safe mode",
    8: "human intervention",
}


class RecoveryManager:
    def __init__(self, store, cfg, journal=None):
        self.store = store
        self.cfg = cfg
        self.journal = journal or store.journal
        self._fail_counts = {}  # (task_id, kind) -> consecutive failures

    # ---- escalation ----
    def record_failure(self, task_id, kind):
        key = (task_id, kind)
        self._fail_counts[key] = self._fail_counts.get(key, 0) + 1
        return self._fail_counts[key]

    def record_success(self, task_id, kind):
        self._fail_counts.pop((task_id, kind), None)

    def level_for(self, task_id, kind):
        """Escalate with consecutive failures; cap the fast loop."""
        n = self._fail_counts.get((task_id, kind), 0)
        if n <= 1:
            return 1
        if n == 2:
            return 2
        if n == 3:
            return 3
        if n <= 5:
            return 4
        if n <= 7:
            return 5
        if n <= 9:
            return 6
        if n <= 11:
            return 7
        return 8

    def recover(self, task_id, kind, detail=None, agent=None):
        """Choose and execute the lowest destructive level that fits."""
        level = self.level_for(task_id, kind)
        self.journal("RECOVERY_LEVEL", task_id=task_id, kind=kind,
                     level=level, name=LEVELS[level], detail=detail)
        if level >= 7:
            self.enter_safe_mode(task_id, f"escalated to L{level}: {kind}")
        if level == 8 and agent is not None:
            agent.pause_task(task_id, reason=f"L8: {kind}: {detail}")
            self.store.intervention_open(
                task_id=task_id,
                reason=f"automatic recovery exhausted for: {kind}",
                required_action="human review required",
                last_verified_step=str(detail)[:200])
        return level

    # ---- safe mode (phase 41) ----
    def enter_safe_mode(self, task_id=None, reason=""):
        self.store.kv_set("safe_mode", {"active": True, "ts": time.time(),
                                        "reason": reason, "task_id": task_id})
        self.journal("SAFE_MODE_ENTERED", task_id=task_id, reason=reason)

    def exit_safe_mode(self, note=""):
        self.store.kv_set("safe_mode", {"active": False, "ts": time.time(),
                                        "note": note})
        self.journal("SAFE_MODE_EXITED", note=note)

    def in_safe_mode(self):
        sm = self.store.kv_get("safe_mode") or {}
        return bool(sm.get("active"))

    # ---- startup reconciliation (phase 33) ----
    def reconcile_on_boot(self, ctx):
        """Compare saved state vs actual environment. Returns report dict."""
        report = {"ts": time.time(), "fixed": [], "warnings": []}
        self.journal("RECONCILE_STARTED")

        # 1. stale locks
        reaped = self.store.reap_stale_locks()
        if reaped:
            report["fixed"].append(f"reaped stale locks: {reaped}")

        # 2. expired leases held by dead workers -> release
        with_dead = []
        for task in self.store.list_tasks():
            # leases table scan via direct query
            pass  # handled below via store internals
        report["fixed"].extend(self._reap_dead_leases())

        # 3. tasks stuck RUNNING from a dead agent -> mark for resume
        agent_hb = self.store.get_heartbeat("agent")
        agent_alive = agent_hb and _pid_alive(agent_hb.get("pid") or 0)
        for t in self.store.list_tasks(status="RUNNING"):
            if not agent_alive:
                report["warnings"].append(
                    f"task {t['task_id']} was RUNNING with no live agent;"
                    " will resume from checkpoint")

        # 4. file integrity on tracked files
        bad = integ.verify(self.store, ctx.get("base_dir", "/tmp"))
        if bad:
            report["warnings"].append(f"integrity mismatches: {bad}")
        else:
            report["fixed"].append("integrity check clean")

        # 5. dependency check
        dep_results = depsmod.check_all(self.store, ctx)
        failed = [k for k, (ok, _) in dep_results.items() if not ok]
        if failed:
            report["warnings"].append(f"failed deps: {failed}")

        # 6. resource snapshot
        snap = resmod.sample()
        level, reasons = resmod.pressure_level(snap)
        report["resources"] = {"level": level, "reasons": reasons,
                               "sample": snap}
        if level != "ok":
            report["warnings"].append(f"resource pressure: {reasons}")

        # 7. network classification probe (DNS)
        try:
            import socket as _s
            _s.getaddrinfo("example.com", 80, timeout=5)
            report["fixed"].append("network: dns ok")
        except OSError as e:
            kind = netmod.classify(str(e))
            report["warnings"].append(f"network: {kind}: {e}")

        self.journal("RECONCILE_COMPLETED", **{k: v for k, v in report.items()
                                               if k != "resources"})
        return report

    def _reap_dead_leases(self):
        fixed = []
        # direct SQL: leases whose owner pid is dead and lease expired
        import sqlite3
        conn = sqlite3.connect(self.store.db_path, timeout=10)
        try:
            rows = conn.execute("SELECT lease_id, task_id, worker_id, owner,"
                                " expires_at FROM leases").fetchall()
            now = time.time()
            for lid, tid, wid, owner, exp in rows:
                try:
                    pid = int(str(owner).rsplit(":", 1)[-1])
                except (ValueError, AttributeError):
                    pid = 0
                if exp < now and not _pid_alive(pid):
                    conn.execute("DELETE FROM leases WHERE lease_id=?", (lid,))
                    fixed.append(f"released dead lease {lid}")
            conn.commit()
        finally:
            conn.close()
        for f in fixed:
            self.journal("LEASE_REAPED", detail=f)
        return fixed
