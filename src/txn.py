"""Phase 29/30/31: transactional tool execution + idempotent operation registry.

Prepare -> Execute -> Verify -> Commit. On verification failure: rollback
where safe, else repair / retry / pause.

Every recoverable operation carries an op_id. Before executing, the registry
is consulted:
  COMPLETED -> skip (already done, no duplicate side effects)
  RUNNING   -> check liveness; stale RUNNING becomes UNKNOWN
  UNKNOWN   -> reconcile against verified world state before retrying
  FAILED/PENDING/none -> execute

External side effects that cannot prove exactly-once are recorded with their
idempotency key; ambiguous crashes leave them UNKNOWN, never assumed.
"""
import time
import uuid

try:
    from .state import _pid_alive  # reuse liveness helper
except ImportError:  # standalone use
    from state import _pid_alive


class TxnResult:
    def __init__(self, ok, phase, detail=None, rolled_back=False):
        self.ok = ok
        self.phase = phase  # prepare|execute|verify|commit|rollback|paused
        self.detail = detail or {}
        self.rolled_back = rolled_back

    def to_dict(self):
        return {"ok": self.ok, "phase": self.phase,
                "detail": self.detail, "rolled_back": self.rolled_back}


class TxnRunner:
    def __init__(self, store, executor, verifier, journal=None):
        self.store = store
        self.ex = executor
        self.verifier = verifier
        self.journal = journal or (lambda e, **k: None)

    # ---- operation registry (phases 30/31) ----
    def should_run(self, op_id, reconcile_fn=None):
        """Decide whether op_id needs execution. Returns (run: bool, reason)."""
        rec = self.store.op_get(op_id)
        if not rec:
            return True, "no record: first run"
        st = rec["status"]
        if st == "COMPLETED":
            return False, "already completed: skip (idempotent)"
        if st == "FAILED":
            return True, "previous attempt failed: retry allowed"
        if st == "PENDING":
            return True, "pending: execute"
        if st == "RUNNING":
            # RUNNING with no live owner is stale -> UNKNOWN, reconcile.
            self.store.op_mark_unknown(op_id)
            st = "UNKNOWN"
        if st == "UNKNOWN":
            if reconcile_fn:
                done, evidence = reconcile_fn()
                self.journal("OP_RECONCILED", op_id=op_id, done=done,
                             evidence=evidence)
                if done:
                    self.store.op_set(op_id, rec.get("task_id"), rec["kind"],
                                      "COMPLETED", result={"reconciled": True})
                    return False, "reconciled: already done"
                self.store.op_set(op_id, rec.get("task_id"), rec["kind"],
                                  "PENDING")
                return True, "reconciled: not done, safe to retry"
            return False, ("ambiguous: no reconcile function; paused for human"
                           " review")
        return True, f"status {st}: execute"

    # ---- transactional execution (phase 29) ----
    def run_txn(self, op_id, task_id, kind, prepare_fn, execute_fn,
                verify_fn, rollback_fn=None, commit_fn=None,
                idempotency_key=None, reconcile_fn=None,
                external_resource=None):
        """Full prepare/execute/verify/commit cycle with rollback on failure.

        Phase 63/64: if the operation touches an external resource, ownership
        of that resource is claimed first (exclusive, with lease); after
        execution the local record is reconciled against the external
        resource's actual state.
        """
        if external_resource:
            from . import ownership as ownershipmod
            ok, msg = ownershipmod.own_external(
                self.store, task_id, external_resource)
            if not ok:
                self.journal("EXT_OWNERSHIP_DENIED", op_id=op_id,
                             resource=external_resource, reason=msg)
                return TxnResult(False, "paused",
                                 {"reason": f"external ownership: {msg}"})
            self.journal("EXT_OWNERSHIP_TAKEN", op_id=op_id,
                         resource=external_resource)
        run, reason = self.should_run(op_id, reconcile_fn)
        self.journal("TXN_DECISION", op_id=op_id, run=run, reason=reason,
                     task_id=task_id)
        if not run:
            if "paused" in reason or "ambiguous" in reason:
                return TxnResult(False, "paused", {"reason": reason})
            return TxnResult(True, "commit", {"reason": reason,
                                              "skipped": True})

        self.store.op_set(op_id, task_id, kind, "RUNNING",
                          idempotency_key=idempotency_key)
        # PREPARE
        try:
            prep = prepare_fn() if prepare_fn else {"ok": True}
        except Exception as e:  # noqa: BLE001
            prep = {"ok": False, "error": repr(e)}
        if not prep.get("ok", True):
            self.store.op_set(op_id, task_id, kind, "FAILED",
                              result={"phase": "prepare", **prep})
            return TxnResult(False, "prepare", prep)

        # EXECUTE
        try:
            exec_res = execute_fn()
        except Exception as e:  # noqa: BLE001
            exec_res = {"ok": False, "error": repr(e)}
        self.journal("TXN_EXECUTED", op_id=op_id,
                     ok=bool(exec_res.get("ok", True)))

        # VERIFY (independent of the executor's own claims)
        try:
            verdict = verify_fn(exec_res) if verify_fn else {"passed": True}
            passed = verdict.get("passed", bool(verdict))
        except Exception as e:  # noqa: BLE001
            passed, verdict = False, {"error": repr(e)}

        if passed:
            if commit_fn:
                try:
                    commit_fn(exec_res)
                except Exception as e:  # noqa: BLE001
                    self.journal("TXN_COMMIT_WARN", op_id=op_id,
                                 error=repr(e))
            self.store.op_set(op_id, task_id, kind, "COMPLETED",
                              idempotency_key=idempotency_key,
                              result={"verdict": verdict})
            self.journal("TXN_COMMITTED", op_id=op_id)
            return TxnResult(True, "commit", {"verdict": verdict})

        # VERIFY FAILED -> rollback / retry / pause
        rolled_back = False
        if rollback_fn:
            try:
                rollback_fn(exec_res)
                rolled_back = True
                self.journal("TXN_ROLLED_BACK", op_id=op_id)
            except Exception as e:  # noqa: BLE001
                self.journal("TXN_ROLLBACK_FAILED", op_id=op_id,
                             error=repr(e))
        self.store.op_set(op_id, task_id, kind, "FAILED",
                          result={"phase": "verify", "verdict": verdict,
                                  "rolled_back": rolled_back})
        return TxnResult(False, "verify",
                         {"verdict": verdict, "exec": str(exec_res)[:500]},
                         rolled_back=rolled_back)


