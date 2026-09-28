"""Phase 60: secret isolation.

A dedicated secret-management boundary:

  - secrets never appear in task state, event journal, logs, model context,
    error messages, browser screenshots, or debug output
  - secrets are exposed to tools only when required, at execution time
  - tool output is redacted before it reaches logs or the model
  - accidental credential exposure is detected and journaled (loudly)

Design: secrets live in an in-memory vault (never persisted to SQLite or
the journal). Tools receive them via a controlled `SecretRef` placeholder
that is substituted at the last moment inside the executor and redacted
from every record.

Canonical store: the module-level `_VAULT` dict behind `vault_set` /
`vault_get` / `vault_drop` (scoped leases per task). The `SecretVault`
class below is a legacy alternate that is not used by the runtime;
`redact_text` consults both, but the runtime resolves and redacts through
the module-level store only.
"""
import os
import re

# Patterns that look like leaked credentials in free text.
EXPOSURE_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{16,}"),                       # openai-style
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),               # slack-style
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),                       # github pat
    re.compile(r"AKIA[0-9A-Z]{16}"),                           # aws key id
    re.compile(r"(?i)(api[_-]?key|secret|passwd|password)\s*[:=]\s*['\"]?([^\s'\"]{8,})"),
    re.compile(r"-----BEGIN (RSA |OPENSSH |EC )?PRIVATE KEY-----"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9\-._~+/]{16,}={0,2}"),
]

# Argument names that are always treated as secrets.
SECRET_ARG_NAMES = ("token", "secret", "password", "passwd", "api_key",
                    "apikey", "auth", "credential", "private_key",
                    "client_secret", "access_token", "refresh_token")


class SecretRef:
    """Placeholder for a secret. The real value is only substituted inside
    the executor at run time; str()/repr() never reveal it."""

    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return f"<SecretRef {self.name}>"

    def __str__(self):
        return f"<SecretRef {self.name}>"


