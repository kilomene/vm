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
import os
import re

PROTECTED_PATTERNS = [
    # Power actions as actual commands (start of string or after shell
    # operators, quotes, or newlines — not substrings inside paths).
    # Covers: shutdown, /sbin/shutdown -h now, sudo /sbin/reboot,
    # env shutdown now, (shutdown now), bash -c 'shutdown now',
    # newline-separated commands.
    r"(^|[;&|(\n'\"]\s*|\bsudo\s+|\benv\s+)"
    r"(\S*/)?(shutdown|reboot|halt|poweroff)\b",
    # systemctl power actions and killing/disabling our own unit
    r"systemctl\s+(poweroff|reboot|halt|kill)\b",
    r"systemctl\s+(disable|stop|mask|kill)\s+vm-agent",
    # classic init runlevels
    r"(^|[;&|(\n]\s*|\bsudo\s+)\binit\s+[06]\b",
    r"rm\s+.*state\.db", r"rm\s+-rf?\s+.*vm-agent/state",
    r"rm\s+-rf?\s+.*vm-agent/checkpoints",
    # state/db destruction beyond rm
    r"\bfind\b.*vm-agent/state\b.*-delete\b",
    r"\bmv\b.*vm-agent/state\b",
    r"\btruncate\b.*state\.db",
    # writes to the installed policy source itself
    r"(^|[;&|]\s*)(sed|ed|tee|printf|echo)\b.*lib/vmagent/policy\.py",
    r">\s*.*lib/vmagent/policy\.py",
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

    def authorize_path(self, base_dir, path):
        """Policy for the write_file/mkdir tools: refuse paths under the
        install prefix's protected trees (state, lib/vmagent, config,
        run). The path is resolved with os.path.realpath first, so
        symlinks and '..' traversals cannot escape the check. Relative
        paths are evaluated against both the process cwd and base_dir.
        Returns (allowed: bool, reason: str)."""
        if not path:
            return True, "no path"
        base = os.path.realpath(base_dir) if base_dir else ""
        if not base:
            return True, "no base_dir to evaluate against"
        protected = [os.path.join(base, d)
                     for d in ("state", "lib/vmagent", "config", "run")]
        candidates = [os.path.realpath(path)]
        if not os.path.isabs(path):
            candidates.append(os.path.realpath(os.path.join(base, path)))
        for full in candidates:
            for pdir in protected:
                if full == pdir or full.startswith(pdir + os.sep):
                    return False, (
                        f"PROTECTED path refused: {path} "
                        f"(under {os.path.relpath(pdir, base)})")
        return True, "SAFE"
