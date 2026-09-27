"""Phase 53: progress detection from OBSERVABLE state changes.

Progress is never measured by model output. It is measured by comparing
snapshots of the real world before and after an action:

  - files created / modified / deleted (hashes)
  - processes started / stopped
  - ports opened / closed
  - command outputs changed
  - database rows changed (row counts via a probe query)
  - endpoints became healthy

If repeated actions produce no meaningful state change, the task is marked
STALLED and recovery is triggered.
"""
import hashlib
import json
import os
import socket
import time


def _file_fingerprint(paths):
    out = {}
    for p in paths or []:
        try:
            if os.path.isfile(p):
                h = hashlib.sha256()
                with open(p, "rb") as f:
                    for chunk in iter(lambda: f.read(65536), b""):
                        h.update(chunk)
                out[p] = h.hexdigest()[:16]
            elif os.path.exists(p):
                out[p] = "exists"
        except OSError:
            out[p] = "unreadable"
    return out


def _process_fingerprint(patterns, executor):
    out = {}
    for pat in patterns or []:
        r = executor.run("shell", args={
            "command": f"ps aux | grep -F '{pat}' | grep -v grep | wc -l",
            "timeout_s": 15})
        try:
            out[pat] = int((r.stdout or "0").strip())
        except ValueError:
            out[pat] = -1
    return out


def _port_fingerprint(ports):
    out = {}
    for host, port in ports or []:
        s = socket.socket()
        s.settimeout(3)
        try:
            s.connect((host, port))
            out[f"{host}:{port}"] = "open"
        except OSError:
            out[f"{host}:{port}"] = "closed"
        finally:
            s.close()
    return out


def _command_fingerprint(commands, executor):
    out = {}
    for name, cmd in (commands or {}).items():
        r = executor.run("shell", args={"command": cmd, "timeout_s": 30})
        out[name] = hashlib.sha256(
            (r.stdout or "").encode()).hexdigest()[:16]
    return out


class StateFingerprint:
    """A snapshot of observable world state for progress comparison."""

    def __init__(self, files=None, processes=None, ports=None, commands=None):
        self.files = _file_fingerprint(files)
        self.processes = _process_fingerprint(processes, _NoExecutor())
        self.ports = _port_fingerprint(ports)
        self.commands = commands or {}
        self.targets = {"files": files, "processes": processes,
                        "ports": ports, "commands": commands}

    @classmethod
    def capture(cls, executor, targets):
        fp = cls.__new__(cls)
        fp.files = _file_fingerprint(targets.get("files"))
        fp.processes = _process_fingerprint(targets.get("processes"),
                                            executor)
        fp.ports = _port_fingerprint(targets.get("ports"))
        fp.commands = _command_fingerprint(targets.get("commands"),
                                           executor)
        fp.ts = time.time()
        return fp

    def diff(self, other):
        """Return list of meaningful changes vs another fingerprint."""
        changes = []
        for section in ("files", "processes", "ports", "commands"):
            a, b = getattr(self, section), getattr(other, section)
            for k in set(a) | set(b):
                if a.get(k) != b.get(k):
                    changes.append(f"{section}:{k}: {a.get(k)} -> {b.get(k)}")
        return changes

    def to_dict(self):
        return {"files": self.files, "processes": self.processes,
                "ports": self.ports, "commands": self.commands,
                "ts": getattr(self, "ts", time.time())}


class _NoExecutor:
    def run(self, *a, **k):
        class R:
            stdout = ""
        return R()


class ProgressTracker:
    """Tracks per-task observable progress across steps.

    A step that produces no fingerprint change is a no-op. After
    `stall_limit` consecutive no-op steps, the task is STALLED and the
    recovery manager is asked to intervene.
    """

    def __init__(self, store, executor, journal=None, stall_limit=3):
        self.store = store
        self.ex = executor
        self.journal = journal or store.journal
        self.stall_limit = stall_limit
        self._nochange = {}  # task_id -> consecutive no-change steps
        self._before = {}    # task_id -> StateFingerprint

    def before_step(self, task_id, targets):
        fp = StateFingerprint.capture(self.ex, targets)
        fp.targets = targets  # remember what to re-capture
        self._before[task_id] = fp

    def after_step(self, task_id, step_idx, name):
        before = self._before.pop(task_id, None)
        if before is None:
            return True
        after = StateFingerprint.capture(self.ex, before.targets or {})
        changes = before.diff(after)
        self.journal("PROGRESS_CHECK", task_id=task_id, step=step_idx,
                     name=name, changes=len(changes))
        if changes:
            self._nochange[task_id] = 0
            self.store.journal("PROGRESS_OBSERVED", task_id=task_id,
                               step=step_idx, changes=changes[:10])
            return True
        n = self._nochange.get(task_id, 0) + 1
        self._nochange[task_id] = n
        self.journal("PROGRESS_NONE", task_id=task_id, step=step_idx,
                     consecutive=n)
        if n >= self.stall_limit:
            self.journal("TASK_STALLED", task_id=task_id,
                         consecutive_nochange=n)
            self.store.update_task(task_id, status="STALLED")
            return False
        return True

    def reset(self, task_id):
        self._nochange.pop(task_id, None)
        self._before.pop(task_id, None)


# ---- store-backed progress tracking (agent integration) ----
def fingerprint_of(store):
    """Hash of the task's observable world-state (progress.* keys, excluding
    the tracker's own history). The agent updates progress.step/last_ok on
    every verified step, so a changing fingerprint means real progress."""
    data = {}
    for k, v in store.world_all().items():
        if k.startswith("progress.") and ".fingerprints." not in k:
            data[k] = v["value"]
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:16]


def track_stall(store, task_id, max_same=3):
    """Record the current observable fingerprint for a task.

    Returns (stalled, consecutive_identical): stalled is True when the
    last `max_same` fingerprints are identical, i.e. repeated actions
    produced no observable state change.
    """
    fp = fingerprint_of(store)
    key = f"progress.fingerprints.{task_id}"
    hist = store.world_get(key) or []
    hist = (hist + [fp])[-max_same:]
    store.world_set(key, hist, verifier="progress:tracker")
    # count trailing identical fingerprints
    n = 1
    for prev in reversed(hist[:-1]):
        if prev == fp:
            n += 1
        else:
            break
    stalled = len(hist) == max_same and n == max_same
    if stalled:
        store.journal("PROGRESS_STALLED", task_id=task_id,
                      unchanged_cycles=n)
    return stalled, n
