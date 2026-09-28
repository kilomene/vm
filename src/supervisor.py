"""Supervisor: independent process that owns agent continuity.

The supervisor:
  - starts the agent as a child process (never the reverse)
  - holds an exclusive lock so two supervisors can never run
  - watches heartbeats; restarts on crash OR suspected hang
  - uses exponential backoff; refuses infinite rapid restart loops
  - collects diagnostics before every restart
  - on (re)start, the agent itself recovers unfinished tasks

Hang detection: heartbeat stale beyond heartbeat_timeout_s AND no task
progress (current_step unchanged) for no_progress_timeout_s => suspected hang.
CPU usage is NOT used as liveness proof.
"""
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
import traceback

from . import config as config_mod
from .state import Store
from .recovery import RecoveryManager


class Supervisor:
    def __init__(self, cfg):
        self.cfg = cfg
        self.store = Store(cfg["state_db"], cfg["journal_dir"])
        self.recovery = RecoveryManager(self.store, cfg,
                                        journal=self.store.journal)
        self.base = cfg["base_dir"]
        self.run_dir = cfg["run_dir"]
        os.makedirs(self.run_dir, exist_ok=True)
        self.lock_path = os.path.join(self.run_dir, "supervisor.lock")
        self.pid_path = os.path.join(self.run_dir, "supervisor.pid")
        self.agent_proc = None
        self._shutdown = False
        self._lock_fh = None
        self._last_progress = {}  # task_id -> (step, ts)
        self._clock_bad = 0   # consecutive failed clock checks
        self._clock_good = 0  # consecutive good clock checks

    # ---- exclusivity ----
    def acquire_lock(self):
        self._lock_fh = open(self.lock_path, "w")
        try:
            fcntl.flock(self._lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit("another supervisor is already running")
        self._lock_fh.write(str(os.getpid()))
        self._lock_fh.flush()
        with open(self.pid_path, "w") as f:
            f.write(str(os.getpid()))

    # ---- agent lifecycle ----
    def start_agent(self):
        env = dict(os.environ, VM_AGENT_HOME=self.base,
                   PYTHONPATH=os.path.join(self.base, "lib"))
        self.agent_proc = subprocess.Popen(
            [sys.executable, "-m", "vmagent.agent"],
            cwd=self.base, env=env,
            stdout=open(os.path.join(self.base, "logs", "agent.out"), "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True)
        self.store.journal("AGENT_STARTED", pid=self.agent_proc.pid,
                           by="supervisor")
        self.store.record_restart("agent", "supervisor start")
        return self.agent_proc

    def stop_agent(self, timeout=20):
        p = self.agent_proc
        if not p or p.poll() is not None:
            return
        p.terminate()  # SIGTERM -> graceful shutdown handler in agent
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.store.journal("AGENT_KILL", pid=p.pid,
                               reason="graceful timeout exceeded")
            p.kill()
            p.wait(timeout=10)
        self.agent_proc = None

    def collect_diagnostics(self, reason):
        d = {"ts": time.time(), "reason": reason, "supervisor_pid": os.getpid()}
        try:
            if self.agent_proc:
                d["agent_pid"] = self.agent_proc.pid
                d["agent_returncode"] = self.agent_proc.poll()
            hb = self.store.get_heartbeat("agent")
            d["last_heartbeat"] = hb
            d["tasks"] = [(t["task_id"], t["status"], t["current_step"])
                          for t in self.store.list_tasks()]
        except Exception as e:  # noqa: BLE001 - diagnostics must not fail
            d["diag_error"] = repr(e)
        path = os.path.join(self.base, "logs",
                            f"diag-{int(time.time())}.json")
        with open(path, "w") as f:
            json.dump(d, f, indent=2, default=str)
        self.store.journal("DIAGNOSTICS_COLLECTED", path=path, reason=reason)
        return path

    # ---- backoff ----
    def backoff_delay(self):
        hour_ago = time.time() - 3600
        n = self.store.restart_count_since("agent", hour_ago)
        if n >= self.cfg["max_restarts_per_hour"]:
            return None  # refuse: too many restarts, stay down and alert
        delay = min(self.cfg["restart_backoff_base_s"] * (2 ** n),
                    self.cfg["restart_backoff_max_s"])
        return delay

    def restart_agent(self, reason):
        self.store.journal("AGENT_RESTARTING", reason=reason)
        self.collect_diagnostics(reason)
        self.stop_agent()
        delay = self.backoff_delay()
        if delay is None:
            self.store.journal("RESTART_REFUSED",
                               reason="max restarts per hour exceeded")
            return False
        time.sleep(delay)
        self.start_agent()
        return True

    # ---- health evaluation ----
    def agent_health(self):
        """Returns (state, detail): ok | crashed | hung."""
        p = self.agent_proc
        if not p or p.poll() is not None:
            return "crashed", f"process exited rc={p.poll() if p else 'gone'}"
        hb = self.store.get_heartbeat("agent")
        if not hb:
            # agent may still be starting up; give it 60s
            if time.time() - self._start_ts < 60:
                return "ok", "starting"
            return "hung", "no heartbeat ever received"
        # Phase 70: an unreliable clock invalidates heartbeat freshness —
        # fail closed instead of trusting stale-but-"fresh" beats.
        from . import timecheck as timecheckmod
        if not timecheckmod.lease_valid(hb["ts"], max_age_s=None,
                                        check_clock=True):
            return "hung", ("clock unreliable: heartbeat freshness cannot "
                            "be verified (fail-closed)")
        stale_s = time.time() - hb["ts"]
        if stale_s > self.cfg["heartbeat_timeout_s"]:
            # stale heartbeat: hang only if also no task progress.
            # Keep the ORIGINAL first-seen timestamp for an unchanged step
            # so idle time accumulates across ticks; only (re)baseline when
            # the step (or task) actually advances. Overwriting the entry
            # on every tick resets idle_for to ~one tick and hang detection
            # can never fire.
            tid, step = hb.get("task_id"), hb.get("step")
            key = tid or "__idle__"
            prev = self._last_progress.get(key)
            if prev is None or prev[0] != step:
                self._last_progress[key] = (step, time.time())
                return "ok", f"heartbeat stale {stale_s:.0f}s but progressing"
            idle_for = time.time() - prev[1]
            if idle_for > self.cfg["no_progress_timeout_s"]:
                return "hung", (f"heartbeat stale {stale_s:.0f}s, "
                                f"no progress for {idle_for:.0f}s")
            return "ok", f"heartbeat stale {stale_s:.0f}s but progressing"
        # heartbeat fresh again: drop any accumulated idle baseline so a
        # recovered agent is not judged by its old stall.
        self._last_progress.pop(hb.get("task_id") or "__idle__", None)
        return "ok", "healthy"

    # ---- time synchronization (phase 70) ----
    def _clock_check(self):
        """One time-sync check with hysteresis.

        A single bad check (e.g. VM suspend/resume, where wall time jumps
        but monotonic doesn't) must not latch the system in safe mode:
        safe mode is entered only after clock_fail_threshold consecutive
        failures, and a clock-caused safe mode auto-clears after
        clock_recover_threshold consecutive good checks. Escalation-caused
        safe mode (reason_kind != "clock") is never auto-cleared — only an
        operator clears it. Returns (ok, detail).
        """
        from . import timecheck as timecheckmod
        clock_ok, clock_detail = timecheckmod.check_sync()
        self.store.world_set("supervisor.clock", {
            "ok": clock_ok, "detail": clock_detail,
            "checked_at": time.time()}, verifier="supervisor:time")
        if not clock_ok:
            self._clock_bad += 1
            self._clock_good = 0
            self.store.journal("CLOCK_UNRELIABLE", detail=clock_detail,
                               consecutive=self._clock_bad)
            if (self._clock_bad >= self.cfg.get("clock_fail_threshold", 3)
                    and not self.recovery.in_safe_mode()):
                self.store.journal(
                    "CLOCK_UNRELIABLE",
                    detail=clock_detail,
                    action="safe mode (fail-closed)")
                self.recovery.enter_safe_mode(
                    None, f"clock unreliable: {clock_detail}",
                    reason_kind="clock")
        else:
            self._clock_good += 1
            self._clock_bad = 0
            sm = self.store.kv_get("safe_mode") or {}
            if (sm.get("active") and sm.get("reason_kind") == "clock"
                    and self._clock_good >= self.cfg.get(
                        "clock_recover_threshold", 3)):
                self.recovery.exit_safe_mode(
                    note="clock recovered: auto-cleared clock-caused "
                         "safe mode")
        return clock_ok, clock_detail

    # ---- main loop ----
    def install_signal_handlers(self):
        def _term(signum, frame):
            self._shutdown = True
        signal.signal(signal.SIGTERM, _term)
        signal.signal(signal.SIGINT, _term)

    def serve_forever(self):
        self.acquire_lock()
        self.install_signal_handlers()
        self._start_ts = time.time()
        self.store.journal("SUPERVISOR_STARTED", pid=os.getpid())
        self.store.heartbeat("supervisor", pid=os.getpid(), status="alive")
        # Phase 33: reconcile before the agent starts — stale locks, leases,
        # RUNNING tasks from a dead agent, config integrity.
        ctx = {"base_dir": self.base}
        self.recovery.reconcile_on_boot(ctx)
        # Phase 41: if we booted into safe mode, journal it loudly.
        if self.recovery.in_safe_mode():
            self.store.journal("SAFE_MODE_ACTIVE_AT_BOOT")
        self.start_agent()
        last_hb = 0
        last_clock_check = 0
        while not self._shutdown:
            state, detail = self.agent_health()
            # Phase 70: time synchronization — fail closed on unreliable
            # clocks, since heartbeats/leases are meaningless without time.
            # _clock_check applies hysteresis: one blip never latches safe
            # mode; clock-caused safe mode auto-clears on recovery.
            if time.time() - last_clock_check > 60:
                last_clock_check = time.time()
                self._clock_check()
            if state == "crashed":
                self.store.journal("AGENT_CRASHED", detail=detail)
                if not self.restart_agent(f"crash: {detail}"):
                    break
                self._start_ts = time.time()
            elif state == "hung":
                self.store.journal("AGENT_HUNG", detail=detail)
                if not self.restart_agent(f"hang: {detail}"):
                    break
                self._start_ts = time.time()
            if time.time() - last_hb > 30:
                last_hb = time.time()
                self.store.heartbeat("supervisor", pid=os.getpid(),
                                     status="alive")
            time.sleep(5)
        # graceful shutdown: stop agent, keep state
        self.store.journal("SUPERVISOR_STOPPING")
        self.stop_agent()
        self.store.journal("SUPERVISOR_STOPPED")
        try:
            os.unlink(self.pid_path)
        except OSError:
            pass


def main():
    cfg = config_mod.load()
    sup = Supervisor(cfg)
    try:
        sup.serve_forever()
    except Exception:
        # last-resort: never die silently
        with open(os.path.join(cfg["base_dir"], "logs", "supervisor.crash"),
                  "a") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%S") + "\n")
            traceback.print_exc(file=f)
        raise


if __name__ == "__main__":
    main()
