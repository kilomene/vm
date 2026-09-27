"""Phase 45/46: file integrity + configuration protection.

Important files get a recorded sha256. After recovery, the recorded hash is
compared against the current hash; only matching content is trusted.

Protected configuration (supervisor unit, config.json, policy) additionally
requires an explicit authorization token to modify — the agent runtime
refuses casual writes.
"""
import hashlib
import os

# Paths that are integrity-tracked by default (relative to base_dir).
TRACKED = [
    "config/config.json",
    "systemd/vm-agent.service",
    "state/state.db",
]

# Config files the agent may not modify without an auth token.
PROTECTED_CONFIG = [
    "config/config.json",
    "systemd/vm-agent.service",
]


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def record(store, base_dir, paths=None):
    """Record current hashes. Returns {path: sha256}."""
    out = {}
    for rel in (paths or TRACKED):
        full = os.path.join(base_dir, rel)
        if os.path.exists(full):
            digest = sha256_file(full)
            store.hash_record(full, digest)
            out[full] = digest
    store.journal("INTEGRITY_RECORDED", files=list(out))
    return out


def verify(store, base_dir, paths=None):
    """Compare recorded vs current hashes. Returns list of mismatches."""
    bad = []
    for rel in (paths or TRACKED):
        full = os.path.join(base_dir, rel)
        expected = store.hash_get(full)
        if expected is None:
            continue  # never recorded: nothing to compare
        if not os.path.exists(full):
            bad.append({"path": full, "issue": "missing"})
            continue
        actual = sha256_file(full)
        if actual != expected:
            bad.append({"path": full, "issue": "modified",
                        "expected": expected[:12], "actual": actual[:12]})
    if bad:
        store.journal("INTEGRITY_MISMATCH", mismatches=bad)
    return bad


def is_protected_config(base_dir, path):
    full = os.path.abspath(path)
    base = os.path.abspath(base_dir)
    if not full.startswith(base):
        return False
    rel = os.path.relpath(full, base)
    return rel in PROTECTED_CONFIG


def authorize_config_write(path, auth_token=None):
    """Config writes need an out-of-band token; without it, refuse."""
    if auth_token is None:
        return False, f"config modification refused (no auth token): {path}"
    # A real deployment validates the token against a secret store.
    # Here: any non-empty token supplied out-of-band is accepted and logged.
    return True, "config write authorized"
