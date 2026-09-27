"""Agent runtime: the task engine.

The LLM (or a task spec) proposes steps. This engine:
  1. loads the task + last checkpoint from SQLite (never from memory)
  2. executes each step via the Executor
  3. verifies each step via the Verifier
  4. checkpoints only after verification passes
  5. heartbeats every N seconds with pid/task/step/operation
  6. on failure: retries with backoff, then marks FAILED (supervisor decides)

Task spec format (JSON):
  {"steps": [
     {"name": "install nginx",
      "tool": "shell", "args": {"command": "apt-get install -y nginx"},
      "verify": [{"check": "command_ok", "command": "nginx -v"}],
      "idempotent_check": {"check": "command_ok", "command": "which nginx"},
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
from .state import Store
from .tools import Executor
from .verify import Verifier
from .policy import Policy


class AgentRuntime:
    def __init__(self, cfg):
        self.cfg = cfg
        self.store = Store(cfg["state_db"], cfg["journal_dir"])
        self.journal = self.store.journal
        self.ex = Executor(cfg, journal=self._journal_fn)
        self.verifier = Verifier(self.ex)
        self.policy = Policy()
        self.pid = os.getpid()
        self._shutdown = False
        self._current_task = None
        self._current_step = 0
        self._current_op = "idle"
        self._last_beat = 0

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

    # ---- task engine ----
    def run_task(self, task_id):
        task = self.store.get_task(task_id)
        if not task:
            raise ValueError(f"unknown task {task_id}")
        spec = self._spec_of(task)
        steps = spec.get("steps", [])
        self._current_task = task_id
        self.store.update_task(task_id, status="RUNNING")
        self.journal("TASK_STARTED", task_id=task_id,
                     total_steps=len(steps))

        # Resume from checkpoint: skip verified steps.
        start = task["current_step"] or 0
        ckpt = self.store.latest_checkpoint(task_id)
        if ckpt:
            self.journal("TASK_RESUMED", task_id=task_id,
                         from_step=ckpt["step"], label=ckpt["label"])
            start = max(start, ckpt["step"] + 1)

        for idx in range(start, len(steps)):
            if self._shutdown:
                self.store.update_task(task_id, status="PAUSED",
                                       current_step=idx)
                self.journal("TASK_PAUSED", task_id=task_id, step=idx)
                return "PAUSED"
            step = steps[idx]
            self._current_step = idx
            result = self._run_step(task_id, idx, step, task)
            if result == "INTERRUPTED":
                self.store.update_task(task_id, status="PAUSED",
                                       current_step=idx)
                self.journal("TASK_PAUSED", task_id=task_id, step=idx)
                return "PAUSED"
            if not result:
                self.store.update_task(task_id, status="FAILED",
                                       current_step=idx)
                self.journal("TASK_FAILED", task_id=task_id, step=idx,
                             name=step.get("name"))
                return "FAILED"
        self.store.update_task(task_id, status="COMPLETED",
                               current_step=len(steps))
        self.journal("TASK_COMPLETED", task_id=task_id)
        return "COMPLETED"

    def _spec_of(self, task):
        import json
        try:
            return json.loads(task["spec"] or "{}")
        except Exception:
            return {}

    def _run_step(self, task_id, idx, step, task):
        import json
        name = step.get("name", f"step-{idx}")
        self._current_op = name
        self.beat(operation=name)
        self.journal("STEP_STARTED", task_id=task_id, step=idx, name=name)

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
        """Find RUNNING/PAUSED tasks and resume them from checkpoints."""
        self.journal("RECOVERY_STARTED")
        resumed = []
        for task in self.store.list_tasks():
            if task["status"] in ("RUNNING", "PAUSED"):
                self.journal("TASK_RECOVERY", task_id=task["task_id"],
                             status=task["status"])
                result = self.run_task(task["task_id"])
                resumed.append((task["task_id"], result))
        self.journal("RECOVERY_COMPLETED", resumed=resumed)
        return resumed

    # ---- main loop ----
    def serve_forever(self):
        self.install_signal_handlers()
        self.journal("AGENT_STARTED", pid=self.pid)
        self.recover()  # resume unfinished work on every start
        while not self._shutdown:
            self.beat()
            # pick up newly queued PENDING tasks
            pending = self.store.list_tasks(status="PENDING")
            for t in pending:
                self.run_task(t["task_id"])
                if self._shutdown:
                    break
            time.sleep(5)
        self.journal("AGENT_STOPPED", pid=self.pid)


def main():
    cfg = config_mod.load()
    rt = AgentRuntime(cfg)
    rt.serve_forever()


if __name__ == "__main__":
    main()
