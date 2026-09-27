"""Phase 26/51: self-healing dependencies + dependency health manager.

Detect -> Diagnose -> Determine safe repair -> Repair -> Verify -> Resume.

Phase 51: every critical dependency is tracked in a registry with:
  required version, installed version, availability, compatibility,
  health, last verification.

Failures are classified temporary vs permanent. Safe repairs only;
reinstall only when required; every repair attempt is recorded. Never
blindly reinstall the whole environment; never touch a healthy dep.
"""
import os
import re
import shutil
import socket
import subprocess
import time

from . import net as netmod
from . import timecheck


def _which(name):
    return shutil.which(name)


def _run_version(cmd):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        out = (p.stdout or p.stderr or "").strip()
        m = re.search(r"(\d+\.\d+(\.\d+)?)", out)
        return m.group(1) if m else out[:40]
    except (OSError, subprocess.TimeoutExpired):
        return None


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


# ---- Phase 51: dependency registry ----
# name -> {required_version, check_kind, repair_kind}
# repair_kind: safe_reinstall | manual | none
DEP_REGISTRY = {
    "python3": {"required_version": ">=3.8", "repair": "manual",
                "critical": True},
    "node": {"required_version": ">=18", "repair": "manual",
             "critical": False},
    "sqlite3": {"required_version": "any", "repair": "manual",
                "critical": True},
    "disk_writable": {"required_version": "n/a", "repair": "manual",
                      "critical": True},
    "dns": {"required_version": "n/a", "repair": "none",
            "critical": False},
    "env_vars": {"required_version": "n/a", "repair": "manual",
                 "critical": False},
    "clock": {"required_version": "n/a", "repair": "none",
              "critical": True},
}


def dep_status(name, ctx):
    """Full health record for one dependency (phase 51)."""
    entry = DEP_REGISTRY.get(name, {})
    rec = {"name": name,
           "required_version": entry.get("required_version", "any"),
           "installed_version": None,
           "available": False,
           "compatible": None,
           "health": "unknown",
           "last_verification": None,
           "critical": entry.get("critical", False)}
    if name == "python3":
        p = _which("python3")
        rec["available"] = bool(p)
        rec["installed_version"] = _run_version(["python3", "--version"])
    elif name == "node":
        p = _which("node")
        rec["available"] = bool(p)
        rec["installed_version"] = _run_version(["node", "--version"])
    elif name == "sqlite3":
        try:
            import sqlite3
            rec["available"] = True
            rec["installed_version"] = sqlite3.sqlite_version
        except ImportError:
            pass
    elif name == "disk_writable":
        base = ctx.get("base_dir", "/tmp")
        probe = os.path.join(base, ".vm-agent-write-probe")
        try:
            with open(probe, "w") as f:
                f.write("ok")
            os.unlink(probe)
            rec["available"] = True
        except OSError as e:
            rec["detail"] = str(e)
    elif name == "dns":
        try:
            socket.getaddrinfo("example.com", 80, timeout=5)
            rec["available"] = True
        except OSError as e:
            rec["detail"] = str(e)
    elif name == "env_vars":
        missing = [v for v in ctx.get("required_env", [])
                   if not os.environ.get(v)]
        rec["available"] = not missing
        if missing:
            rec["detail"] = f"missing: {missing}"
    elif name == "clock":
        ok, detail = timecheck.check_sync()
        rec["available"] = ok
        rec["detail"] = detail.get("reason", "ok")
    else:
        fn = CHECKS.get(name)
        if fn:
            ok, detail, _ = fn(ctx)
            rec["available"] = ok
            rec["detail"] = detail
    req = rec["required_version"]
    if req in ("any", "n/a") or not rec["installed_version"]:
        rec["compatible"] = rec["available"] or None
    else:
        m = re.match(r">=\s*(\d+)", req)
        if m:
            try:
                rec["compatible"] = (
                    int(rec["installed_version"].split(".")[0])
                    >= int(m.group(1)))
            except (ValueError, IndexError):
                rec["compatible"] = None
        else:
            rec["compatible"] = rec["available"]
    rec["health"] = ("healthy" if rec["available"] and
                     rec["compatible"] not in (False,)
                     else "failed")
    rec["last_verification"] = time.time()
    return rec


