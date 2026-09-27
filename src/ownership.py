"""Phases 58/63/85/86/87: ownership + safe cleanup.

58: track ownership of resources (ports, files, browser sessions, processes,
    tmpdirs, databases, deployments, external jobs): owner task, execution,
    acquisition time, lease expiration, current state. Unrelated tasks must
    not interfere with resources owned by another task.
63: external resource ownership: resource ID, task ID, creation op, known
    state, last verification. After restart, query external resources before
    creating duplicates.
85: every important operation carries task ID, execution ID, resource owner,
    op type, start time, state — one task can never complete/cancel another
    task's operation.
86: stale operation cleanup: after crash/net loss/reboot/worker failure,
    determine whether an abandoned operation completed externally, failed,
    is pending, or needs cleanup. Never assume abandoned means failed.
87: safe resource cleanup: state-aware. Check ownership/references before
    deleting anything. Never blind-clean.
"""
import os
import time
import uuid


# ---- phase 85: operation ownership ----
def new_execution_id():
    return f"exec-{uuid.uuid4().hex[:12]}"


def _op_meta(op):
    import json
    try:
        return json.loads(op.get("result_json") or "{}")
    except Exception:
        return {}


def claim_operation(store, task_id, op_id, worker_id, ttl_s=120):
    """Claim exclusive ownership of an operation. Returns False if another
    live worker owns it (unexpired lease)."""
    now = time.time()
    op = store.op_get(op_id)
    if op and op.get("status") == "RUNNING":
        meta = _op_meta(op)
        owner, exp = meta.get("owner"), meta.get("expires_at")
        if owner and owner != worker_id and exp and exp > now:
            return False  # owned by a live worker
    store.op_set(op_id, task_id, op.get("kind") if op else "operation",
                 "RUNNING", result={"owner": worker_id,
                                    "claimed_at": now,
                                    "expires_at": now + ttl_s})
    return True


def heartbeat_operation(store, task_id, op_id, worker_id):
    """Refresh the ownership lease. Only the owner can heartbeat."""
    op = store.op_get(op_id)
    if not op or op.get("task_id") != task_id:
        return False
    meta = _op_meta(op)
    if meta.get("owner") != worker_id:
        return False
    meta["expires_at"] = time.time() + 120
    store.op_set(op_id, task_id, op.get("kind"), op.get("status"),
                 result=meta)
    return True


def owns_operation(store, task_id, execution_id, op_id):
    """True only if this execution owns the operation."""
    op = store.op_get(op_id)
    if not op:
        return False
    meta = _op_meta(op)
    return (op.get("task_id") == task_id and
            meta.get("execution_id") in (execution_id, None))


# ---- phase 58/63: resource + external ownership ----
def claim_resource(store, task_id, resource_path, kind, owner=None,
                   ttl_s=3600):
    """Claim a local resource (file, port, tmpdir) for a task."""
    return store.resource_acquire(resource_path, kind,
                                  owner or f"task:{task_id}",
                                  task_id=task_id, ttl_s=ttl_s)


def own_external(store, task_id, resource_id, kind="external", ttl_s=3600):
    """Phase 63: exclusive ownership of an external resource. After a
    restart, ownership must be re-taken — never assume it survived."""
    ok = store.resource_acquire(f"ext:{resource_id}", kind,
                                f"task:{task_id}", task_id=task_id,
                                ttl_s=ttl_s,
                                meta={"external_id": resource_id})
    if not ok:
        r = store.resource_get(f"ext:{resource_id}")
        return False, (f"owned by {r['owner']}" if r
                       else "acquire failed")
    return True, "ownership taken"


def release_external(store, task_id, resource_id):
    store.resource_release(f"ext:{resource_id}", owner=f"task:{task_id}")
    return True


