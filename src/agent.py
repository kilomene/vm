"""Agent runtime: the task engine.

The LLM (or a task spec) proposes steps. This engine:
  1. loads the task + last checkpoint from SQLite (never from memory)
  2. executes each step via the Executor
  3. verifies each step via the Verifier
  4. checkpoints only after verification passes
  5. heartbeats every N seconds with pid/task/step/operation
  6. on failure: retries with backoff, then marks FAILED (supervisor decides)

Task spec format (JSON):
  {"priority": "HIGH",            # CRITICAL|HIGH|NORMAL|LOW (default NORMAL)
   "budgets": {"tool_calls": 200, "retries": 5, "duration_s": 3600},
   "steps": [
     {"name": "install nginx",
      "tool": "shell", "args": {"command": "apt-get install -y nginx"},
      "verify": [{"check": "command_ok", "command": "nginx -v"}],
      "idempotent_check": {"check": "command_ok", "command": "which nginx"},
      "op_id": "install_nginx",   # idempotent operation ID (phase 30)
      "retries": 2},
     ...
  ]}

`idempotent_check`: if it passes, the step is skipped (already done) —
this is what makes resume-after-crash safe.
"""
import os
import signal
import sys
import time

from . import config as config_mod
from . import resources as resmod
from .recovery import RecoveryManager
from .state import Store
from .tools import Executor
from .verify import Verifier
from .policy import Policy
from .txn import TxnRunner

PRIORITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "NORMAL": 2, "LOW": 3}

# Phase 52: state-aware cancellation flow
CANCEL_FLOW = ["CANCEL_REQUESTED", "STOPPING", "CLEANUP", "CHECKPOINT",
               "CANCELLED"]


