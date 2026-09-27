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

# Phase 3 reliability layer (51-90)
from . import caps as capsmod
from . import classify as classifymod
from . import failure as failuremod
from . import ownership as ownershipmod
from . import planner as plannermod
from . import progress as progressmod
from . import secrets as secretsmod
from . import timecheck as timecheckmod

PRIORITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "NORMAL": 2, "LOW": 3}

# Phase 65: preemption caps — a preempted task can be preempted at most this
# many times before it refuses further preemption (prevents starvation).
MAX_PREEMPTIONS = 2

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

    # ---- remote-control / lifecycle ops (phases 50/52/66) ----
    def request_pause(self, task_id):
        """Phase 66: pause that verifies the pause took effect.

        Sets PAUSED, then confirms within a deadline that the task is
        actually not RUNNING anymore. Returns a verified result dict."""
        task = self.store.get_task(task_id)
        if not task or task["status"] not in ("RUNNING", "PENDING"):
            return {"paused": False,
                    "reason": f"status={task['status'] if task else 'missing'}"}
        self.store.update_task(task_id, status="PAUSED")
        self.journal("TASK_PAUSE_REQUESTED", task_id=task_id)
        # Verify: the task must leave RUNNING within the deadline. The run
        # loop only transitions at step boundaries, so we allow time.
        deadline = time.time() + self.cfg.get("pause_verify_s", 30)
        while time.time() < deadline:
            t = self.store.get_task(task_id)
            if t["status"] != "RUNNING":
                self.journal("TASK_PAUSE_VERIFIED", task_id=task_id,
                             status=t["status"])
                return {"paused": True, "status": t["status"]}
            time.sleep(0.5)
        self.journal("TASK_PAUSE_UNVERIFIED", task_id=task_id)
        return {"paused": False,
                "reason": "pause not verified within deadline"}

    def request_resume(self, task_id):
        """Phase 66: resume that verifies state restoration.

        Checks the snapshot chain is intact (phase 67) before queueing, then
        confirms the task left PAUSED."""
        task = self.store.get_task(task_id)
        if not task or task["status"] not in ("PAUSED", "FAILED"):
            return {"resumed": False,
                    "reason": f"status={task['status'] if task else 'missing'}"}
        # Verify snapshot integrity before resuming (phase 67).
        ckpt = self.store.latest_checkpoint(task_id)
        if ckpt:
            ok, issue = self._verify_snapshot_chain(task_id)
            if not ok:
                self.journal("TASK_RESUME_REFUSED", task_id=task_id,
                             issue=issue)
                return {"resumed": False,
                        "reason": f"snapshot chain broken: {issue}"}
        self.store.update_task(task_id, status="PENDING")
        self.journal("TASK_RESUME_REQUESTED", task_id=task_id)
        t = self.store.get_task(task_id)
        verified = t["status"] == "PENDING"
        self.journal("TASK_RESUME_VERIFIED" if verified
                     else "TASK_RESUME_UNVERIFIED", task_id=task_id,
                     status=t["status"])
        return {"resumed": verified, "status": t["status"]}

    def _verify_snapshot_chain(self, task_id):
        """Phase 67: the snapshot chain must be intact to resume."""
        ckpts = self.store.checkpoints(task_id)
        if not ckpts:
            return True, "no snapshots yet"
        for c in ckpts:
            try:
                import json as _j
                _j.loads(c.get("state_json") or c.get("state") or "{}")
            except Exception:
                return False, f"snapshot {c['id']} unparsable"
        steps = [c["step"] for c in ckpts]
        if steps != sorted(steps):
            return False, "snapshot steps out of order"
        return True, "chain intact"

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

    def _pause_check(self, task_id, idx):
        """Honor an external pause request at the step boundary."""
        t = self.store.get_task(task_id)
        if t and t["status"] == "PAUSED":
            self._checkpoint(task_id, idx, "pause",
                             {"paused_at_step": idx})
            self.store.release_lease(task_id)
            self.journal("TASK_PAUSED", task_id=task_id, step=idx,
                         verified=True)
            return True
        return False

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
        """Phase 69/77: deterministic prioritization + recovery priority.

        Order: tasks awaiting recovery (FAILED with open interventions) go
        first — recovery gets priority over normal work (phase 77). Then
        priority class, then queue time, then task_id for a total order
        (phase 69: same input always yields the same schedule)."""
        recovery_first = set()
        for t in self.store.list_tasks(status="PENDING"):
            if self.store.task_recoveries(t["task_id"], limit=1):
                recovery_first.add(t["task_id"])
        pending = self.store.list_tasks(status="PENDING")
        return sorted(
            pending,
            key=lambda t: (
                0 if t["task_id"] in recovery_first else 1,
                PRIORITY_ORDER.get(self._priority_of(t), 2),
                t.get("started_at") or 0,
                t["task_id"]))

    def _should_preempt(self, task_id):
        """Phase 65: check whether the running task must yield to a
        higher-priority arrival. State (checkpoint) is preserved; the
        preempted task is re-queued as PENDING (phase 64)."""
        me = self.store.get_task(task_id)
        if not me:
            return False
        my_prio = PRIORITY_ORDER.get(self._priority_of(me), 2)
        preempted = self.store.world_get(f"preempted.{task_id}") or {}
        if (preempted.get("count", 0) >= MAX_PREEMPTIONS):
            return False  # starvation guard
        for t in self._pending_ordered():
            if t["task_id"] == task_id:
                continue
            tp = PRIORITY_ORDER.get(self._priority_of(t), 2)
            if tp < my_prio and self._priority_of(t) == "CRITICAL":
                return True
        return False

    def _preempt(self, task_id, idx):
        """Checkpoint, release, and re-queue as PENDING for the CRITICAL task."""
        self._checkpoint(task_id, idx, "preempt",
                         {"preempted_at_step": idx})
        preempted = self.store.world_get(f"preempted.{task_id}") or {}
        count = preempted.get("count", 0) + 1
        self.store.world_set(f"preempted.{task_id}", {"count": count},
                             verifier="agent:preemption")
        self.store.update_task(task_id, status="PENDING", current_step=idx)
        self.store.release_lease(task_id)
        self.journal("TASK_PREEMPTED", task_id=task_id, step=idx,
                     preemption=count, max=MAX_PREEMPTIONS)
        self._cancel_requested.discard(task_id)

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
        # Phase 59: grant this task exactly the tools its spec uses —
        # anything else is denied at execution time.
        capsmod.grant_all_basic(
            self.store, task_id, {s.get("tool", "shell") for s in steps})
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

        idx = start
        while idx < len(steps):
            if self._shutdown:
                self._release_task(task_id, idx, "PAUSED", "shutdown")
                return "PAUSED"
            if self._cancellation_check(task_id, idx):
                return "CANCELLED"
            if self._pause_check(task_id, idx):
                return "PAUSED"
            # Phase 65: priority preemption — a CRITICAL arrival preempts at
            # the step boundary; state is checkpointed, task re-queued.
            if self._should_preempt(task_id):
                self._preempt(task_id, idx)
                return "PREEMPTED"
            # Phase 39/68: duration budget
            if not self._budget_ok(task_id, "duration_s",
                                   time.time() - self._task_start_ts[task_id]):
                self._release_task(task_id, idx, "PAUSED", "budget")
                return "PAUSED"
            # Phase 68: memory pressure — pause this task if it exceeds its
            # memory budget (bytes RSS sampled per step).
            mem_limit = (spec.get("budgets") or {}).get("memory_mb")
            if mem_limit:
                rss_mb = resmod.process_rss_mb()
                if rss_mb is not None and rss_mb > mem_limit:
                    self.journal("TASK_BUDGET_PAUSED", task_id=task_id,
                                 kind="memory_mb", used=rss_mb,
                                 limit=mem_limit)
                    self._release_task(task_id, idx, "PAUSED",
                                       "memory budget")
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
            if result == "REPLANNED":
                # Phase 52: the plan was rebuilt from verified world state —
                # reload the spec and re-run this step under the new plan.
                task = self.store.get_task(task_id)
                spec = self._spec_of(task)
                steps = spec.get("steps", [])
                self.journal("TASK_REPLANNED", task_id=task_id, step=idx,
                             total_steps=len(steps))
                continue
            if not result:
                self.store.update_task(task_id, status="FAILED",
                                       current_step=idx)
                self.journal("TASK_FAILED", task_id=task_id, step=idx,
                             name=step.get("name"))
                # Phase 40: escalate recovery for the failed step kind.
                # The kind is a matrix failure kind ("step_failed"), not the
                # tool name, so the recovery matrix actually applies.
                self.recovery.record_failure(task_id, "step_failed")
                self.recovery.recover(task_id, "step_failed",
                                      detail=f"{step.get('tool', '?')}:"
                                             f"{step.get('name')}",
                                      agent=self,
                                      operation=step.get("name", ""),
                                      error_text=getattr(
                                          self, "_last_step_error", ""))
                self.store.release_lease(task_id)
                return "FAILED"
            self.recovery.record_success(task_id, step.get("tool", "?"))
            self.store.heartbeat_lease(task_id, f"agent:{self.pid}")
            idx += 1
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

    def _classify_error(self, tool, err_text):
        """Phase 54: classify a step error deterministically, so the retry
        loop is governed by the failure kind, not a fixed retry count."""
        from . import model as modelmod
        from . import net as netmod
        text = err_text or ""
        net_kind = netmod.classify(text)
        if net_kind != "other":
            return classifymod.classify("net", net_kind)
        if "malformed" in text.lower() or "schema" in text.lower():
            return classifymod.classify("model", "malformed_response")
        if tool == "browser":
            return classifymod.classify("browser", "page_timeout")
        return classifymod.classify("tool", "exit_nonzero")

    def _run_step(self, task_id, idx, step, task):
        import json
        name = step.get("name", f"step-{idx}")
        self._current_op = name
        self.beat(operation=name)
        self.journal("STEP_STARTED", task_id=task_id, step=idx, name=name)
        tool, args = step.get("tool", "shell"), step.get("args", {})

        # Phase 59: capability check BEFORE execution. A tool the task was
        # not granted cannot run even if the command shape is policy-safe.
        cap_ok, cap_reason = capsmod.check_tool(self.store, task_id, tool)
        if not cap_ok:
            self.journal("CAPABILITY_DENIED", task_id=task_id, step=idx,
                         tool=tool, reason=cap_reason)
            self.store.intervention_open(
                task_id=task_id,
                reason=f"capability denied for tool '{tool}': {cap_reason}",
                required_action="grant capability via policy or remove step",
                last_verified_step=name)
            return False

        # Phase 60: never let raw secrets into the journal/traces.
        args = self._resolve_vault_refs(task_id, tool, args)

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

        # Phase 54: classification-governed retry loop. Some failures must
        # never be retried; some need exactly N attempts with a specific
        # backoff. The classification comes from the error, not the spec.
        last_err = ""
        attempt = 0
        while True:
            if self._shutdown:
                # Interrupted by shutdown, not a failure — let the caller
                # mark the task PAUSED so it resumes on restart.
                self.journal("STEP_INTERRUPTED", task_id=task_id, step=idx,
                             name=name)
                return "INTERRUPTED"
            if tool == "shell":
                allowed, reason = self.policy.authorize(
                    args.get("command", ""))
                self.journal("POLICY_CHECK", task_id=task_id, step=idx,
                             level=self.policy.classify(args.get("command", "")),
                             reason=reason)
                if not allowed:
                    last_err = f"policy blocked: {reason}"
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
                # Phase 53: observable progress — the world changes on success.
                self.store.world_set("progress.step", idx,
                                     verifier="agent:progress")
                self.store.world_set("progress.last_ok", name,
                                     verifier="agent:progress")
                self._checkpoint(task_id, idx, name,
                                 {"verdict": verdict.to_dict()})
                self.beat(operation=name, last_action=f"step {idx} ok")
                self._planner_note_success(task_id)
                return True
            last_err = (f"tool_ok={res.ok} verify={verdict.passed} "
                        f"stderr={secretsmod.redact_text(res.stderr[:200])}")
            decision, max_retries, base_backoff, c_reason = \
                self._classify_error(tool, last_err)
            self.journal("STEP_CLASSIFICATION", task_id=task_id, step=idx,
                         name=name, decision=decision, reason=c_reason)
            if decision in (classifymod.PERMANENT,
                            classifymod.POLICY_BLOCKED,
                            classifymod.HUMAN_REQUIRED):
                # Never retry these — refuse or escalate instead of looping.
                break
            if attempt >= max_retries:
                break
            self.journal("STEP_RETRY", task_id=task_id, step=idx, name=name,
                         attempt=attempt, decision=decision,
                         error=last_err)
            time.sleep(min(base_backoff * (2 ** attempt), 30))
            attempt += 1

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
        # Stash for run_task's recovery escalation (matrix fingerprinting).
        self._last_step_error = last_err
        # Phase 55/56: fingerprint + history for this failure.
        failuremod.record(self.store, task_id, "step_failed", name,
                          "retry_step", result="failed",
                          error_text=last_err)
        # Phase 53: no observable change on failure — check for a stall.
        stalled, count = progressmod.track_stall(self.store, task_id)
        if stalled:
            self.journal("TASK_STALLED", task_id=task_id, step=idx,
                         unchanged_cycles=count)
            self.store.update_task(task_id, status="STALLED")
        # Phase 52: planner recovery on a stalled plan.
        if self._maybe_recover_plan(task_id, idx, step, task):
            return "REPLANNED"
        return False

    def _resolve_vault_refs(self, task_id, tool, args):
        """Phase 60: resolve {"vault": "<secret-name>"} args to the raw
        value at execution time only — raw values never persist in step
        specs, checkpoints, or the journal."""
        if not isinstance(args, dict):
            return args
        resolved = dict(args)
        for k, v in args.items():
            if isinstance(v, dict) and "vault" in v and len(v) == 1:
                name = v["vault"]
                val = secretsmod.vault_get(self.store, task_id, name)
                if val is None:
                    raise PermissionError(
                        f"vault secret '{name}' not granted to task {task_id}")
                resolved[k] = val
                self.journal("VAULT_REF_RESOLVED", task_id=task_id,
                             secret=name)
        return resolved

    def _planner_note_success(self, task_id):
        self.store.world_set(f"planner.fail_streak.{task_id}",
                             {"count": 0}, verifier="agent:planner")

    def _maybe_recover_plan(self, task_id, idx, step, task):
        """Phase 52: on a stalled plan, diagnose, rebuild from verified
        world state, validate the new plan, and let the caller resume it."""
        streak = self.store.world_get(f"planner.fail_streak.{task_id}") or {}
        count = streak.get("count", 0) + 1
        self.store.world_set(f"planner.fail_streak.{task_id}",
                             {"count": count, "step": idx},
                             verifier="agent:planner")
        if count < 3:
            return False
        spec = self._spec_of(task)
        steps = spec.get("steps", [])
        history = self.store.task_recoveries(task_id, limit=5)
        diagnosis = plannermod.diagnose_stall({
            "steps": steps, "step_index": idx,
            "history_kinds": [r["failure_kind"] for r in history]})
        new_steps, plan_reason = plannermod.rebuild_plan(
            task_id, steps, idx, reason=f"plan stall: {diagnosis}")
        valid, issues = plannermod.validate_plan(new_steps)
        self.journal("PLANNER_RECOVERY", task_id=task_id, diagnosis=diagnosis,
                     reason=plan_reason, valid=valid, issues=issues)
        if not valid:
            self.store.intervention_open(
                task_id=task_id,
                reason=f"plan stalled and rebuilt plan invalid: {issues}",
                required_action="human review of plan",
                last_verified_step=step.get("name"))
            return False
        spec["steps"] = new_steps
        import json as _j
        self.store.update_task(task_id, spec=_j.dumps(spec))
        self.store.world_set(f"planner.fail_streak.{task_id}",
                             {"count": 0, "step": idx},
                             verifier="agent:planner")
        return True

    def dry_run(self, task_id):
        """Phase 84: simulate the task without executing anything.

        Validates every step (action schema, policy, capabilities, budget
        feasibility) and returns a per-step report. No tool is executed,
        no state is mutated."""
        from . import model as modelmod
        task = self.store.get_task(task_id)
        if not task:
            return {"ok": False, "error": f"unknown task {task_id}"}
        spec = self._spec_of(task)
        steps = spec.get("steps", [])
        report = {"task_id": task_id, "steps": [], "ok": True,
                  "would_execute": 0, "would_skip": 0, "blockers": []}
        tool_calls = 0
        for idx, step in enumerate(steps):
            name = step.get("name", f"step-{idx}")
            tool, args = step.get("tool", "shell"), step.get("args", {})
            entry = {"step": idx, "name": name, "tool": tool,
                     "verdicts": []}
            ok_v, errors = modelmod.validate_action(
                {"tool": tool, "args": args})
            entry["verdicts"].append(
                ("action_valid", ok_v, "; ".join(errors)))
            if tool == "shell":
                level = self.policy.classify(args.get("command", ""))
                allowed, reason = self.policy.authorize(
                    args.get("command", ""))
                entry["verdicts"].append(
                    ("policy", allowed, f"{level}: {reason}"))
                if not allowed:
                    report["blockers"].append(f"step {idx}: {reason}")
            cap_ok, cap_reason = capsmod.check_tool(self.store, task_id, tool)
            entry["verdicts"].append(
                ("capability", cap_ok, cap_reason))
            if not cap_ok:
                report["blockers"].append(f"step {idx}: {cap_reason}")
            n_verify = len(step.get("verify") or [])
            entry["verdicts"].append(
                ("verification_rules", n_verify > 0,
                 f"{n_verify} rule(s)" if n_verify
                 else "no verification rules"))
            tool_calls += 1
            entry["ok"] = all(v[1] for v in entry["verdicts"])
            if not entry["ok"]:
                report["ok"] = False
            report["steps"].append(entry)
        # budget feasibility
        budgets = spec.get("budgets") or {}
        tc_limit = budgets.get("tool_calls")
        if tc_limit is not None and tool_calls > tc_limit:
            report["blockers"].append(
                f"tool_calls budget {tc_limit} < {tool_calls} steps")
            report["ok"] = False
        self.journal("TASK_DRY_RUN", task_id=task_id, ok=report["ok"],
                     blockers=len(report["blockers"]))
        return report

    def _checkpoint(self, task_id, step, label, state):
        self.store.checkpoint(task_id, step, label, state)
        # Phase 67: resumable snapshots — a content-hashed snapshot of task
        # state so resume can verify integrity, not just trust the DB.
        import hashlib
        import json as _j
        blob = _j.dumps({"task_id": task_id, "step": step, "label": label,
                         "state": state}, sort_keys=True)
        self.store.snapshot_save(
            task_id, f"snap:{task_id}:{step}:{label}", blob,
            content_sha256=hashlib.sha256(blob.encode()).hexdigest())
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
