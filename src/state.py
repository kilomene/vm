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
    content_sha256 TEXT,
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
-- Phase 51-90: advanced reliability layer
CREATE TABLE IF NOT EXISTS recovery_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    task_id TEXT,
    failure_kind TEXT NOT NULL,        -- e.g. worker_crash
    failure_signature TEXT NOT NULL,   -- fingerprint (phase 56)
    recovery_method TEXT NOT NULL,     -- e.g. restart_worker
    level INTEGER,                     -- escalation level used
    result TEXT NOT NULL,              -- ok|failed
    detail TEXT
);
CREATE TABLE IF NOT EXISTS failure_fingerprints (
    signature TEXT PRIMARY KEY,
    task_id TEXT,
    kind TEXT NOT NULL,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    occurrences INTEGER NOT NULL DEFAULT 1,
    last_method TEXT,
    escalated INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS resource_owners (
    resource_id TEXT PRIMARY KEY,      -- e.g. "port:8765", "file:/tmp/x"
    kind TEXT NOT NULL,                -- port|file|process|tmpdir|browser_session|deployment|external_job
    task_id TEXT,
    execution_id TEXT,
    owner TEXT NOT NULL,
    acquired_at REAL NOT NULL,
    lease_expires_at REAL,
    state TEXT NOT NULL DEFAULT 'active',  -- active|released|stale
    meta TEXT
);
CREATE TABLE IF NOT EXISTS external_resources (
    resource_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    task_id TEXT NOT NULL,
    creation_op TEXT,
    known_state TEXT NOT NULL DEFAULT 'UNKNOWN',  -- SUCCESS|FAILED|PENDING|UNKNOWN
    last_verified REAL,
    meta TEXT
);
CREATE TABLE IF NOT EXISTS capabilities (
    task_id TEXT NOT NULL,
    capability TEXT NOT NULL,
    granted_by TEXT NOT NULL DEFAULT 'scheduler',
    granted_at REAL NOT NULL,
    expires_at REAL,
    PRIMARY KEY (task_id, capability)
);
CREATE TABLE IF NOT EXISTS config_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    config_path TEXT NOT NULL,
    version INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    content TEXT NOT NULL,
    is_known_good INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS backups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    created_at REAL NOT NULL,
    path TEXT NOT NULL,
    includes_json TEXT NOT NULL,
    manifest_sha256 TEXT,
    restored_ok INTEGER,               -- NULL=never tested, 0=failed, 1=passed
    restored_at REAL
);
CREATE TABLE IF NOT EXISTS executions (
    execution_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL,
    status TEXT NOT NULL DEFAULT 'RUNNING'  -- RUNNING|DONE|ABANDONED
);
CREATE TABLE IF NOT EXISTS migrations (
    version INTEGER PRIMARY KEY,
    applied_at REAL NOT NULL,
    note TEXT
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
            self._migrate()
            self._conn.commit()

    def _migrate(self):
        """Column-level migrations for DBs created before a schema change."""
        cols = {r["name"] for r in self._conn.execute(
            "PRAGMA table_info(capabilities)")}
        if "expires_at" not in cols:
            self._conn.execute(
                "ALTER TABLE capabilities ADD COLUMN expires_at REAL")
        scols = {r["name"] for r in self._conn.execute(
            "PRAGMA table_info(snapshots)")}
        if "content_sha256" not in scols:
            self._conn.execute(
                "ALTER TABLE snapshots ADD COLUMN content_sha256 TEXT")

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

    def checkpoints(self, task_id):
        """All checkpoints for a task, oldest first (phase 67: the chain
        request_resume verifies before allowing a resume)."""
        with _lock:
            rows = self._conn.execute(
                "SELECT * FROM checkpoints WHERE task_id=? ORDER BY step ASC",
                (task_id,)).fetchall()
        return [dict(r) for r in rows]

    def clear_checkpoints(self, task_id):
        """Delete all checkpoints for a task. Used by tests/fault injection
        to model a crash that happened before the checkpoint was written."""
        with _lock:
            self._conn.execute(
                "DELETE FROM checkpoints WHERE task_id=?", (task_id,))
            self._conn.commit()

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

    # ---- snapshots (phase 47/67) ----
    def snapshot(self, task_id, label, state, config=None):
        with _lock:
            cur = self._conn.execute(
                "INSERT INTO snapshots (task_id, label, state_json, config_json,"
                " created_at) VALUES (?,?,?,?,?)",
                (task_id, label, json.dumps(state),
                 json.dumps(config) if config else None, time.time()))
            self._conn.commit()
            return cur.lastrowid

    def snapshot_save(self, task_id, label, state_blob, content_sha256=None):
        """Phase 67: content-hashed resumable snapshot. state_blob is the
        canonical JSON string (hash computed over exactly this)."""
        with _lock:
            cur = self._conn.execute(
                "INSERT INTO snapshots (task_id, label, state_json,"
                " content_sha256, created_at) VALUES (?,?,?,?,?)",
                (task_id, label, state_blob, content_sha256, time.time()))
            self._conn.commit()
            return cur.lastrowid

    def snapshot_verify(self, snapshot_id):
        """Phase 67: verify a snapshot's content hash. Returns (ok, reason)."""
        import hashlib as _h
        with _lock:
            row = self._conn.execute(
                "SELECT * FROM snapshots WHERE id=?",
                (snapshot_id,)).fetchone()
        if not row:
            return False, "snapshot not found"
        d = dict(row)
        if not d.get("content_sha256"):
            return True, "no hash recorded (legacy snapshot)"
        actual = _h.sha256(d["state_json"].encode()).hexdigest()
        if actual != d["content_sha256"]:
            return False, "content hash mismatch: snapshot corrupted"
        return True, "hash verified"

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

    # ---- recovery attempts + failure fingerprints (phases 55/56) ----
    def recovery_record(self, task_id, failure_kind, signature, method,
                        level=None, result="ok", detail=None):
        now = time.time()
        with _lock:
            self._conn.execute(
                "INSERT INTO recovery_attempts (ts, task_id, failure_kind,"
                " failure_signature, recovery_method, level, result, detail)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (now, task_id, failure_kind, signature, method, level,
                 result, detail))
            row = self._conn.execute(
                "SELECT occurrences FROM failure_fingerprints WHERE signature=?",
                (signature,)).fetchone()
            if row:
                self._conn.execute(
                    "UPDATE failure_fingerprints SET last_seen=?,"
                    " occurrences=occurrences+1, last_method=? WHERE signature=?",
                    (now, method, signature))
            else:
                self._conn.execute(
                    "INSERT INTO failure_fingerprints (signature, task_id, kind,"
                    " first_seen, last_seen, occurrences, last_method)"
                    " VALUES (?,?,?,?,?,1,?)",
                    (signature, task_id, failure_kind, now, now, method))
            self._conn.commit()
        self.journal("RECOVERY_RECORDED", task_id=task_id,
                     failure=failure_kind, method=method, result=result)

    def recoveries_list(self, task_id=None, limit=20):
        """Phase 55: recent recovery attempts, newest first."""
        with _lock:
            q = "SELECT * FROM recovery_attempts"
            args = []
            if task_id:
                q += " WHERE task_id=?"
                args.append(task_id)
            q += " ORDER BY id DESC LIMIT ?"
            args.append(limit)
            rows = self._conn.execute(q, args).fetchall()
        return [dict(r) for r in rows]

    def task_recoveries(self, task_id, limit=20):
        """Phase 55: recovery history for one task (alias)."""
        return self.recoveries_list(task_id=task_id, limit=limit)

    def failure_signatures(self, task_id=None, limit=50):
        """Phase 56: known failure fingerprints, most frequent first."""
        with _lock:
            q = "SELECT * FROM failure_fingerprints"
            args = []
            if task_id:
                q += " WHERE task_id=?"
                args.append(task_id)
            q += " ORDER BY occurrences DESC LIMIT ?"
            args.append(limit)
            rows = self._conn.execute(q, args).fetchall()
        return [dict(r) for r in rows]

    def recovery_history(self, signature=None, task_id=None, limit=20):
        with _lock:
            q = "SELECT * FROM recovery_attempts"
            conds, args = [], []
            if signature:
                conds.append("failure_signature=?"); args.append(signature)
            if task_id:
                conds.append("task_id=?"); args.append(task_id)
            if conds:
                q += " WHERE " + " AND ".join(conds)
            q += " ORDER BY id DESC LIMIT ?"
            args.append(limit)
            rows = self._conn.execute(q, args).fetchall()
        return [dict(r) for r in rows]

    def same_method_failed(self, signature, method):
        """True if this exact method already failed for this signature —
        don't repeat a known-unsuccessful strategy."""
        with _lock:
            row = self._conn.execute(
                "SELECT COUNT(*) c FROM recovery_attempts"
                " WHERE failure_signature=? AND recovery_method=?"
                " AND result='failed'", (signature, method)).fetchone()
        return row["c"] > 0

    def fingerprint_get(self, signature):
        with _lock:
            row = self._conn.execute(
                "SELECT * FROM failure_fingerprints WHERE signature=?",
                (signature,)).fetchone()
        return dict(row) if row else None

    def fingerprint_escalate(self, signature):
        with _lock:
            self._conn.execute(
                "UPDATE failure_fingerprints SET escalated=1 WHERE signature=?",
                (signature,))
            self._conn.commit()
        self.journal("FAILURE_ESCALATED", signature=signature)

    # ---- resource ownership (phases 58/85/86/87) ----
    def resource_acquire(self, resource_id, kind, owner, task_id=None,
                         execution_id=None, ttl_s=None, meta=None):
        now = time.time()
        with _lock:
            row = self._conn.execute(
                "SELECT * FROM resource_owners WHERE resource_id=?",
                (resource_id,)).fetchone()
            if row and row["state"] == "active" and row["owner"] != owner:
                exp = row["lease_expires_at"]
                if exp is None or exp > now:
                    return False  # owned by someone else
            self._conn.execute(
                "INSERT OR REPLACE INTO resource_owners (resource_id, kind,"
                " task_id, execution_id, owner, acquired_at, lease_expires_at,"
                " state, meta) VALUES (?,?,?,?,?,?,?,?,?)",
                (resource_id, kind, task_id, execution_id, owner, now,
                 (now + ttl_s) if ttl_s else None, "active",
                 json.dumps(meta) if meta else None))
            self._conn.commit()
        return True

    def resource_release(self, resource_id, owner=None):
        with _lock:
            if owner:
                self._conn.execute(
                    "UPDATE resource_owners SET state='released'"
                    " WHERE resource_id=? AND owner=?",
                    (resource_id, owner))
            else:
                self._conn.execute(
                    "UPDATE resource_owners SET state='released'"
                    " WHERE resource_id=?", (resource_id,))
            self._conn.commit()

    def resource_get(self, resource_id):
        with _lock:
            row = self._conn.execute(
                "SELECT * FROM resource_owners WHERE resource_id=?",
                (resource_id,)).fetchone()
        return dict(row) if row else None

    def resource_owned_by(self, resource_id, owner):
        r = self.resource_get(resource_id)
        return bool(r and r["state"] == "active" and r["owner"] == owner)

    def resources_for_task(self, task_id, active_only=True):
        with _lock:
            q = "SELECT * FROM resource_owners WHERE task_id=?"
            if active_only:
                q += " AND state='active'"
            rows = self._conn.execute(q, (task_id,)).fetchall()
        return [dict(r) for r in rows]

    def resource_sweep_stale(self):
        """Mark expired resources owned by dead processes as stale."""
        now = time.time()
        stale = []
        with _lock:
            rows = self._conn.execute(
                "SELECT * FROM resource_owners WHERE state='active'"
                " AND lease_expires_at IS NOT NULL"
                " AND lease_expires_at < ?", (now,)).fetchall()
            for row in rows:
                pid = _owner_pid_row(row["owner"])
                if not _pid_alive(pid):
                    self._conn.execute(
                        "UPDATE resource_owners SET state='stale'"
                        " WHERE resource_id=?", (row["resource_id"],))
                    stale.append(row["resource_id"])
            self._conn.commit()
        for rid in stale:
            self.journal("RESOURCE_STALE", resource_id=rid)
        return stale

    # ---- external resources (phase 63) ----
    def ext_resource_register(self, resource_id, kind, task_id, creation_op,
                              meta=None):
        with _lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO external_resources (resource_id, kind,"
                " task_id, creation_op, known_state, last_verified, meta)"
                " VALUES (?,?,?,?, 'UNKNOWN', ?, ?)",
                (resource_id, kind, task_id, creation_op, time.time(),
                 json.dumps(meta) if meta else None))
            self._conn.commit()

    def ext_resource_update(self, resource_id, known_state, meta=None):
        with _lock:
            self._conn.execute(
                "UPDATE external_resources SET known_state=?, last_verified=?,"
                " meta=COALESCE(?, meta) WHERE resource_id=?",
                (known_state, time.time(),
                 json.dumps(meta) if meta else None, resource_id))
            self._conn.commit()

    def ext_resource_get(self, resource_id):
        with _lock:
            row = self._conn.execute(
                "SELECT * FROM external_resources WHERE resource_id=?",
                (resource_id,)).fetchone()
        return dict(row) if row else None

    def ext_resources_pending(self):
        with _lock:
            rows = self._conn.execute(
                "SELECT * FROM external_resources WHERE known_state IN"
                " ('PENDING','UNKNOWN')").fetchall()
        return [dict(r) for r in rows]

    # ---- capabilities (phase 59) ----
    def capability_grant(self, task_id, capability, granted_by="scheduler",
                         ttl_s=None):
        now = time.time()
        expires = (now + ttl_s) if ttl_s is not None else None
        with _lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO capabilities (task_id, capability,"
                " granted_by, granted_at, expires_at) VALUES (?,?,?,?,?)",
                (task_id, capability, granted_by, now, expires))
            self._conn.commit()

    def capability_revoke(self, task_id, capability):
        with _lock:
            self._conn.execute(
                "DELETE FROM capabilities WHERE task_id=? AND capability=?",
                (task_id, capability))
            self._conn.commit()

    def capabilities_for(self, task_id):
        with _lock:
            rows = self._conn.execute(
                "SELECT capability FROM capabilities WHERE task_id=?",
                (task_id,)).fetchall()
        return {r["capability"] for r in rows}

    def capability_has(self, task_id, capability):
        with _lock:
            row = self._conn.execute(
                "SELECT expires_at FROM capabilities WHERE task_id=? AND"
                " capability=?", (task_id, capability)).fetchone()
        if not row:
            return False
        exp = row["expires_at"]
        if exp is not None and time.time() >= exp:
            # expired: clean up lazily
            with _lock:
                self._conn.execute(
                    "DELETE FROM capabilities WHERE task_id=? AND"
                    " capability=?", (task_id, capability))
                self._conn.commit()
            return False
        return True

    # ---- config versions (phase 61) ----
    def config_version_save(self, config_path, content, note="",
                            known_good=False):
        import hashlib as _h
        digest = _h.sha256(content.encode()).hexdigest()
        with _lock:
            row = self._conn.execute(
                "SELECT MAX(version) v FROM config_versions WHERE config_path=?",
                (config_path,)).fetchone()
            ver = (row["v"] or 0) + 1
            cur = self._conn.execute(
                "INSERT INTO config_versions (config_path, version, sha256,"
                " content, is_known_good, created_at, note)"
                " VALUES (?,?,?,?,?,?,?)",
                (config_path, ver, digest, content,
                 1 if known_good else 0, time.time(), note))
            if known_good:
                self._conn.execute(
                    "UPDATE config_versions SET is_known_good=0"
                    " WHERE config_path=? AND id<>?",
                    (config_path, cur.lastrowid))
            self._conn.commit()
            return ver

    def config_last_known_good(self, config_path):
        with _lock:
            row = self._conn.execute(
                "SELECT * FROM config_versions WHERE config_path=?"
                " AND is_known_good=1 ORDER BY version DESC LIMIT 1",
                (config_path,)).fetchone()
        return dict(row) if row else None

    def config_versions(self, config_path, limit=10):
        """Phase 61: version history for a config file, newest first."""
        with _lock:
            rows = self._conn.execute(
                "SELECT * FROM config_versions WHERE config_path=?"
                " ORDER BY version DESC LIMIT ?",
                (config_path, limit)).fetchall()
        return [dict(r) for r in rows]

    # ---- backups (phase 76) ----
    def backup_record(self, label, path, includes, manifest_sha256=None):
        with _lock:
            cur = self._conn.execute(
                "INSERT INTO backups (label, created_at, path, includes_json,"
                " manifest_sha256) VALUES (?,?,?,?,?)",
                (label, time.time(), path, json.dumps(includes),
                 manifest_sha256))
            self._conn.commit()
            return cur.lastrowid

    def backup_mark_restored(self, backup_id, ok):
        with _lock:
            self._conn.execute(
                "UPDATE backups SET restored_ok=?, restored_at=? WHERE id=?",
                (1 if ok else 0, time.time(), backup_id))
            self._conn.commit()

    def backups_list(self):
        with _lock:
            rows = self._conn.execute(
                "SELECT * FROM backups ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]

    # ---- executions (phase 85) ----
    def execution_begin(self, execution_id, task_id):
        with _lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO executions (execution_id, task_id,"
                " started_at, status) VALUES (?,?,?,'RUNNING')",
                (execution_id, task_id, time.time()))
            self._conn.commit()

    def execution_end(self, execution_id, status="DONE"):
        with _lock:
            self._conn.execute(
                "UPDATE executions SET ended_at=?, status=? WHERE execution_id=?",
                (time.time(), status, execution_id))
            self._conn.commit()

    def execution_get(self, execution_id):
        with _lock:
            row = self._conn.execute(
                "SELECT * FROM executions WHERE execution_id=?",
                (execution_id,)).fetchone()
        return dict(row) if row else None

    # ---- migrations (phase 74) ----
    def migration_applied(self, version, note=""):
        with _lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO migrations (version, applied_at, note)"
                " VALUES (?,?,?)", (version, time.time(), note))
            self._conn.commit()

    def migration_version(self):
        with _lock:
            row = self._conn.execute(
                "SELECT MAX(version) v FROM migrations").fetchone()
        return row["v"] or 0

    def close(self):
        with _lock:
            self._conn.commit()
            self._conn.close()


def _owner_pid_row(owner):
    try:
        return int(str(owner or "").rsplit(":", 1)[-1])
    except (ValueError, AttributeError):
        return 0


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
