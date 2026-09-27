"""Persistent state: SQLite task store, checkpoints, append-only event journal.

The database is the source of truth for task continuity. LLM context is never
trusted for progress — only rows in this DB.
"""
import json
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,              -- PENDING|RUNNING|PAUSED|COMPLETED|FAILED
    current_step INTEGER NOT NULL DEFAULT 0,
    current_action TEXT,
    completed_steps TEXT NOT NULL DEFAULT '[]',
    failed_steps TEXT NOT NULL DEFAULT '[]',
    retry_count INTEGER NOT NULL DEFAULT 0,
    working_directory TEXT,
    spec TEXT,                          -- JSON: the plan/steps
    started_at REAL,
    updated_at REAL,
    last_checkpoint TEXT,
    last_verified_result TEXT           -- JSON: verifier output of last op
);
CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    step INTEGER NOT NULL,
    label TEXT,
    state_json TEXT NOT NULL,           -- JSON snapshot of world state
    created_at REAL NOT NULL,
    UNIQUE(task_id, step)
);
CREATE TABLE IF NOT EXISTS heartbeats (
    component TEXT PRIMARY KEY,         -- 'agent', 'supervisor', 'worker:<id>'
    pid INTEGER,
    task_id TEXT,
    step INTEGER,
    operation TEXT,
    last_action TEXT,
    status TEXT,
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS restarts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    component TEXT NOT NULL,
    reason TEXT,
    ts REAL NOT NULL
);
-- Phase 26-60: reliability layer
CREATE TABLE IF NOT EXISTS locks (
    lock_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,               -- component claiming the lock
    pid INTEGER,
    created_at REAL NOT NULL,
    heartbeat_ts REAL NOT NULL,
    expires_at REAL NOT NULL,          -- lease: stale if expired AND owner dead
    operation TEXT
);
CREATE TABLE IF NOT EXISTS leases (
    lease_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    last_heartbeat REAL NOT NULL,
    UNIQUE(task_id, worker_id)
);
CREATE TABLE IF NOT EXISTS operations (
    op_id TEXT PRIMARY KEY,            -- idempotent operation ID
    task_id TEXT,
    kind TEXT NOT NULL,                -- e.g. install_node_20
    status TEXT NOT NULL,              -- PENDING|RUNNING|COMPLETED|FAILED|UNKNOWN
    idempotency_key TEXT,
    result_json TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS interventions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    reason TEXT NOT NULL,
    last_verified_step TEXT,
    next_step TEXT,
    required_action TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN',  -- OPEN|RESOLVED
    created_at REAL NOT NULL,
    resolved_at REAL
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    label TEXT NOT NULL,
    state_json TEXT NOT NULL,
    config_json TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS world_state (
    key TEXT PRIMARY KEY,              -- e.g. runtime.node.installed
    value_json TEXT NOT NULL,          -- verified value
    verified_at REAL NOT NULL,
    verifier TEXT NOT NULL             -- which check produced it
);
CREATE TABLE IF NOT EXISTS model_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    claim_json TEXT NOT NULL,          -- what the model asserted
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS file_hashes (
    path TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    recorded_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS budgets (
    task_id TEXT NOT NULL,
    kind TEXT NOT NULL,                -- tool_calls|retries|duration_s|...
    limit_value REAL NOT NULL,
    used_value REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY (task_id, kind)
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    task_id TEXT,
    actor TEXT NOT NULL,               -- model|runtime|verifier|supervisor|human
    action TEXT NOT NULL,
    args_json TEXT,                    -- redacted
    result TEXT,                       -- ok|failed|refused
    verified TEXT                       -- PASS|FAIL|N/A
);
"""

_lock = threading.RLock()  # RLock: journal() is called from methods
# that already hold the lock (e.g. claim_lease -> LEASE_EXPIRED)


class Store:
    def __init__(self, db_path, journal_dir):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        os.makedirs(journal_dir, exist_ok=True)
        self.db_path = db_path
        self.journal_dir = journal_dir
        self._conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with _lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ---- tasks ----
    def create_task(self, task_id, spec, working_directory=None):
        now = time.time()
        with _lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO tasks (task_id, status, current_step, spec,"
                " working_directory, started_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (task_id, "PENDING", 0, json.dumps(spec), working_directory, now, now),
            )
            self._conn.commit()

    def get_task(self, task_id):
        with _lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def list_tasks(self, status=None):
        with _lock:
            if status:
                rows = self._conn.execute(
                    "SELECT * FROM tasks WHERE status=? ORDER BY updated_at DESC",
                    (status,)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM tasks ORDER BY updated_at DESC").fetchall()
        return [dict(r) for r in rows]

    def update_task(self, task_id, **fields):
        fields["updated_at"] = time.time()
        sets = ", ".join(f"{k}=?" for k in fields)
        with _lock:
            self._conn.execute(
                f"UPDATE tasks SET {sets} WHERE task_id=?",
                (*fields.values(), task_id))
            self._conn.commit()

    # ---- checkpoints ----
    def checkpoint(self, task_id, step, label, state):
        with _lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO checkpoints (task_id, step, label, state_json, created_at)"
                " VALUES (?,?,?,?,?)",
                (task_id, step, label, json.dumps(state), time.time()))
            self._conn.execute(
                "UPDATE tasks SET last_checkpoint=?, updated_at=? WHERE task_id=?",
                (label, time.time(), task_id))
            self._conn.commit()

    def latest_checkpoint(self, task_id):
        with _lock:
            row = self._conn.execute(
                "SELECT * FROM checkpoints WHERE task_id=? ORDER BY step DESC LIMIT 1",
                (task_id,)).fetchone()
        return dict(row) if row else None

    # ---- heartbeats ----
    def heartbeat(self, component, pid=None, task_id=None, step=None,
                  operation=None, last_action=None, status="alive"):
        with _lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO heartbeats (component, pid, task_id, step,"
                " operation, last_action, status, ts) VALUES (?,?,?,?,?,?,?,?)",
                (component, pid, task_id, step, operation, last_action, status,
                 time.time()))
            self._conn.commit()

    def get_heartbeat(self, component):
        with _lock:
            row = self._conn.execute(
                "SELECT * FROM heartbeats WHERE component=?", (component,)).fetchone()
        return dict(row) if row else None

    # ---- kv (world state) ----
    def kv_get(self, key, default=None):
        with _lock:
            row = self._conn.execute(
                "SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def kv_set(self, key, value):
        with _lock:
            self._conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)",
                               (key, json.dumps(value)))
            self._conn.commit()

    # ---- restarts ----
    def record_restart(self, component, reason):
        with _lock:
            self._conn.execute(
                "INSERT INTO restarts (component, reason, ts) VALUES (?,?,?)",
                (component, reason, time.time()))
            self._conn.commit()

    def restart_count_since(self, component, since_ts):
        with _lock:
            row = self._conn.execute(
                "SELECT COUNT(*) c FROM restarts WHERE component=? AND ts>?",
                (component, since_ts)).fetchone()
        return row["c"]

    # ---- event journal (append-only) ----
    def journal(self, event, task_id=None, **meta):
        entry = {"ts": time.time(), "event": event, "task_id": task_id, **meta}
        day = time.strftime("%Y-%m-%d", time.gmtime())
        path = os.path.join(self.journal_dir, f"{day}.jsonl")
        with _lock, open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")
        return entry

    # ---- locks (phase 28) ----
    def acquire_lock(self, lock_id, owner, pid, ttl_s=120, operation=None):
        now = time.time()
        with _lock:
            row = self._conn.execute(
                "SELECT * FROM locks WHERE lock_id=?", (lock_id,)).fetchone()
            if row:
                # existing lock: only take over if stale (expired + owner dead)
                if row["expires_at"] < now and not _pid_alive(row["pid"] or 0):
                    self.journal("LOCK_STOLEN", lock_id=lock_id,
                                 old_owner=row["owner"])
                else:
                    return False
            self._conn.execute(
                "INSERT OR REPLACE INTO locks (lock_id, owner, pid, created_at,"
                " heartbeat_ts, expires_at, operation) VALUES (?,?,?,?,?,?,?)",
                (lock_id, owner, pid, now, now, now + ttl_s, operation))
            self._conn.commit()
        return True

    def refresh_lock(self, lock_id, owner, ttl_s=120):
        with _lock:
            cur = self._conn.execute(
                "UPDATE locks SET heartbeat_ts=?, expires_at=? WHERE lock_id=?"
                " AND owner=?", (time.time(), time.time() + ttl_s, lock_id, owner))
            self._conn.commit()
            return cur.rowcount > 0

    def release_lock(self, lock_id, owner=None):
        with _lock:
            if owner:
                self._conn.execute("DELETE FROM locks WHERE lock_id=? AND owner=?",
                                   (lock_id, owner))
            else:
                self._conn.execute("DELETE FROM locks WHERE lock_id=?", (lock_id,))
            self._conn.commit()

    def list_locks(self):
        with _lock:
            rows = self._conn.execute("SELECT * FROM locks").fetchall()
        return [dict(r) for r in rows]

    def reap_stale_locks(self):
        """Release locks whose lease expired and whose owner process is dead.
        Returns list of reaped lock_ids. Never touches live locks."""
        now = time.time()
        reaped = []
        with _lock:
            rows = self._conn.execute(
                "SELECT * FROM locks WHERE expires_at < ?", (now,)).fetchall()
            for row in rows:
                if not _pid_alive(row["pid"] or 0):
                    self._conn.execute("DELETE FROM locks WHERE lock_id=?",
                                       (row["lock_id"],))
                    reaped.append(row["lock_id"])
            self._conn.commit()
        for lid in reaped:
            self.journal("LOCK_REAPED", lock_id=lid)
        return reaped

    # ---- leases (phase 32) ----
    def claim_lease(self, task_id, worker_id, owner, ttl_s=300):
        """One worker per task: returns True if this worker holds the lease."""
        now = time.time()
        with _lock:
            row = self._conn.execute(
                "SELECT * FROM leases WHERE task_id=?", (task_id,)).fetchone()
            if row and row["worker_id"] != worker_id:
                if row["expires_at"] > now and _pid_alive(_owner_pid(row)):
                    return False  # live lease held by someone else
                self.journal("LEASE_EXPIRED", task_id=task_id,
                             old_worker=row["worker_id"])
            lid = f"{task_id}:{worker_id}"
            self._conn.execute(
                "INSERT OR REPLACE INTO leases (lease_id, task_id, worker_id,"
                " owner, created_at, expires_at, last_heartbeat)"
                " VALUES (?,?,?,?,?,?,?)",
                (lid, task_id, worker_id, owner, now, now + ttl_s, now))
            self._conn.commit()
        return True

    def heartbeat_lease(self, task_id, worker_id, ttl_s=300):
        with _lock:
            cur = self._conn.execute(
                "UPDATE leases SET last_heartbeat=?, expires_at=?"
                " WHERE task_id=? AND worker_id=?",
                (time.time(), time.time() + ttl_s, task_id, worker_id))
            self._conn.commit()
            return cur.rowcount > 0

    def release_lease(self, task_id, worker_id=None):
        with _lock:
            if worker_id:
                self._conn.execute(
                    "DELETE FROM leases WHERE task_id=? AND worker_id=?",
                    (task_id, worker_id))
            else:
                self._conn.execute("DELETE FROM leases WHERE task_id=?",
                                   (task_id,))
            self._conn.commit()

    # ---- operations registry (phases 30/31) ----
    def op_get(self, op_id):
        with _lock:
            row = self._conn.execute(
                "SELECT * FROM operations WHERE op_id=?", (op_id,)).fetchone()
        return dict(row) if row else None

    def op_set(self, op_id, task_id, kind, status, idempotency_key=None,
               result=None):
        now = time.time()
        with _lock:
            self._conn.execute(
                "INSERT INTO operations (op_id, task_id, kind, status,"
                " idempotency_key, result_json, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(op_id) DO UPDATE SET status=excluded.status,"
                " result_json=excluded.result_json, updated_at=excluded.updated_at",
                (op_id, task_id, kind, status, idempotency_key,
                 json.dumps(result) if result is not None else None, now, now))
            self._conn.commit()

    def op_mark_unknown(self, op_id):
        """After an ambiguous crash: reconcile before retrying."""
        with _lock:
            self._conn.execute(
                "UPDATE operations SET status='UNKNOWN', updated_at=? WHERE op_id=?",
                (time.time(), op_id))
            self._conn.commit()
        self.journal("OP_UNKNOWN", op_id=op_id)

    # ---- interventions (phase 48) ----
    def intervention_open(self, task_id, reason, required_action,
                          last_verified_step=None, next_step=None):
        with _lock:
            cur = self._conn.execute(
                "INSERT INTO interventions (task_id, reason, last_verified_step,"
                " next_step, required_action, status, created_at)"
                " VALUES (?,?,?,?,?,'OPEN',?)",
                (task_id, reason, last_verified_step, next_step,
                 required_action, time.time()))
            self._conn.commit()
            iid = cur.lastrowid
        self.journal("INTERVENTION_OPENED", task_id=task_id,
                     intervention_id=iid, reason=reason)
        return iid

    def intervention_resolve(self, iid, note=None):
        with _lock:
            self._conn.execute(
                "UPDATE interventions SET status='RESOLVED', resolved_at=? WHERE id=?",
                (time.time(), iid))
            self._conn.commit()
        self.journal("INTERVENTION_RESOLVED", intervention_id=iid, note=note)

    def interventions_open(self):
        with _lock:
            rows = self._conn.execute(
                "SELECT * FROM interventions WHERE status='OPEN'"
                " ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    # ---- snapshots (phase 47) ----
    def snapshot(self, task_id, label, state, config=None):
        with _lock:
            cur = self._conn.execute(
                "INSERT INTO snapshots (task_id, label, state_json, config_json,"
                " created_at) VALUES (?,?,?,?,?)",
                (task_id, label, json.dumps(state),
                 json.dumps(config) if config else None, time.time()))
            self._conn.commit()
            return cur.lastrowid

    def latest_snapshot(self, task_id, label=None):
        with _lock:
            if label:
                row = self._conn.execute(
                    "SELECT * FROM snapshots WHERE task_id=? AND label=?"
                    " ORDER BY id DESC LIMIT 1", (task_id, label)).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT * FROM snapshots WHERE task_id=? ORDER BY id DESC"
                    " LIMIT 1", (task_id,)).fetchone()
        return dict(row) if row else None

    # ---- world state (phase 34): verifier-only writes ----
    def world_set(self, key, value, verifier):
        with _lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO world_state (key, value_json,"
                " verified_at, verifier) VALUES (?,?,?,?)",
                (key, json.dumps(value), time.time(), verifier))
            self._conn.commit()

    def world_get(self, key, default=None):
        with _lock:
            row = self._conn.execute(
                "SELECT value_json FROM world_state WHERE key=?", (key,)).fetchone()
        return json.loads(row["value_json"]) if row else default

    def world_all(self):
        with _lock:
            rows = self._conn.execute("SELECT * FROM world_state").fetchall()
        return {r["key"]: {"value": json.loads(r["value_json"]),
                           "verified_at": r["verified_at"],
                           "verifier": r["verifier"]} for r in rows}

    # ---- model claims (phase 35): kept separate from observations ----
    def claim(self, task_id, claim):
        with _lock:
            self._conn.execute(
                "INSERT INTO model_claims (task_id, claim_json, created_at)"
                " VALUES (?,?,?)",
                (task_id, json.dumps(claim), time.time()))
            self._conn.commit()
        self.journal("MODEL_CLAIM", task_id=task_id, claim=claim)

    # ---- file hashes (phase 45) ----
    def hash_record(self, path, sha256):
        with _lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO file_hashes (path, sha256, recorded_at)"
                " VALUES (?,?,?)", (path, sha256, time.time()))
            self._conn.commit()

    def hash_get(self, path):
        with _lock:
            row = self._conn.execute(
                "SELECT sha256 FROM file_hashes WHERE path=?", (path,)).fetchone()
        return row["sha256"] if row else None

    # ---- budgets (phase 39) ----
    def budget_set(self, task_id, kind, limit):
        with _lock:
            self._conn.execute(
                "INSERT INTO budgets (task_id, kind, limit_value, used_value,"
                " updated_at) VALUES (?,?,?,0,?)"
                " ON CONFLICT(task_id, kind) DO UPDATE SET limit_value=excluded.limit_value",
                (task_id, kind, limit, time.time()))
            self._conn.commit()

    def budget_consume(self, task_id, kind, amount=1):
        """Returns (ok, used, limit): False when the budget is exhausted."""
        with _lock:
            row = self._conn.execute(
                "SELECT limit_value, used_value FROM budgets WHERE task_id=?"
                " AND kind=?", (task_id, kind)).fetchone()
            if not row:
                return True, 0, None  # no budget configured = unlimited
            used = row["used_value"] + amount
            self._conn.execute(
                "UPDATE budgets SET used_value=?, updated_at=? WHERE task_id=?"
                " AND kind=?", (used, time.time(), task_id, kind))
            self._conn.commit()
            ok = used <= row["limit_value"]
            if not ok:
                self.journal("BUDGET_EXCEEDED", task_id=task_id, kind=kind,
                             used=used, limit=row["limit_value"])
            return ok, used, row["limit_value"]

    # ---- audit (phase 49) ----
    def audit(self, actor, action, task_id=None, args=None, result=None,
              verified=None):
        with _lock:
            self._conn.execute(
                "INSERT INTO audit (ts, task_id, actor, action, args_json,"
                " result, verified) VALUES (?,?,?,?,?,?,?)",
                (time.time(), task_id, actor, action,
                 json.dumps(_redact_audit(args)) if args else None,
                 result, verified))
            self._conn.commit()

    def close(self):
        with _lock:
            self._conn.commit()
            self._conn.close()


def _pid_alive(pid):
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _owner_pid(lease_row):
    # owner stored as "component:pid" or plain component; best-effort parse
    owner = lease_row["owner"] or ""
    try:
        return int(owner.rsplit(":", 1)[-1])
    except (ValueError, AttributeError):
        return 0


def _redact_audit(args):
    if not isinstance(args, dict):
        return {"_": str(args)[:200]}
    red = {}
    for k, v in args.items():
        kl = k.lower()
        if any(s in kl for s in ("token", "secret", "password", "passwd",
                                 "key", "auth", "credential")):
            red[k] = "***REDACTED***"
        else:
            red[k] = v if not isinstance(v, str) or len(v) <= 500 else f"<{len(v)} chars>"
    return red