def dep_status_all(store, ctx):
    """Check every registered dep; persist health to world state."""
    out = {}
    for name in DEP_REGISTRY:
        rec = dep_status(name, ctx)
        out[name] = rec
        store.world_set(f"deps.{name}", {
            "installed": rec["installed_version"],
            "available": rec["available"],
            "health": rec["health"],
            "last_verification": rec["last_verification"]},
            verifier="deps:registry")
        if rec["health"] == "failed":
            store.journal("DEP_FAILED", dep=name,
                          detail=rec.get("detail"))
    return out


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


def classify_dep_failure(name, detail):
    """Phase 51: temporary vs permanent dependency failure."""
    text = str(detail or "").lower()
    if any(k in text for k in ("timeout", "temporary", "unreachable",
                               "try again", "eai_again")):
        return "temporary"
    if any(k in text for k in ("not installed", "not found", "missing",
                               "no such")):
        return "permanent"  # missing until someone installs it
    if "corrupt" in text or "damaged" in text:
        return "permanent"
    if "version" in text and "mismatch" in text:
        return "permanent"
    return "temporary"  # default: assume transient, verify after repair


def heal(store, ctx, max_repairs=3):
    """Attempt safe repairs for failed deps. Returns (repaired, need_human).

    Phase 51: classifies each failure temporary/permanent, reinstalls only
    when required (a reinstallable package with a safe repair), records
    every repair attempt in the recovery history, and never touches a
    healthy dependency."""
    from . import failure as failuremod
    results = check_all(store, ctx)
    repaired, need_human = [], []
    attempts = 0
    for name, (ok, detail) in results.items():
        if ok:
            continue
        failure_kind = classify_dep_failure(name, (detail or {}).get("issue"))
        if attempts >= max_repairs:
            sig = failuremod.record(store, ctx.get("task_id"), "dep_failed",
                                    name, "budget_exhausted",
                                    result="failed",
                                    error_text=str(detail))
            need_human.append((name, detail, "repair budget exhausted"))
            continue
        spec = SAFE_REPAIRS.get(name, {})
        if not spec.get("safe") or not spec.get("repair"):
            # Unsafe or unknown repair: pause + human intervention, no destruction.
            iid = store.intervention_open(
                task_id=ctx.get("task_id"),
                reason=f"dependency failed ({failure_kind}): {name}: "
                       f"{detail.get('issue')}",
                required_action=spec.get("diagnosis", "manual repair required"),
                last_verified_step="dependency check")
            need_human.append((name, detail, f"intervention #{iid}"))
            store.op_set(f"repair:{name}", ctx.get("task_id"), "dep_repair",
                         "FAILED", result={"reason": "unsafe automatic repair"})
            failuremod.record(store, ctx.get("task_id"), "dep_failed", name,
                              "none_safe", result="failed",
                              error_text=str(detail))
            continue
        attempts += 1
        op_id = f"repair:{name}:{int(time.time())}"
        store.op_set(op_id, ctx.get("task_id"), "dep_repair", "RUNNING")
        try:
            spec["repair"](ctx)  # only runs for known-safe repairs
            ok2, detail2 = CHECKS[name](ctx)
            status = "COMPLETED" if ok2 else "FAILED"
            store.op_set(op_id, ctx.get("task_id"), "dep_repair", status,
                         result=detail2)
            failuremod.record(store, ctx.get("task_id"), "dep_failed", name,
                              "safe_repair",
                              result="ok" if ok2 else "failed",
                              error_text=str(detail2))
            if ok2:
                repaired.append(name)
            else:
                need_human.append((name, detail2, "repair did not verify"))
        except Exception as e:  # noqa: BLE001
            store.op_set(op_id, ctx.get("task_id"), "dep_repair", "FAILED",
                         result={"error": repr(e)})
            failuremod.record(store, ctx.get("task_id"), "dep_failed", name,
                              "safe_repair", result="failed",
                              error_text=repr(e))
            need_human.append((name, detail, f"repair crashed: {e!r}"))
    if repaired:
        store.journal("DEPS_REPAIRED", repaired=repaired)
    return repaired, need_human


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
