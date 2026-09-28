"""Phases 55/56: recovery history + repeated-failure analysis.

Every recovery attempt is persisted (failure kind, method, result, attempts).
Before attempting recovery, the history is consulted: a recovery method that
already FAILED for the same failure signature is not tried again — a
different strategy is selected, or the failure is escalated.

Repeated failures get a stable fingerprint:
    kind | operation | error-class | task-context
When the same fingerprint keeps failing with the same result, the system
escalates instead of repeating the cycle (phase 56).
"""
import hashlib
import time

from . import classify as classify_mod


def fingerprint(failure_kind, operation, error_text=""):
    """Stable signature for a recurring failure.

    Built from kind + operation + error CLASS (not the raw text), so the
    same failure recurring with different volatile details (pids, timings,
    temp paths) maps to one signature. task_id is deliberately excluded:
    the same failure in different tasks is the same failure.
    """
    err_cls = _error_class(str(error_text or ""))
    raw = f"{failure_kind}|{operation}|{err_cls}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _error_class(text):
    t = text.lower()
    if "timeout" in t or "timed out" in t:
        return "timeout"
    if "connection" in t or "network" in t:
        return "network"
    if "permission" in t or "denied" in t:
        return "permission"
    if "not found" in t or "no such" in t:
        return "missing"
    if "auth" in t or "401" in t or "403" in t:
        return "auth"
    if "crash" in t or "killed" in t or "segfault" in t:
        return "crash"
    if "disk" in t or "no space" in t:
        return "disk"
    return "other"


# failure_kind -> ordered recovery strategies (least destructive first)
STRATEGIES = {
    "step_failed": ["retry_step", "rebuild_plan", "pause_task"],
    "worker_crash": ["restart_worker", "reload_task_state", "safe_mode"],
    "browser_crash": ["restart_browser", "restore_session", "safe_mode"],
    "dep_failed": ["repair_dep", "reinstall_dep", "human"],
    "net_failure": ["backoff_retry", "wait_reconnect", "human"],
    "model_failure": ["retry_model", "fallback_model", "human"],
    "planner_stall": ["rebuild_plan", "human"],
    "progress_stall": ["retry_step", "rebuild_plan", "human"],
    "deadlock": ["release_stale", "restart_worker", "safe_mode"],
    "stale_lock": ["reap_lock", "resume_task"],
    "db_corrupt": ["restore_backup", "human"],
    "config_bad": ["restore_config", "human"],
}


class FailureAnalyzer:
    def __init__(self, store, journal=None):
        self.store = store
        self.journal = journal or store.journal

    def record(self, task_id, failure_kind, operation, method,
               result="ok", level=None, error_text="", detail=None):
        """Persist one recovery attempt (phase 55)."""
        return record(self.store, task_id, failure_kind, operation, method,
                      result=result, level=level, error_text=error_text,
                      detail=detail)

    def choose_strategy(self, task_id, failure_kind, operation,
                        error_text=""):
        """Pick the next strategy, skipping ones that already failed for
        this fingerprint. Returns (method, signature, escalated)."""
        sig = fingerprint(failure_kind, operation, error_text)
        tried_failed = [a["recovery_method"]
                        for a in self.store.recovery_history(signature=sig,
                                                            limit=500)
                        if a["result"] == "failed"]
        for method in STRATEGIES.get(failure_kind, ["retry_step", "human"]):
            if method not in tried_failed:
                return method, sig, False
        # every known strategy already failed for this fingerprint -> escalate
        self.store.fingerprint_escalate(sig)
        self.journal("STRATEGY_EXHAUSTED", task_id=task_id,
                     failure=failure_kind, signature=sig)
        return "human", sig, True

    def repetition_count(self, signature):
        fp = self.store.fingerprint_get(signature)
        return fp["occurrences"] if fp else 0

    def is_escalated(self, signature):
        fp = self.store.fingerprint_get(signature)
        return bool(fp and fp["escalated"])


def record(store, task_id, failure_kind, operation, method,
           result="ok", level=None, error_text="", detail=None):
    """Module-level record: fingerprint + persist one recovery attempt.
    Returns the failure signature."""
    sig = fingerprint(failure_kind, operation, error_text)
    store.recovery_record(task_id, failure_kind, sig, method,
                          level=level, result=result, detail=detail,
                          operation=operation)
    return sig
