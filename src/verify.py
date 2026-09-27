"""Verification layer: the verifier — not the LLM — decides success.

A step's `verify` block declares observable expectations. The verifier runs
checks against the real world and returns PASS/FAIL with evidence.

Verified state (world_state in the kv store) can only be written by the
verifier, never by agent claims.
"""
import json
import os
import socket


class Verdict:
    def __init__(self, passed, checks=None, evidence=None):
        self.passed = passed
        self.checks = checks or []
        self.evidence = evidence or {}

    def to_dict(self):
        return {"passed": self.passed, "checks": self.checks,
                "evidence": self.evidence}


class Verifier:
    def __init__(self, executor):
        self.ex = executor

    def verify_step(self, step):
        """step: dict with optional 'verify' list. Returns Verdict."""
        checks = step.get("verify", [])
        results, evidence = [], {}
        for c in checks:
            kind = c.get("check")
            try:
                if kind == "file_exists":
                    ok, ev = self._file_exists(c["path"])
                elif kind == "file_contains":
                    ok, ev = self._file_contains(c["path"], c["text"])
                elif kind == "command_ok":
                    ok, ev = self._command_ok(c["command"], c.get("cwd"))
                elif kind == "port_listening":
                    ok, ev = self._port_listening(c.get("host", "127.0.0.1"),
                                                  c["port"])
                elif kind == "http_ok":
                    ok, ev = self._http_ok(c["url"], c.get("contains"))
                elif kind == "process_running":
                    ok, ev = self._process_running(c["pattern"])
                else:
                    ok, ev = False, {"error": f"unknown check: {kind}"}
            except Exception as e:  # noqa: BLE001 - a check must not crash verify
                ok, ev = False, {"error": repr(e)}
            results.append({"check": kind, "passed": ok, **c})
            evidence[kind] = ev
        passed = all(r["passed"] for r in results) if results else True
        return Verdict(passed, results, evidence)

    # ---- checks (all read-only observations) ----
    @staticmethod
    def _file_exists(path):
        ok = os.path.exists(path)
        return ok, {"path": path, "exists": ok}

    @staticmethod
    def _file_contains(path, text):
        try:
            with open(path) as f:
                data = f.read()
            ok = text in data
            return ok, {"path": path, "found": ok}
        except OSError as e:
            return False, {"path": path, "error": str(e)}

    def _command_ok(self, command, cwd):
        r = self.ex.run("shell", args={"command": command, "cwd": cwd,
                                       "timeout_s": 60})
        ok = r.ok
        return ok, {"command": command, "exit_code": r.exit_code,
                    "tail": (r.stdout or "")[-500:]}

    @staticmethod
    def _port_listening(host, port):
        s = socket.socket()
        s.settimeout(5)
        try:
            s.connect((host, port))
            return True, {"host": host, "port": port, "listening": True}
        except OSError:
            return False, {"host": host, "port": port, "listening": False}
        finally:
            s.close()

    def _http_ok(self, url, contains=None):
        r = self.ex.run("http_get", args={"url": url, "timeout_s": 30})
        ok = r.ok and (contains is None or contains in (r.stdout or ""))
        return ok, {"url": url, "status": r.exit_code,
                    "contains_match": True if contains is None else (contains in (r.stdout or ""))}

    def _process_running(self, pattern):
        r = self.ex.run("shell", args={
            "command": f"ps aux | grep -F '{pattern}' | grep -v grep | head -3",
            "timeout_s": 15})
        ok = bool((r.stdout or "").strip())
        return ok, {"pattern": pattern, "running": ok,
                    "sample": (r.stdout or "")[:300]}
