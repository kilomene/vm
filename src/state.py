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
"""

_lock = threading.Lock()


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

    def close(self):
        with _lock:
            self._conn.commit()
            self._conn.close()