class SecretVault:
    """In-memory only. Nothing here is ever written to disk, the DB, or
    the journal. Dies with the process."""

    def __init__(self, journal=None):
        self._secrets = {}
        self._journal = journal

    def put(self, name, value):
        self._secrets[name] = value
        if self._journal:
            self._journal("SECRET_STORED", name=name)

    def ref(self, name):
        if name not in self._secrets:
            raise KeyError(f"unknown secret: {name}")
        return SecretRef(name)

    def resolve(self, value):
        """Substitute SecretRefs at tool-execution time only."""
        if isinstance(value, SecretRef):
            return self._secrets.get(value.name, "")
        if isinstance(value, dict):
            return {k: self.resolve(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.resolve(v) for v in value]
        return value

    def names(self):
        return list(self._secrets)

    def drop(self, name):
        self._secrets.pop(name, None)


def redact_values(text, values):
    """Redact a caller-supplied list of raw secret values from free text.

    Used when the runtime knows exactly which values were substituted for
    this step (e.g. resolved {"vault": ...} refs) — value-based redaction
    catches secrets regardless of the argument NAME they traveled under
    ("content", "command", ...), which name-based redaction misses.
    """
    if not text:
        return text
    out = str(text)
    for val in values or []:
        v = str(val) if val is not None else ""
        if v and len(v) >= 4:
            out = out.replace(v, "***REDACTED***")
    return out


def redact_text(text, vault=None):
    """Redact known secret values and anything matching exposure patterns.

    The module-level vault (populated by vault_set, the canonical store
    the agent and CLI use) is always consulted: a secret stored via
    vault_set must be redacted even when the caller passes no explicit
    vault. An explicit SecretVault instance's values are redacted too.
    """
    if not text:
        return text
    out = str(text)
    values = []
    if vault is not None:
        for name in vault.names():
            values.append(vault._secrets.get(name))
    # canonical module-level vault: vault_set/vault_get/vault_drop
    for rec in _VAULT.values():
        values.append(rec.get("value"))
    for val in values:
        if val and len(val) >= 4:
            out = out.replace(val, "***REDACTED***")
    for pat in EXPOSURE_PATTERNS:
        out = pat.sub("***REDACTED***", out)
    return out


def is_secret_arg(name):
    n = name.lower()
    return any(s in n for s in SECRET_ARG_NAMES)


def redact_args(args, vault=None):
    red = {}
    for k, v in (args or {}).items():
        if isinstance(v, SecretRef):
            red[k] = repr(v)
        elif is_secret_arg(k):
            red[k] = "***REDACTED***"
        elif isinstance(v, str) and len(v) > 500:
            red[k] = f"<{len(v)} chars>"
        else:
            red[k] = v
    return red


def scan_for_exposure(text, context=""):
    """Detect accidental credential exposure. Returns list of findings."""
    findings = []
    for pat in EXPOSURE_PATTERNS:
        for m in pat.finditer(str(text or "")):
            findings.append({"pattern": pat.pattern[:40],
                             "context": context,
                             "sample": m.group(0)[:24] + "..."})
    return findings


def audit_output(text, journal, context=""):
    """Scan tool output for leaked secrets; journal loudly if found."""
    findings = scan_for_exposure(text, context)
    if findings and journal:
        journal("SECRET_EXPOSURE_DETECTED", context=context,
                findings=[{k: f[k] for k in ("pattern", "context")}
                          for f in findings])
    return findings


def scan_journal(journal_dir, days=1):
    """Phase 60: exposure detection — scan recent journal lines for
    secret-shaped values. Returns list of 'file:line: pattern' hits.
    Call this from diagnostics; investigate every hit."""
    import glob
    import time
    cutoff = time.time() - days * 86400
    hits = []
    for path in sorted(glob.glob(os.path.join(journal_dir, "*.jsonl"))):
        try:
            if os.path.getmtime(path) < cutoff - 86400:
                continue
        except OSError:
            continue
        try:
            with open(path) as f:
                for lineno, line in enumerate(f, 1):
                    if scan_for_exposure(line):
                        hits.append(f"{os.path.basename(path)}:{lineno}")
        except OSError:
            continue
    return hits


# ---- Phase 60: scoped, time-limited secret leases (process-local) ----
# Values live ONLY in this dict: never in SQLite, never in the journal,
# never in checkpoints. They die with the process. A task receives a
# secret only if it is the owner (or the secret is global "*"), and only
# while its lease has not expired.
import time as _time

_VAULT = {}  # name -> {"value", "owner", "expires"}


def vault_set(store, name, value, owner="*", ttl_s=None):
    """Store a secret with an owner scope and optional lease (seconds).
    Only the name is journaled — never the value."""
    expires = (_time.time() + ttl_s) if ttl_s is not None else None
    _VAULT[name] = {"value": value, "owner": owner, "expires": expires}
    try:
        store.journal("SECRET_STORED", name=name, owner=owner,
                      ttl_s=ttl_s)
    except Exception:  # noqa: BLE001 - journaling must not break vault use
        pass
    return True


def vault_get(store, task_id, name):
    """Retrieve a secret if the task is authorized and the lease is valid.
    Returns None otherwise (unknown, wrong owner, or expired)."""
    rec = _VAULT.get(name)
    if not rec:
        return None
    if rec["owner"] not in ("*", task_id):
        return None
    if rec["expires"] is not None and _time.time() >= rec["expires"]:
        _VAULT.pop(name, None)
        return None
    return rec["value"]


def vault_drop(store, name, task_id):
    """Revoke a secret (owner or global scope only)."""
    rec = _VAULT.get(name)
    if rec and rec["owner"] in ("*", task_id):
        _VAULT.pop(name, None)
        try:
            store.journal("SECRET_REVOKED", name=name)
        except Exception:  # noqa: BLE001
            pass
        return True
    return False
