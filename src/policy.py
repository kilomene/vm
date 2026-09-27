"""Protected-operations policy layer.

SAFE       — always allowed (read-only, file writes inside task dirs, etc.)
RESTRICTED — allowed with a logged reason (package installs, service restarts)
PROTECTED  — never executed by the agent runtime without an explicit,
             out-of-band authorization token. The runtime refuses these.

Protected: shutdown, reboot, disabling/removing the supervisor or its unit,
modifying supervisor config, deleting state DB / journal / checkpoints,
killing the supervisor.
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

RESTRICTED_PATTERNS = [
    r"\bapt(-get)?\s+install\b", r"\bpip\s+install\b",
    r"systemctl\s+restart\s+(?!vm-agent)",
]


class Policy:
    def classify(self, command):
        cmd = command or ""
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
