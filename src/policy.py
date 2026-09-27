"""Protected-operations policy layer.

SAFE       — always allowed (read-only, file writes inside task dirs, etc.)
RESTRICTED — allowed with a logged reason (package installs, service restarts)
PROTECTED  — never executed by the agent runtime without an explicit,
             out-of-band authorization token. The runtime refuses these.

Protected: shutdown, reboot, disabling/removing the supervisor or its unit,
modifying supervisor config, deleting state DB / journal / checkpoints,
killing the supervisor.

Phase 78 self-protection: the recovery infrastructure, logs, monitoring,
configuration, and recovery history are protected from modification; safety
features cannot be disabled; recovery mechanisms cannot be exploited to
perform unauthorized actions (e.g. granting oneself capabilities through the
DB, or invoking recovery tooling to bypass policy).
"""
import re

PROTECTED_PATTERNS = [
    # Match as actual commands (start of string or after shell operators),
    # not as substrings inside file paths.
    r"(^|[;&|]\s*|\bsudo\s+)shutdown\b", r"(^|[;&|]\s*|\bsudo\s+)reboot\b",
    r"(^|[;&|]\s*|\bsudo\s+)halt\b", r"(^|[;&|]\s*|\bsudo\s+)poweroff\b",
    r"systemctl\s+(disable|stop|mask)\s+vm-agent",
    r"rm\s+.*state\.db", r"rm\s+-rf?\s+.*vm-agent/state",
    r"rm\s+-rf?\s+.*vm-agent/checkpoints",
    r"kill.*supervisor",
]

# Phase 78: self-protection of the recovery infrastructure itself.
SELF_PROTECTION_PATTERNS = [
    # deleting logs / monitoring / backups / recovery history
    r"rm\s+-rf?\s+.*vm-agent/logs",
    r"rm\s+-rf?\s+.*vm-agent/backups",
    r"rm\s+-rf?\s+.*vm-agent/state/journal",
    r">\s*.*vm-agent/logs/",           # truncating a log
    r"logrotate\s+.*-f.*vm-agent",     # forced rotation of agent logs
    # modifying safety/recovery source or config
    r"(^|[;&|]\s*)(sed|ed|tee|printf|echo)\b.*src/(policy|integrity|"
    r"supervisor|recovery|caps|secrets|matrix|faultinject)\.py",
    r">\s*.*vm-agent/src/(policy|integrity|supervisor|recovery|caps|"
    r"secrets|matrix)\.py",
    r">\s*.*vm-agent/config/",
    # disabling safety features
    r"systemctl\s+(disable|stop|mask)\s+\S*watch",
    r"crontab\s+-r\b",
    r"rm\s+.*vm-agent\.service",
    r"kill\s+.*-9.*supervisor",
    r"pkill.*supervisor",
    # exploiting recovery mechanisms for unauthorized actions
    r"sqlite3?\s+.*state\.db.*\b(UPDATE|DELETE|INSERT)\b.*\b(capabilities|"
    r"operations|locks|leases)\b",   # self-granting capabilities / locks
    r"\bvm\b.*\brecover\b.*--force",  # forced recovery bypass
    r"\bvm\b.*\bgrant-cap\b",         # CLI capability grant by the agent
    # escaping the runtime home to run outside supervision
    r"VM_AGENT_HOME\s*=",
    r"chmod\s+[0-7]*777\s+.*vm-agent/run",
]

RESTRICTED_PATTERNS = [
    r"\bapt(-get)?\s+install\b", r"\bpip\s+install\b",
    r"systemctl\s+restart\s+(?!vm-agent)",
]


def self_protection_check(command):
    """Phase 78: does this command attack the recovery infrastructure,
    disable a safety feature, or exploit a recovery mechanism?
    Returns (is_protected: bool, reason: str)."""
    cmd = command or ""
    for p in SELF_PROTECTION_PATTERNS:
        if re.search(p, cmd):
            return True, f"self-protection: {p}"
    return False, ""


class Policy:
    def classify(self, command):
        cmd = command or ""
        protected, _why = self_protection_check(cmd)
        if protected:
            return "PROTECTED"
        for p in PROTECTED_PATTERNS:
            if re.search(p, cmd):
                return "PROTECTED"
        for p in RESTRICTED_PATTERNS:
            if re.search(p, cmd):
                return "RESTRICTED"
        return "SAFE"

    def authorize(self, command, auth_token=None):
        """Returns (allowed: bool, reason: str)."""
        level = self.classify(command)
        if level == "PROTECTED":
            # Out-of-band token lives outside the agent's reachable files;
            # without it the runtime refuses. Never auto-approve.
            return False, f"PROTECTED operation refused: {command[:80]}"
        if level == "RESTRICTED":
            return True, "RESTRICTED — executed with audit log entry"
        return True, "SAFE"