class AgentRuntime:
    def __init__(self, cfg):
        self.cfg = cfg
        self.store = Store(cfg["state_db"], cfg["journal_dir"])
        self.journal = self.store.journal
        self.ex = Executor(cfg, journal=self._journal_fn)
        self.verifier = Verifier(self.ex)
        self.policy = Policy()
        self.txn = TxnRunner(self.store, self.ex, self.verifier,
                             journal=self._journal_fn)
        self.recovery = RecoveryManager(self.store, cfg,
                                         journal=self._journal_fn)
        self.pid = os.getpid()
        self._shutdown = False
        self._current_task = None
        self._current_step = 0
        self._current_op = "idle"
        self._last_beat = 0
        self._cancel_requested = set()  # task_ids with cancellation in flight
        self._task_start_ts = {}

    def _journal_fn(self, event, **kw):
        kw.setdefault("task_id", self._current_task)
        self.journal(event, **kw)

    # ---- lifecycle ----
    def install_signal_handlers(self):
        def _term(signum, frame):
            self._shutdown = True
            self.journal("AGENT_SHUTDOWN", task_id=self._current_task,
                         reason="SIGTERM")
        signal.signal(signal.SIGTERM, _term)
        signal.signal(signal.SIGINT, _term)

    def beat(self, operation=None, last_action=None):
        now = time.time()
        if now - self._last_beat < self.cfg["heartbeat_interval_s"]:
            return
        self._last_beat = now
        self.store.heartbeat("agent", pid=self.pid, task_id=self._current_task,
                             step=self._current_step,
                             operation=operation or self._current_op,
                             last_action=last_action, status="alive")

    # ---- remote-control / lifecycle ops (phases 50/52) ----
    def request_pause(self, task_id):
        task = self.store.get_task(task_id)
        if not task or task["status"] not in ("RUNNING", "PENDING"):
            return f"cannot pause: status={task['status'] if task else 'missing'}"
        self.store.update_task(task_id, status="PAUSED")
        self.journal("TASK_PAUSE_REQUESTED", task_id=task_id)
        return "paused (takes effect at next step boundary)"

    def request_resume(self, task_id):
        task = self.store.get_task(task_id)
        if not task or task["status"] not in ("PAUSED", "FAILED"):
            return f"cannot resume: status={task['status'] if task else 'missing'}"
        self.store.update_task(task_id, status="PENDING")
        self.journal("TASK_RESUME_REQUESTED", task_id=task_id)
        return "queued for resume"

    def request_cancel(self, task_id):
        """Phase 52: state-aware cancellation, not abrupt kill."""
        task = self.store.get_task(task_id)
        if not task or task["status"] in ("COMPLETED", "CANCELLED", "FAILED"):
            return f"cannot cancel: status={task['status'] if task else 'missing'}"
        self._cancel_requested.add(task_id)
        self.store.update_task(task_id, status="CANCEL_REQUESTED")
        self.journal("TASK_CANCEL_REQUESTED", task_id=task_id)
        return "cancellation requested (STOPPING -> CLEANUP -> CHECKPOINT -> CANCELLED)"

    def _cancellation_check(self, task_id, idx):
        """Advance the cancel flow at step boundaries. Returns True to stop."""
        if task_id not in self._cancel_requested:
            return False
        for state in ("STOPPING", "CLEANUP", "CHECKPOINT"):
            self.store.update_task(task_id, status=state)
            self.journal("TASK_CANCEL_" + state, task_id=task_id, step=idx)
        # final checkpoint before marking cancelled
        self._checkpoint(task_id, idx, "cancel-checkpoint",
                         {"cancelled_at_step": idx})
        self.store.update_task(task_id, status="CANCELLED")
        self.store.release_lease(task_id)
        self.journal("TASK_CANCELLED", task_id=task_id)
        self._cancel_requested.discard(task_id)
        return True

    def pause_low_priority(self):
        """Phase 42: shed load — pause LOW/NORMAL tasks, keep CRITICAL/HIGH."""
        paused = []
        for t in self.store.list_tasks(status="RUNNING"):
            prio = self._priority_of(t)
            if prio in ("LOW", "NORMAL"):
                self.store.update_task(t["task_id"], status="PAUSED")
                self.journal("TASK_SHED", task_id=t["task_id"],
                             priority=prio, reason="resource pressure")
                paused.append(t["task_id"])
        return paused

    def _priority_of(self, task):
        try:
            import json as _j
            spec = _j.loads(task.get("spec") or "{}")
            return spec.get("priority", "NORMAL").upper()
        except Exception:
            return "NORMAL"

    def _pending_ordered(self):
        """Phase 51: priority queues with fair scheduling (no starvation:
        PENDING tasks are ordered by priority, then queue time)."""
        pending = self.store.list_tasks(status="PENDING")
        return sorted(pending,
                      key=lambda t: (PRIORITY_ORDER.get(self._priority_of(t), 2),
                                     t.get("started_at") or 0))

    def _budgets_init(self, task_id, spec):
        for kind, limit in (spec.get("budgets") or {}).items():
            self.store.budget_set(task_id, kind, limit)

    def _budget_ok(self, task_id, kind, amount=1):
        ok, used, limit = self.store.budget_consume(task_id, kind, amount)
        if not ok:
            self.journal("TASK_BUDGET_PAUSED", task_id=task_id, kind=kind,
                         used=used, limit=limit)
            self.store.update_task(task_id, status="PAUSED")
            self.store.intervention_open(
                task_id=task_id,
                reason=f"action budget exhausted: {kind} ({used}/{limit})",
                required_action="raise budget or approve continuation",
                last_verified_step=f"step {self._current_step}")
        return ok

    # ---- task engine ----
    def run_task(self, task_id):
        task = self.store.get_task(task_id)
        if not task:
            raise ValueError(f"unknown task {task_id}")
        # Phase 41: safe mode stops dangerous work but preserves state.
        if self.recovery.in_safe_mode():
            self.journal("TASK_DEFERRED_SAFE_MODE", task_id=task_id)
            return "DEFERRED"
        spec = self._spec_of(task)
        steps = spec.get("steps", [])
        # Phase 32: claim the task lease; another live worker owns it otherwise.
        if not self.store.claim_lease(task_id, f"agent:{self.pid}",
                                      f"agent:{self.pid}"):
            self.journal("TASK_LEASE_DENIED", task_id=task_id)
            return "DEFERRED"
        self._budgets_init(task_id, spec)
        self._task_start_ts[task_id] = time.time()
        self._current_task = task_id
        self.store.update_task(task_id, status="RUNNING")
        self.journal("TASK_STARTED", task_id=task_id,
                     total_steps=len(steps),
                     priority=spec.get("priority", "NORMAL"))

        # Resume from checkpoint: skip verified steps.
        start = task["current_step"] or 0
        ckpt = self.store.latest_checkpoint(task_id)
        if ckpt:
            self.journal("TASK_RESUMED", task_id=task_id,
                         from_step=ckpt["step"], label=ckpt["label"])
            start = max(start, ckpt["step"] + 1)

        for idx in range(start, len(steps)):
            if self._shutdown:
                self._release_task(task_id, idx, "PAUSED", "shutdown")
                return "PAUSED"
            if self._cancellation_check(task_id, idx):
                return "CANCELLED"
            # Phase 39: duration budget
            if not self._budget_ok(task_id, "duration_s",
                                   time.time() - self._task_start_ts[task_id]):
                self._release_task(task_id, idx, "PAUSED", "budget")
                return "PAUSED"
            step = steps[idx]
            self._current_step = idx
            # Phase 39: tool-call budget
            if not self._budget_ok(task_id, "tool_calls", 1):
                self._release_task(task_id, idx, "PAUSED", "budget")
                return "PAUSED"
            result = self._run_step(task_id, idx, step, task)
            if result == "INTERRUPTED":
                self._release_task(task_id, idx, "PAUSED", "interrupted")
                return "PAUSED"
            if not result:
                self.store.update_task(task_id, status="FAILED",
                                       current_step=idx)
                self.journal("TASK_FAILED", task_id=task_id, step=idx,
                             name=step.get("name"))
                # Phase 40: escalate recovery for the failed step kind.
                self.recovery.record_failure(task_id, step.get("tool", "?"))
                self.recovery.recover(task_id, step.get("tool", "?"),
                                      detail=step.get("name"), agent=self)
                self.store.release_lease(task_id)
                return "FAILED"
            self.recovery.record_success(task_id, step.get("tool", "?"))
            self.store.heartbeat_lease(task_id, f"agent:{self.pid}")
        self.store.update_task(task_id, status="COMPLETED",
                               current_step=len(steps))
        self.store.release_lease(task_id)
        self.journal("TASK_COMPLETED", task_id=task_id)
        return "COMPLETED"

    def _release_task(self, task_id, idx, status, reason):
        self.store.update_task(task_id, status=status, current_step=idx)
        self.store.release_lease(task_id)
        self.journal("TASK_" + status, task_id=task_id, step=idx,
                     reason=reason)

    def _spec_of(self, task):
        import json
        try:
            return json.loads(task["spec"] or "{}")
        except Exception:
            return {}

    def _reconcile_op(self, step):
        """Phase 31: reconcile an UNKNOWN operation against verified state
        before retrying. Uses the step's idempotent_check as evidence."""
        idem = step.get("idempotent_check")
        if not idem:
            return False, {"reason": "no idempotent_check to reconcile with"}
        v = self.verifier.verify_step({"verify": [idem]})
        return v.passed, {"verdict": v.to_dict()}

    def _run_step(self, task_id, idx, step, task):
        import json
        name = step.get("name", f"step-{idx}")
        self._current_op = name
        self.beat(operation=name)
        self.journal("STEP_STARTED", task_id=task_id, step=idx, name=name)

        # Phase 30/31: idempotent operation registry. If this op already
        # COMPLETED, skip it — no duplicate side effects after a crash.
        op_id = step.get("op_id")
        if op_id:
            run, reason = self.txn.should_run(
                op_id,
                reconcile_fn=(lambda: self._reconcile_op(step)))
            self.journal("OP_DECISION", task_id=task_id, step=idx,
                         op_id=op_id, run=run, reason=reason)
            if not run:
                if "paused" in reason or "ambiguous" in reason:
                    self.store.intervention_open(
                        task_id=task_id,
                        reason=f"ambiguous operation {op_id}: {reason}",
                        required_action="reconcile manually, then resume",
                        last_verified_step=name)
                    return False
                self._checkpoint(task_id, idx, f"op-skip:{name}",
                                 {"op_id": op_id, "reason": reason})
                return True
            self.store.op_set(op_id, task_id, step.get("tool", "shell"),
                              "RUNNING")

        # Idempotency: skip work already done.
        idem = step.get("idempotent_check")
        if idem:
            v = self.verifier.verify_step({"verify": [idem]})
            if v.passed:
                self.journal("STEP_SKIPPED", task_id=task_id, step=idx,
                             name=name, reason="idempotent_check passed")
                self._checkpoint(task_id, idx, f"skip:{name}", {"skipped": True})
                return True

        retries = int(step.get("retries", 1))
        last_err = ""
        for attempt in range(retries + 1):
            if self._shutdown:
                # Interrupted by shutdown, not a failure — let the caller
                # mark the task PAUSED so it resumes on restart.
                self.journal("STEP_INTERRUPTED", task_id=task_id, step=idx,
                             name=name)
                return "INTERRUPTED"
            tool, args = step.get("tool", "shell"), step.get("args", {})
            if tool == "shell":
                allowed, reason = self.policy.authorize(
                    args.get("command", ""))
                self.journal("POLICY_CHECK", task_id=task_id, step=idx,
                             level=self.policy.classify(args.get("command", "")),
                             reason=reason)
                if not allowed:
                    last_err = reason
                    break
            res = self.ex.run(tool, args=args, task_id=task_id)
            verdict = self.verifier.verify_step(step)
            self.store.update_task(
                task_id, last_verified_result=json.dumps(verdict.to_dict()))
            if res.ok and verdict.passed:
                self.journal("STEP_OK", task_id=task_id, step=idx, name=name,
                             attempt=attempt)
                if op_id:
                    self.store.op_set(op_id, task_id, step.get("tool", "shell"),
                                      "COMPLETED",
                                      result={"verdict": verdict.to_dict()})
                self._checkpoint(task_id, idx, name,
                                 {"verdict": verdict.to_dict()})
                self.beat(operation=name, last_action=f"step {idx} ok")
                return True
            last_err = (f"tool_ok={res.ok} verify={verdict.passed} "
                        f"stderr={res.stderr[:200]}")
            self.journal("STEP_RETRY", task_id=task_id, step=idx, name=name,
                         attempt=attempt, error=last_err)
            time.sleep(min(2 ** attempt, 30))
        # record failed step
        try:
            failed = json.loads(task.get("failed_steps") or "[]")
        except Exception:
            failed = []
        failed.append(idx)
        self.store.update_task(task_id, failed_steps=json.dumps(failed),
                               retry_count=(task.get("retry_count") or 0) + 1)
        if op_id:
            self.store.op_set(op_id, task_id, step.get("tool", "shell"),
                              "FAILED", result={"error": last_err})
        self.journal("STEP_FAILED", task_id=task_id, step=idx, name=name,
                     error=last_err)
        return False

    def _checkpoint(self, task_id, step, label, state):
        self.store.checkpoint(task_id, step, label, state)
        self.store.update_task(task_id, current_step=step + 1)
        self.journal("CHECKPOINT_CREATED", task_id=task_id, step=step,
                     label=label)

    # ---- recovery entry ----
    def recover(self):
        """Phase 33: reconcile environment vs saved state BEFORE resuming."""
        ctx = {"base_dir": self.cfg["base_dir"], "task_id": None}
        report = self.recovery.reconcile_on_boot(ctx)
        self.journal("RECOVERY_STARTED", reconcile=report.get("fixed"))
        resumed = []
        for task in self.store.list_tasks():
            if task["status"] in ("RUNNING", "PAUSED"):
                self.journal("TASK_RECOVERY", task_id=task["task_id"],
                             status=task["status"])
                result = self.run_task(task["task_id"])
                resumed.append((task["task_id"], result))
        self.journal("RECOVERY_COMPLETED", resumed=resumed)
        return resumed

    # ---- graceful shutdown (phase 53): 9-step sequence ----
    def graceful_shutdown(self):
        j = self.journal
        j("SHUTDOWN_STARTED", pid=self.pid)
        # 1. stop accepting new work
        self._shutdown = True
        # 2-4. finish safe ops: checkpoint current task, save state
        if self._current_task:
            try:
                self._checkpoint(self._current_task, self._current_step,
                                 "shutdown", {"op": self._current_op})
            except Exception:  # noqa: BLE001 - shutdown must not fail
                pass
            self.store.update_task(self._current_task, status="PAUSED",
                                   current_step=self._current_step)
        # 5. flush logs (journal is append-only; fsync via close)
        # 6. release leases
        if self._current_task:
            self.store.release_lease(self._current_task)
        j("SHUTDOWN_LEASES_RELEASED")
        # 7. close database
        try:
            self.store.close()
        except Exception:  # noqa: BLE001
            pass
        # 8. workers: none persistent in this design (subprocess per tool)
        j("SHUTDOWN_COMPLETE", pid=self.pid)
        # 9. exit cleanly (caller returns from serve_forever)

    # ---- main loop ----
    def serve_forever(self):
        self.install_signal_handlers()
        self.journal("AGENT_STARTED", pid=self.pid)
        # Phase 50: local control interface (localhost + token auth)
        control = None
        try:
            from .remote import ControlServer
            control = ControlServer(self.store, self.cfg,
                                    agent_ref=lambda: self)
            control.start()
        except Exception as e:  # noqa: BLE001 - control is optional
            self.journal("CONTROL_FAILED", error=repr(e))
        self.recover()  # reconcile + resume unfinished work on every start
        while not self._shutdown:
            self.beat()
            # Phase 42: resource pressure check each loop
            snap = resmod.sample()
            level, reasons = resmod.pressure_level(snap)
            if level != "ok":
                resmod.respond(self.store, self.cfg, level, reasons,
                               agent=self)
            # Phase 51: pick up PENDING tasks by priority
            for t in self._pending_ordered():
                self.run_task(t["task_id"])
                if self._shutdown:
                    break
            time.sleep(5)
        if control:
            try:
                control.stop()
            except Exception:  # noqa: BLE001
                pass
        self.graceful_shutdown()


def main():
    cfg = config_mod.load()
    rt = AgentRuntime(cfg)
    rt.serve_forever()


if __name__ == "__main__":
    main()
