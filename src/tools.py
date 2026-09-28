"""Tool execution layer: the runtime executes actions, never the LLM's word.

Every tool call runs in a subprocess (or controlled in-process call) with:
- an operation-specific timeout (hang detection needs this)
- captured exit code / stdout / stderr
- a result record the verifier inspects

Supported tools: shell, write_file, read_file, http_get, mkdir.
The `browser` tool is a declared slot: without a host-provided driver it
returns UNAVAILABLE rather than faking a result.
"""
import json
import os
import subprocess
import time
import urllib.request

from . import secrets as secretsmod


class ToolResult:
    def __init__(self, ok, exit_code=None, stdout="", stderr="",
                 timed_out=False, tool=None, op=None):
        self.ok = ok
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        self.tool = tool
        self.op = op
        self.observed_at = time.time()

    def to_dict(self):
        return {"ok": self.ok, "exit_code": self.exit_code,
                "stdout": self.stdout[-4000:], "stderr": self.stderr[-4000:],
                "timed_out": self.timed_out, "tool": self.tool, "op": self.op,
                "observed_at": self.observed_at}


class Executor:
    def __init__(self, cfg, journal=None):
        self.cfg = cfg
        self.journal = journal
        self.timeouts = cfg.get("tool_timeouts", {})
        self.default_timeout = cfg.get("tool_timeout_default_s", 120)
        self.timeout_max = cfg.get("tool_timeout_max_s", 3600)

    def _sane_timeout(self, t):
        """Clamp a timeout to something meaningful.

        Non-positive or non-numeric values are misconfiguration (0 would
        insta-kill every command; None disables the timeout entirely and
        hangs forever): fall back to the configured default, itself
        guarded to a sane 120s floor. Values above tool_timeout_max_s
        (spec-supplied or configured) are clamped to the max: a runaway
        timeout is as bad as no timeout.
        """
        default = self.default_timeout
        if not isinstance(default, (int, float)) or default <= 0:
            default = 120
        if not isinstance(t, (int, float)) or t <= 0:
            return default
        max_t = self.timeout_max
        if not isinstance(max_t, (int, float)) or max_t <= 0:
            max_t = 3600
        return min(t, max_t)

    def _timeout_for(self, tool, op=None):
        if op and op in self.timeouts:
            return self._sane_timeout(self.timeouts[op])
        return self._sane_timeout(
            self.timeouts.get(tool, self.default_timeout))

    def _log(self, event, **kw):
        if self.journal:
            self.journal(event, **kw)

    def run(self, tool, op="run", args=None, task_id=None,
            redact_values=None):
        """Execute one tool call. Returns ToolResult (observed fact).

        redact_values: raw secret values substituted into args for this
        call — redacted (by value) from the journaled args and from the
        result's stdout/stderr before they reach the journal or the
        caller.
        """
        args = args or {}
        values = list(redact_values or [])
        timeout = self._timeout_for(tool, op if tool == "shell" else None)
        if "timeout_s" in args:
            timeout = self._sane_timeout(args["timeout_s"])
        self._log("TOOL_STARTED", tool=tool, op=op,
                  args=self._redact(args, values))
        t0 = time.time()
        try:
            if tool == "shell":
                res = self._shell(args.get("command", ""), args.get("cwd"),
                                  timeout)
            elif tool == "write_file":
                res = self._write_file(args.get("path"), args.get("content", ""),
                                       args.get("mode", "overwrite"))
            elif tool == "read_file":
                res = self._read_file(args.get("path"))
            elif tool == "mkdir":
                res = self._mkdir(args.get("path"))
            elif tool == "http_get":
                res = self._http_get(args.get("url"), timeout)
            elif tool == "browser":
                res = ToolResult(ok=False, stderr="browser tool unavailable:"
                                 " no host driver wired in")
            else:
                res = ToolResult(ok=False, stderr=f"unknown tool: {tool}")
        except Exception as e:  # noqa: BLE001 - executor must not crash
            res = ToolResult(ok=False, stderr=f"executor exception: {e!r}")
        res.tool, res.op = tool, op
        # Redact before anything journaled or returned: the raw values
        # must not reach the journal, last_err, last_verified_result,
        # or the verifier's tails.
        res.stdout = secretsmod.redact_values(res.stdout, values)
        res.stderr = secretsmod.redact_values(res.stderr, values)
        self._log("TOOL_FINISHED", tool=tool, op=op,
                  ok=res.ok, exit_code=res.exit_code,
                  timed_out=res.timed_out,
                  duration_s=round(time.time() - t0, 2))
        return res

    # ---- tool implementations ----
    def _shell(self, command, cwd, timeout):
        if not command:
            return ToolResult(ok=False, stderr="empty command")
        # Defense in depth: never hand subprocess a falsy/None timeout —
        # None means "wait forever".
        timeout = self._sane_timeout(timeout)
        try:
            p = subprocess.run(command, shell=True, capture_output=True,
                               text=True, timeout=timeout, cwd=cwd or None)
            return ToolResult(ok=p.returncode == 0, exit_code=p.returncode,
                              stdout=p.stdout, stderr=p.stderr)
        except subprocess.TimeoutExpired as e:
            return ToolResult(ok=False, timed_out=True,
                              stdout=e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or ""),
                              stderr=f"timed out after {timeout}s")

    def _write_file(self, path, content, mode):
        try:
            if not path:
                return ToolResult(ok=False, stderr="no path")
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            if mode == "append":
                with open(path, "a") as f:
                    f.write(content)
            else:
                if os.path.exists(path) and mode == "create_new":
                    return ToolResult(ok=False, stderr="file exists")
                with open(path, "w") as f:
                    f.write(content)
            return ToolResult(ok=True, stdout=f"wrote {len(content)} bytes to {path}")
        except OSError as e:
            return ToolResult(ok=False, stderr=str(e))

    def _read_file(self, path):
        try:
            with open(path) as f:
                data = f.read()
            return ToolResult(ok=True, stdout=data[:20000])
        except OSError as e:
            return ToolResult(ok=False, stderr=str(e))

    def _mkdir(self, path):
        try:
            os.makedirs(path, exist_ok=True)
            return ToolResult(ok=True, stdout=f"ok: {path}")
        except OSError as e:
            return ToolResult(ok=False, stderr=str(e))

    def _http_get(self, url, timeout):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "vm-agent/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read(200000).decode("utf-8", "replace")
                return ToolResult(ok=200 <= r.status < 300,
                                  exit_code=r.status, stdout=body)
        except Exception as e:  # noqa: BLE001
            return ToolResult(ok=False, stderr=f"http error: {e!r}")

    def _redact(self, args, values=None):
        """Name-based redaction plus value-based redaction of known raw
        secret values (so a secret under 'content' or 'command' is still
        caught)."""
        red = {}
        for k, v in args.items():
            kl = k.lower()
            if any(s in kl for s in ("token", "secret", "password", "key", "auth")):
                red[k] = "***REDACTED***"
            elif k == "content" and isinstance(v, str) and len(v) > 500:
                red[k] = f"<{len(v)} chars>"
            elif isinstance(v, str):
                red[k] = secretsmod.redact_values(v, values)
            else:
                red[k] = v
        return red