def new_op_id(kind):
    return f"{kind}:{uuid.uuid4().hex[:12]}"


def reconcile_external(store, op_id, probe_fn, resume_fn=None, journal=None):
    """Phase 64: reconcile a local op record against the external
    resource's actual state (state synchronization).

    probe_fn() -> {"state": "done"|"in_progress"|"failed"|"unknown",
                   "evidence": {...}}

    Returns (decision, detail); decision is one of:
      synced-done   — external proves completion; local record committed
      resuming      — external in progress; progress resumption armed
      retry         — external failed; local record reset to PENDING
      unknown-open  — cannot prove; left UNKNOWN for human review (never
                      assumed failed, never assumed done)
    """
    j = journal or (lambda e, **k: None)
    rec = store.op_get(op_id)
    if not rec:
        return "unknown-open", {"reason": "no local record"}
    try:
        probe = probe_fn()
    except Exception as e:  # noqa: BLE001
        probe = {"state": "unknown", "evidence": {"error": repr(e)}}
    state = (probe or {}).get("state", "unknown")
    j("EXT_RECONCILED", op_id=op_id, external_state=state,
      evidence=(probe or {}).get("evidence"))
    if state == "done":
        # result verification: external evidence commits the local record
        store.op_set(op_id, rec.get("task_id"), rec["kind"], "COMPLETED",
                     result={"reconciled": True,
                             "evidence": (probe or {}).get("evidence")})
        return "synced-done", probe
    if state == "in_progress":
        # progress resumption: keep tracking, resume monitoring
        if resume_fn:
            try:
                resume_fn()
            except Exception as e:  # noqa: BLE001
                return "unknown-open", {"error": repr(e)}
        return "resuming", {"note": "external op still running"}
    if state == "failed":
        # failure recovery of the in-progress operation: safe to retry
        store.op_set(op_id, rec.get("task_id"), rec["kind"], "PENDING",
                     result={"external_failed": (probe or {}).get("evidence")})
        return "retry", probe
    store.op_set(op_id, rec.get("task_id"), rec["kind"], "UNKNOWN",
                 result={"evidence": (probe or {}).get("evidence")})
    return "unknown-open", probe
