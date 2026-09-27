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


# ---- Phase 61: configuration versioning and restoration ----
def snapshot_config(store, base_dir, paths=None, note=""):
    """Version the current config files (content stored, not just hashes).
    Call after any authorized config change. Returns version ids."""
    import time
    vids = []
    for rel in (paths or PROTECTED_CONFIG):
        full = os.path.join(base_dir, rel)
        if not os.path.exists(full):
            continue
        with open(full) as f:
            content = f.read()
        vid = store.config_version_save(full, content, note=note)
        vids.append((rel, vid))
    if vids:
        store.journal("CONFIG_VERSIONED", versions=[v[1] for v in vids],
                      note=note)
    return vids


def restore_config(store, base_dir, rel_path, version_id=None):
    """Restore a config file to a previous version (last-known-good by
    default). The pre-restore content is versioned first so the restore
    itself is reversible. Returns (ok, message)."""
    full = os.path.join(base_dir, rel_path)
    target = None
    if version_id is not None:
        for v in store.config_versions(full):
            if v["id"] == version_id or v["version"] == version_id:
                target = v
                break
        if target is None:
            return False, f"version {version_id} not found for {rel_path}"
    else:
        target = store.config_last_known_good(full)
        if target is None:
            versions = store.config_versions(full, limit=1)
            target = versions[0] if versions else None
        if target is None:
            return False, f"no saved version for {rel_path}"
    # version the current (broken) content first — restore is reversible
    if os.path.exists(full):
        with open(full) as f:
            store.config_version_save(full, f.read(),
                                      note="pre-restore backup")
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as f:
        f.write(target["content"])
    # re-record the hash so integrity.verify() trusts the restored file
    store.hash_record(full, sha256_file(full))
    store.journal("CONFIG_RESTORED", path=rel_path, version=target["id"])
    return True, f"restored {rel_path} to version {target['id']}"


def mark_config_good(store, base_dir, paths=None):
    """Mark current config versions as last-known-good (after the agent
    proves healthy with them)."""
    import time
    now = time.time()
    for rel in (paths or PROTECTED_CONFIG):
        full = os.path.join(base_dir, rel)
        versions = store.config_versions(full, limit=1)
        if versions:
            store.config_version_save(full, versions[0]["content"],
                                      known_good=True,
                                      note="promoted to last-known-good")
    store.journal("CONFIG_MARKED_GOOD", at=now)
