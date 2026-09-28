"""Phase 50: secure local control interface.

HTTP API bound to 127.0.0.1 only, token-authenticated. Operations:
  status, health, tasks, pause, resume, cancel, restart-worker,
  diagnostics, recovery-events, interventions.

Never exposed publicly; the token lives in the install dir with 0600 perms
and is never logged.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse


def _token_path(base_dir):
    return os.path.join(base_dir, "run", "control.token")


def ensure_token(base_dir):
    """Create the control token once; return it.

    The file is created atomically with mode 0600 via os.open with
    O_CREAT|O_EXCL: there is no 0644 window between creation and chmod
    (the old code created with the default umask and chmod'd after).
    If the token already exists, it is read back, never replaced.
    run/ is created (or tightened) to 0700.
    """
    path = _token_path(base_dir)
    run_dir = os.path.dirname(path)
    os.makedirs(run_dir, mode=0o700, exist_ok=True)
    try:
        os.chmod(run_dir, 0o700)
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        with open(path) as f:
            return f.read().strip()
    try:
        with os.fdopen(fd, "w") as f:
            f.write(token)
    except BaseException:
        # don't leave a half-written token behind for a retry to read
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return token


def read_token(base_dir):
    try:
        with open(_token_path(base_dir)) as f:
            return f.read().strip()
    except OSError:
        return None


class ControlServer:
    def __init__(self, store, cfg, agent_ref=None, journal=None):
        self.store = store
        self.cfg = cfg
        self.agent_ref = agent_ref  # callable returning the AgentRuntime
        self.journal = journal or store.journal
        self._httpd = None
        self._thread = None
        self.token = ensure_token(cfg["base_dir"])

    def _agent(self):
        return self.agent_ref() if self.agent_ref else None

    def routes(self):
        import time
        s = self.store
        a = self._agent()
        return {
            ("GET", "/status"): lambda b: {"ok": True, "ts": time.time()},
            ("GET", "/health"): lambda b: self._health(),
            ("GET", "/tasks"): lambda b: s.list_tasks(),
            ("GET", "/interventions"): lambda b: s.interventions_open(),
            ("GET", "/locks"): lambda b: s.list_locks(),
            ("GET", "/world"): lambda b: s.world_all(),
            ("POST", "/pause"): lambda b: self._task_op(b, "pause"),
            ("POST", "/resume"): lambda b: self._task_op(b, "resume"),
            ("POST", "/cancel"): lambda b: self._task_op(b, "cancel"),
            ("GET", "/diagnostics"): lambda b: self._diagnostics(),
            ("GET", "/recovery-events"): lambda b: self._recovery_events(),
        }

    def _health(self):
        import time
        hb = self.store.get_heartbeat("agent")
        return {"agent_heartbeat_age_s":
                round(time.time() - hb["ts"], 1) if hb else None,
                "tasks_running": len(self.store.list_tasks("RUNNING")),
                "safe_mode": (self.store.kv_get("safe_mode") or {})
                .get("active", False)}

    def _task_op(self, body, op):
        task_id = (body or {}).get("task_id")
        if not task_id:
            return {"error": "task_id required"}
        a = self._agent()
        if not a:
            return {"error": "agent not running"}
        fn = {"pause": a.request_pause, "resume": a.request_resume,
              "cancel": a.request_cancel}[op]
        return {"ok": True, "result": fn(task_id)}

    def _diagnostics(self):
        out = {"heartbeats": {},
               "locks": self.store.list_locks(),
               "interventions": len(self.store.interventions_open()),
               "world_keys": len(self.store.world_all())}
        for comp in ("supervisor", "agent"):
            hb = self.store.get_heartbeat(comp)
            out["heartbeats"][comp] = hb
        return out

    def _recovery_events(self, limit=50):
        day = __import__("time").strftime("%Y-%m-%d",
                                          __import__("time").gmtime())
        path = os.path.join(self.cfg["journal_dir"], f"{day}.jsonl")
        events = []
        try:
            with open(path) as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if "RECOVER" in e.get("event", "") or "SAFE_MODE" in e.get(
                            "event", "") or "DEADLOCK" in e.get("event", ""):
                        events.append(e)
        except OSError:
            pass
        return events[-limit:]

    def _handler(self):
        routes = self.routes()
        token = self.token
        journal = self.journal

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet; journal instead
                pass

            def _auth(self):
                auth = self.headers.get("Authorization", "")
                if not auth.startswith("Bearer "):
                    return False
                presented = auth[7:]
                return hmac.compare_digest(presented, token)

            def _send(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle(self):
                if not self._auth():
                    return self._send(401, {"error": "unauthorized"})
                parsed = urlparse(self.path)
                fn = routes.get((self.command, parsed.path))
                if not fn:
                    return self._send(404, {"error": "not found"})
                body = None
                if self.command == "POST":
                    try:
                        n = int(self.headers.get("Content-Length", 0))
                        raw = self.rfile.read(n) if n else b""
                        body = json.loads(raw) if raw else {}
                    except (ValueError, OSError):
                        return self._send(400, {"error": "bad json"})
                try:
                    result = fn(body)
                except Exception as e:  # noqa: BLE001
                    journal("CONTROL_ERROR", path=parsed.path, error=repr(e))
                    return self._send(500, {"error": "internal"})
                journal("CONTROL_CALL", path=parsed.path)
                return self._send(200, result)

            def do_GET(self):
                self._handle()

            def do_POST(self):
                self._handle()

        return H

    def start(self, host="127.0.0.1", port=0):
        """Bind localhost only. port=0 picks an ephemeral port; the chosen
        port is written to run/control.port. Never bind 0.0.0.0."""
        if host != "127.0.0.1":
            raise ValueError("control interface must bind 127.0.0.1 only")
        self._httpd = HTTPServer((host, port), self._handler())
        actual = self._httpd.server_address[1]
        with open(os.path.join(self.cfg["base_dir"], "run", "control.port"),
                  "w") as f:
            f.write(str(actual))
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        daemon=True)
        self._thread.start()
        self.journal("CONTROL_STARTED", host=host, port=actual)
        return actual

    def stop(self):
        if self._httpd:
            self._httpd.shutdown()
            self._httpd = None
        self.journal("CONTROL_STOPPED")