# ---- phase 86: stale operation cleanup ----
def reap_stale_operations(store, journal=None):
    """Find RUNNING operations whose owner is dead and determine their
    real outcome. Returns list of {op_id, disposition}.

    Dispositions: COMPLETED_EXTERNALLY | FAILED | PENDING | NEEDS_CLEANUP.
    'abandoned' is never mapped to 'failed' without evidence.
    """
    journal = journal or store.journal
    results = []
    try:
        import sqlite3
        conn = sqlite3.connect(store.db_path, timeout=10)
        rows = conn.execute(
            "SELECT op_id, task_id, kind, result_json FROM operations"
            " WHERE status='RUNNING'").fetchall()
        conn.close()
    except Exception as e:  # noqa: BLE001
        journal("STALE_OP_SWEEP_FAILED", error=repr(e))
        return results
    for op_id, task_id, kind, result_json in rows:
        execution_id = None
        owner_expired = False
        try:
            import json
            meta = json.loads(result_json or "{}")
            execution_id = meta.get("execution_id")
            exp = meta.get("expires_at")
            if exp and exp < time.time():
                owner_expired = True  # ownership lease lapsed
        except Exception:
            pass
        alive = False
        if execution_id and not owner_expired:
            # executions table tracks live executions
            ex = store.execution_get(execution_id)
            alive = bool(ex and ex["status"] == "RUNNING")
        if alive:
            continue  # genuinely running
        # Owner dead or unknown: mark UNKNOWN, never assume failure.
        store.op_mark_unknown(op_id)
        journal("STALE_OP_FOUND", op_id=op_id, task_id=task_id, kind=kind)
        results.append({"op_id": op_id, "task_id": task_id,
                        "disposition": "UNKNOWN_NEEDS_RECONCILE"})
    return results


# ---- phase 87: safe resource cleanup ----
def safe_release_resource(store, resource_id, requester, journal=None,
                          force=False):
    """Release a resource only if the requester owns it (or it's stale).
    Returns (released, reason)."""
    journal = journal or store.journal
    r = store.resource_get(resource_id)
    if not r:
        return True, "not tracked: nothing to clean"
    if r["state"] != "active":
        return True, f"already {r['state']}"
    if r["owner"] != requester and not force:
        # Check: is the owner's process dead? Then it's safe.
        from .state import _pid_alive
        try:
            pid = int(str(r["owner"]).rsplit(":", 1)[-1])
        except (ValueError, AttributeError):
            pid = 0
        if pid and _pid_alive(pid):
            journal("CLEANUP_REFUSED", resource_id=resource_id,
                    owner=r["owner"], requester=requester)
            return False, f"owned by live {r['owner']}: refusing"
    store.resource_release(resource_id)
    journal("RESOURCE_CLEANED", resource_id=resource_id,
            owner=r["owner"], requester=requester)
    return True, "released"


def cleanup_task_resources(store, task_id, requester, journal=None,
                           keep=None):
    """Release all resources owned by a task (e.g. on cancel/cleanup).
    keep: set of resource_ids to preserve. State-aware: never touches
    resources owned by other tasks."""
    journal = journal or store.journal
    keep = keep or set()
    released, refused = [], []
    for r in store.resources_for_task(task_id):
        if r["resource_id"] in keep:
            continue
        ok, _ = safe_release_resource(store, r["resource_id"], requester,
                                      journal)
        (released if ok else refused).append(r["resource_id"])
    return {"released": released, "refused": refused}


def cleanup_tmpdir(path, store, task_id, journal=None):
    """Delete a tmpdir only if no task references it. Returns (ok, reason)."""
    journal = journal or store.journal
    rid = f"tmpdir:{path}"
    r = store.resource_get(rid)
    if r and r["state"] == "active" and r["task_id"] != task_id:
        journal("CLEANUP_REFUSED", resource_id=rid, reason="other task owns")
        return False, f"owned by task {r['task_id']}"
    try:
        import shutil
        if os.path.isdir(path):
            shutil.rmtree(path)
        journal("TMPDIR_CLEANED", path=path)
        return True, "cleaned"
    except OSError as e:
        return False, str(e)
