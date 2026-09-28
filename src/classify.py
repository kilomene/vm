"""Phase 54: deterministic retry classification.

Every failure is classified into exactly one of:
  TRANSIENT      - temporary; safe to retry with backoff
  RETRYABLE      - failure of a component; retry with bounded attempts
  PERMANENT      - will not succeed on retry (bad input, missing resource)
  POLICY_BLOCKED - refused by policy; never retried
  HUMAN_REQUIRED - needs a person; never retried automatically

Retry decisions consult this module — never blind or unlimited retries.
The classification table doubles as the static core of the recovery matrix
(phase 79), which the RecoveryManager enforces at runtime.
"""
import re

TRANSIENT = "TRANSIENT"
RETRYABLE = "RETRYABLE"
PERMANENT = "PERMANENT"
POLICY_BLOCKED = "POLICY_BLOCKED"
HUMAN_REQUIRED = "HUMAN_REQUIRED"

# (source, kind) -> (class, max_retries, base_backoff_s, note)
RULES = {
    # network kinds (from net.py)
    ("net", "dns_failure"): (TRANSIENT, 5, 2, "transient: backoff and retry"),
    ("net", "no_internet"): (TRANSIENT, 8, 5, "transient: wait for connectivity"),
    ("net", "connection_timeout"): (TRANSIENT, 5, 2, "transient: backoff"),
    ("net", "tls_failure"): (RETRYABLE, 3, 5, "maybe transient (clock/proxy)"),
    ("net", "rate_limited"): (TRANSIENT, 6, 10, "honor backoff; never hammer"),
    ("net", "remote_server_error"): (TRANSIENT, 4, 5, "5xx: backoff and retry"),
    ("net", "auth_failure"): (PERMANENT, 0, 0, "credential wrong: fix, don't retry"),
    ("net", "local_firewall"): (HUMAN_REQUIRED, 0, 0, "needs human: firewall change"),
    ("net", "unknown"): (RETRYABLE, 3, 5, "cautious bounded retry"),
    # model failure kinds (from model.py)
    ("model", "timeout"): (TRANSIENT, 3, 5, "transient model timeout"),
    ("model", "rate_limit"): (TRANSIENT, 5, 10, "backoff, then retry"),
    ("model", "auth_failure"): (PERMANENT, 0, 0, "API key wrong: needs human"),
    ("model", "malformed_response"): (RETRYABLE, 2, 2, "re-ask once or twice"),
    ("model", "model_unavailable"): (TRANSIENT, 4, 10, "capacity issue: wait"),
    ("model", "context_limit"): (HUMAN_REQUIRED, 0, 0, "handled by context recovery"),
    ("model", "inference_failure"): (RETRYABLE, 2, 5, "bounded retry"),
    # verifier / tool kinds
    ("tool", "timeout"): (RETRYABLE, 2, 5, "tool hung: retry bounded"),
    ("tool", "crash"): (RETRYABLE, 2, 2, "worker crash: retry bounded"),
    ("tool", "exit_nonzero"): (RETRYABLE, 1, 2, "one retry, then diagnose"),
    ("tool", "invalid_command"): (PERMANENT, 0, 0, "fix the command, don't retry"),
    ("tool", "missing_resource"): (PERMANENT, 0, 0, "resource absent: fix first"),
    ("tool", "unsupported"): (PERMANENT, 0, 0, "operation not supported"),
    # dependency kinds
    ("dep", "missing"): (RETRYABLE, 2, 5, "attempt safe repair, bounded"),
    ("dep", "corrupted"): (RETRYABLE, 1, 5, "one repair attempt"),
    ("dep", "version_mismatch"): (HUMAN_REQUIRED, 0, 0, "needs human decision"),
    # policy
    ("policy", "protected"): (POLICY_BLOCKED, 0, 0, "never retried"),
    ("policy", "restricted"): (RETRYABLE, 1, 2, "allowed with audit; one retry"),
    ("policy", "capability_denied"): (POLICY_BLOCKED, 0, 0, "needs capability grant"),
    # browser kinds
    ("browser", "browser_crash"): (RETRYABLE, 3, 5, "restart browser, bounded"),
    ("browser", "session_invalid"): (RETRYABLE, 2, 3, "restore session"),
    ("browser", "page_timeout"): (TRANSIENT, 3, 3, "transient page issue"),
    ("browser", "navigation_failure"): (RETRYABLE, 2, 3, "retry navigation"),
    ("browser", "driver_failure"): (RETRYABLE, 2, 5, "restart driver"),
    ("browser", "auth_expired"): (HUMAN_REQUIRED, 0, 0, "re-login needed"),
    # process / lock / planner / progress / db / resource / config kinds
    ("process", "crash"): (RETRYABLE, 2, 5, "restart process, bounded"),
    ("process", "hang"): (RETRYABLE, 2, 5, "restart suspected-hung process"),
    ("lock", "deadlock"): (RETRYABLE, 2, 3, "break cycle least-destructively"),
    ("lock", "stale"): (RETRYABLE, 1, 2, "reap stale lock once"),
    ("planner", "stall"): (RETRYABLE, 2, 3, "rebuild plan from world state"),
    ("progress", "stall"): (RETRYABLE, 3, 3, "retry, then rebuild plan"),
    ("db", "corrupt"): (HUMAN_REQUIRED, 0, 0, "restore from backup, verify"),
    ("resource", "disk"): (RETRYABLE, 3, 5, "free safe space, bounded"),
    ("resource", "memory"): (RETRYABLE, 2, 5, "shed load, bounded"),
    ("config", "bad"): (RETRYABLE, 1, 2, "restore last-known-good config"),
    ("budget", "exhausted"): (HUMAN_REQUIRED, 0, 0, "needs human decision"),
    ("failure", "repeated"): (HUMAN_REQUIRED, 0, 0, "strategy exhausted"),
    # generic
    ("unknown", "unknown"): (RETRYABLE, 2, 5, "bounded default"),
}

