"""Phase 26: self-healing dependencies.

Detect -> Diagnose -> Determine safe repair -> Repair -> Verify -> Resume.

Each dependency has a check (observation), a diagnosis, and a repair that is
only attempted when known-safe. Unsafe repairs pause the task and open a
human intervention instead of retrying destructively. Every repair attempt
is recorded in the operations registry.
"""
import os
import shutil
import socket
import time

from . import net as netmod


def _which(name):
    return shutil.which(name)


def _port_open(host, port, timeout=3):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


CHECKS = {}


def check(name):
    def deco(fn):
        CHECKS[name] = fn
        return fn
    return deco


@check("python3")
def _c_python3(ctx):
    p = _which("python3")
    if not p:
        return False, {"issue": "python3 not on PATH"}, None
    return True, {"path": p}, None


@check("node")
def _c_node(ctx):
    # node is optional: missing is fine, only repairs when a task needs it
    p = _which("node")
    if not p:
        return False, {"issue": "node not installed"}, "node"
    return True, {"path": p}, None


@check("disk_writable")
def _c_disk(ctx):
    base = ctx.get("base_dir", "/tmp")
    probe = os.path.join(base, ".vm-agent-write-probe")
    try:
        with open(probe, "w") as f:
            f.write("ok")
        os.unlink(probe)
        return True, {"writable": True}, None
    except OSError as e:
        return False, {"issue": f"state dir not writable: {e}"}, None


@check("dns")
def _c_dns(ctx):
    try:
        socket.getaddrinfo("example.com", 80, timeout=5)
        return True, {"dns": "ok"}, None
    except OSError as e:
        kind = netmod.classify(str(e))
        return False, {"issue": f"dns failure: {e}", "kind": kind}, None


@check("env_vars")
def _c_env(ctx):
    missing = [v for v in ctx.get("required_env", []) if not os.environ.get(v)]
    if missing:
        return False, {"issue": f"missing env vars: {missing}"}, None
    return True, {}, None


# Repairs known to be safe. Anything else -> intervention, not destructive retry.
SAFE_REPAIRS = {
    "node": {
        "diagnosis": "node missing; tasks needing node cannot run",
        "repair": None,  # installing node is environment-specific: escalate
        "safe": False,
    },
}


def check_all(store, ctx):
    """Run every dependency check. Returns {name: (ok, detail)}."""
    results = {}
    for name, fn in CHECKS.items():
        try:
            ok, detail, _repair_key = fn(ctx)
        except Exception as e:  # noqa: BLE001 - a check must not crash
            ok, detail = False, {"issue": f"check crashed: {e!r}"}
        results[name] = (ok, detail)
        store.world_set(f"deps.{name}.ok", ok, verifier=f"deps:{name}")
        if not ok:
            store.journal("DEP_FAILED", dep=name, detail=detail)
    return results


def heal(store, ctx, max_repairs=3):
    """Attempt safe repairs for failed deps. Returns (repaired, need_human)."""
    results = check_all(store, ctx)
    repaired, need_human = [], []
    attempts = 0
    for name, (ok, detail) in results.items():
        if ok:
            continue
        if attempts >= max_repairs:
            need_human.append((name, detail, "repair budget exhausted"))
            continue
        key = detail.get("repair_key") if isinstance(detail, dict) else None
        spec = SAFE_REPAIRS.get(name, {})
        if not spec.get("safe") or not spec.get("repair"):
            # Unsafe or unknown repair: pause + human intervention, no destruction.
            iid = store.intervention_open(
                task_id=ctx.get("task_id"),
                reason=f"dependency failed: {name}: {detail.get('issue')}",
                required_action=spec.get("diagnosis", "manual repair required"),
                last_verified_step="dependency check")
            need_human.append((name, detail, f"intervention #{iid}"))
            store.op_set(f"repair:{name}", ctx.get("task_id"), "dep_repair",
                         "FAILED", result={"reason": "unsafe automatic repair"})
            continue
        attempts += 1
        op_id = f"repair:{name}:{int(time.time())}"
        store.op_set(op_id, ctx.get("task_id"), "dep_repair", "RUNNING")
        try:
            spec["repair"](ctx)
            ok2, detail2 = CHECKS[name](ctx)
            status = "COMPLETED" if ok2 else "FAILED"
            store.op_set(op_id, ctx.get("task_id"), "dep_repair", status,
                         result=detail2)
            if ok2:
                repaired.append(name)
            else:
                need_human.append((name, detail2, "repair did not verify"))
        except Exception as e:  # noqa: BLE001
            store.op_set(op_id, ctx.get("task_id"), "dep_repair", "FAILED",
                         result={"error": repr(e)})
            need_human.append((name, detail, f"repair crashed: {e!r}"))
    if repaired:
        store.journal("DEPS_REPAIRED", repaired=repaired)
    return repaired, need_human
