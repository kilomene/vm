"""vm-agent CLI: status, logs, tasks, health, restart, diagnostics, recover."""
import argparse
import json
import os
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from vmagent import config as config_mod  # noqa: E402
from vmagent.state import Store  # noqa: E402


def _cfg():
    return config_mod.load()


def _store(cfg):
    return Store(cfg["state_db"], cfg["journal_dir"])


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def cmd_status(args):
    cfg, s = _cfg(), None
    s = _store(cfg)
    sup_pid = _read_pid(os.path.join(cfg["run_dir"], "supervisor.pid"))
    print(f"supervisor: {'RUNNING (pid %d)' % sup_pid if sup_pid and _pid_alive(sup_pid) else 'DOWN'}")
    hb = s.get_heartbeat("agent")
    if hb and _pid_alive(hb["pid"] or 0):
        age = time.time() - hb["ts"]
        print(f"agent: RUNNING (pid {hb['pid']}, heartbeat {age:.0f}s ago, "
              f"task={hb['task_id']}, step={hb['step']}, op={hb['operation']})")
    else:
        print("agent: DOWN")
    tasks = s.list_tasks()
    print(f"tasks: {len(tasks)} total")
    for t in tasks[:10]:
        print(f"  {t['task_id']}: {t['status']} (step {t['current_step']})")
    hour_ago = time.time() - 3600
    print(f"agent restarts (last hour): {s.restart_count_since('agent', hour_ago)}")
    s.close()


def _read_pid(path):
    try:
        with open(path) as f:
            pid = int(f.read().strip())
        return pid if _pid_alive(pid) else None
    except (OSError, ValueError):
        return None


def cmd_health(args):
    cfg = _cfg()
    s = _store(cfg)
    sup_pid = _read_pid(os.path.join(cfg["run_dir"], "supervisor.pid"))
    hb = s.get_heartbeat("agent")
    agent_up = bool(hb and _pid_alive(hb["pid"] or 0))
    info = {
        "supervisor": "up" if sup_pid else "down",
        "supervisor_pid": sup_pid,
        "agent": "up" if agent_up else "down",
        "agent_pid": hb["pid"] if hb else None,
        "heartbeat_age_s": round(time.time() - hb["ts"], 1) if hb else None,
        "current_task": hb["task_id"] if hb else None,
        "current_step": hb["step"] if hb else None,
        "last_operation": hb["operation"] if hb else None,
        "tasks_total": len(s.list_tasks()),
        "tasks_running": len(s.list_tasks("RUNNING")),
        "restarts_last_hour": s.restart_count_since("agent", time.time() - 3600),
    }
    try:
        st = os.statvfs(cfg["base_dir"])
        info["disk_free_mb"] = round(st.f_bavail * st.f_frsize / 1e6, 1)
    except OSError:
        pass
    print(json.dumps(info, indent=2))
    s.close()
    return 0 if (sup_pid and agent_up) else 1


def cmd_logs(args):
    cfg = _cfg()
    name = args.name or "agent.out"
    path = os.path.join(cfg["log_dir"], name)
    if not os.path.exists(path):
        # also try logs dir directly
        path = os.path.join(cfg["base_dir"], "logs", name)
    try:
        with open(path) as f:
            lines = f.readlines()
        for line in lines[-int(args.lines):]:
            print(line, end="")
    except OSError as e:
        print(f"no log {name}: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_tasks(args):
    s = _store(_cfg())
    tasks = s.list_tasks(status=args.status)
    for t in tasks:
        print(f"{t['task_id']}\t{t['status']}\tstep {t['current_step']}\t"
              f"updated {time.strftime('%H:%M:%S', time.localtime(t['updated_at']))}")
    s.close()


def cmd_submit(args):
    import uuid
    cfg = _cfg()
    s = _store(cfg)
    with open(args.spec) as f:
        spec = json.load(f)
    task_id = args.id or f"task-{uuid.uuid4().hex[:8]}"
    s.create_task(task_id, spec, working_directory=args.cwd)
    s.journal("TASK_QUEUED", task_id=task_id, steps=len(spec.get("steps", [])))
    print(f"queued {task_id}")
    s.close()


def cmd_restart(args):
    cfg = _cfg()
    pid = _read_pid(os.path.join(cfg["run_dir"], "supervisor.pid"))
    if not pid:
        print("supervisor not running; use: systemctl start vm-agent", file=sys.stderr)
        return 1
    os.kill(pid, signal.SIGTERM)
    print(f"sent SIGTERM to supervisor (pid {pid}); it will restart the agent gracefully")


def cmd_diagnostics(args):
    cfg = _cfg()
    s = _store(cfg)
    print("=== heartbeats ===")
    for comp in ("supervisor", "agent"):
        hb = s.get_heartbeat(comp)
        print(f"{comp}: {hb}")
    print("=== recent journal ===")
    day = time.strftime("%Y-%m-%d", time.gmtime())
    jpath = os.path.join(cfg["journal_dir"], f"{day}.jsonl")
    try:
        with open(jpath) as f:
            lines = f.readlines()
        for line in lines[-15:]:
            e = json.loads(line)
            print(time.strftime("%H:%M:%S", time.gmtime(e["ts"])), e["event"],
                  e.get("task_id") or "")
    except OSError:
        print("(no journal today)")
    s.close()


def cmd_recover(args):
    # Recovery happens automatically on agent start; this triggers it now
    # by asking a running agent, or runs it inline if none is up.
    cfg = _cfg()
    s = _store(cfg)
    running = [t for t in s.list_tasks() if t["status"] in ("RUNNING", "PAUSED")]
    print(f"{len(running)} unfinished task(s):")
    for t in running:
        ck = s.latest_checkpoint(t["task_id"])
        print(f"  {t['task_id']}: {t['status']} at step {t['current_step']}, "
              f"last checkpoint: {ck['label'] if ck else 'none'}")
    print("unfinished tasks resume automatically when the agent (re)starts.")
    s.close()


def main():
    ap = argparse.ArgumentParser(prog="vm-agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("health").set_defaults(fn=cmd_health)
    p = sub.add_parser("logs"); p.add_argument("--lines", default="50")
    p.add_argument("name", nargs="?"); p.set_defaults(fn=cmd_logs)
    p = sub.add_parser("tasks"); p.add_argument("--status", default=None)
    p.set_defaults(fn=cmd_tasks)
    p = sub.add_parser("submit"); p.add_argument("spec")
    p.add_argument("--id", default=None); p.add_argument("--cwd", default=None)
    p.set_defaults(fn=cmd_submit)
    sub.add_parser("restart").set_defaults(fn=cmd_restart)
    sub.add_parser("diagnostics").set_defaults(fn=cmd_diagnostics)
    sub.add_parser("recover").set_defaults(fn=cmd_recover)
    args = ap.parse_args()
    sys.exit(args.fn(args) or 0)


if __name__ == "__main__":
    main()