_REPEAT_ESCALATION = {
    TRANSIENT: HUMAN_REQUIRED,
    RETRYABLE: HUMAN_REQUIRED,
    # a step that already burned its whole retry budget and then fails
    # again on the next run is not a flake: escalate, don't loop blindly.
    "retry_exhausted": HUMAN_REQUIRED,
}


def classify(source, kind):
    """Return (class, max_retries, base_backoff_s, note)."""
    return RULES.get((source, kind), RULES[("unknown", "unknown")])


def backoff_for(cls, attempt, base):
    if attempt < 0:
        return 0
    return min(base * (2 ** attempt), 300)


def should_retry(source, kind, attempt):
    """attempt is 0-based count of retries already used."""
    cls, max_r, base, _ = classify(source, kind)
    return attempt < max_r, cls


def backoff_sequence(source, kind):
    """Exact backoff schedule for a failure kind: base * 2^i per retry,
    matching the agent's retry loop (min(base * 2^attempt, 30))."""
    _cls, max_r, base, _ = classify(source, kind)
    return [base * (2 ** i) for i in range(max_r)]


# ---- free-text mapping for tool failures (used by the agent) ----
_TOOL_PATTERNS = [
    (r"timed out|timeout", "timeout"),
    (r"command not found|No such file or directory.*command", "invalid_command"),
    (r"No such file or directory", "missing_resource"),
    (r"Permission denied", "missing_resource"),
    (r"not supported|unsupported|unknown tool", "unsupported"),
]


def classify_tool_error(stderr, exit_code=None):
    text = str(stderr or "")
    for pat, kind in _TOOL_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            return classify("tool", kind)
    if exit_code == 127:
        # POSIX "command not found" (bash: "command not found"; dash:
        # "sh: 1: foo: not found"). Permanent by definition — never retry.
        return classify("tool", "invalid_command")
    if exit_code not in (None, 0):
        return classify("tool", "exit_nonzero")
    return classify("unknown", "unknown")


def escalate_on_repeat(cls):
    """When the same failure repeats past its retry budget, it stops being
    retryable and becomes a human matter. Never loop forever."""
    return _REPEAT_ESCALATION.get(cls, cls)
