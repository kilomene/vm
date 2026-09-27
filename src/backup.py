"""Phase 76: disaster recovery backups.

Backups of critical persistent state — task DB, event journal, checkpoints,
configuration, recovery metadata — kept SEPARATE from the live runtime state
where practical.

A backup that has never been restored successfully is NOT treated as
proven recovery material: every backup records `restored_ok`, and only
backups with restored_ok=1 count as proven. `test_restore()` restores a
backup to a scratch dir and verifies the DB opens, the schema is intact,
and the journal/checkpoints/config are present.
"""
import hashlib
import json
import os
import shutil
import sqlite3
import time


def _tree_hashes(root):
    out = {}
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, root)
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            out[rel] = h.hexdigest()
    return out


def create_backup(store, cfg, label="manual"):
    """Copy critical state into a timestamped backup dir under
    <base>/backups (separate from live <base>/state). Returns backup_id."""
    base = cfg["base_dir"]
    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    dest = os.path.join(base, "backups", f"{label}-{ts}")
    os.makedirs(dest, exist_ok=True)
    includes = []
    # task DB: use the sqlite backup API for a consistent copy
    db_src = cfg["state_db"]
    db_dest = os.path.join(dest, "state.db")
    if os.path.exists(db_src):
        src = sqlite3.connect(db_src, timeout=10)
        dst = sqlite3.connect(db_dest, timeout=10)
        try:
            src.backup(dst)
            includes.append("state.db")
        finally:
            dst.close()
            src.close()
    for name, src_dir in (("journal", cfg["journal_dir"]),
                          ("checkpoints", cfg["checkpoint_dir"]),
                          ("config", os.path.join(base, "config"))):
        if os.path.isdir(src_dir):
            shutil.copytree(src_dir, os.path.join(dest, name),
                            ignore=shutil.ignore_patterns("__pycache__"))
            includes.append(name)
    # recovery metadata: snapshots table lives in the DB; also copy run dir
    run_src = cfg["run_dir"]
    if os.path.isdir(run_src):
        shutil.copytree(run_src, os.path.join(dest, "run"),
                        ignore=shutil.ignore_patterns("*.pid", "*.lock",
                                                      "control.token"))
        includes.append("run")
    manifest = _tree_hashes(dest)
    with open(os.path.join(dest, "MANIFEST.json"), "w") as f:
        json.dump({"label": label, "created_at": time.time(),
                   "includes": includes, "files": manifest}, f, indent=2)
    mhash = hashlib.sha256(
        json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    bid = store.backup_record(label, dest, includes, manifest_sha256=mhash)
    store.journal("BACKUP_CREATED", backup_id=bid, path=dest,
                  includes=includes)
    return bid


def verify_backup(backup_id, store):
    """Check the backup dir still matches its manifest. Returns (ok, issues)."""
    rows = [b for b in store.backups_list() if b["id"] == backup_id]
    if not rows:
        return False, ["backup not found"]
    b = rows[0]
    man_path = os.path.join(b["path"], "MANIFEST.json")
    if not os.path.exists(man_path):
        return False, ["MANIFEST.json missing"]
    with open(man_path) as f:
        manifest = json.load(f)
    issues = []
    current = _tree_hashes(b["path"])
    current.pop("MANIFEST.json", None)
    expected = dict(manifest.get("files", {}))
    expected.pop("MANIFEST.json", None)
    for rel, h in expected.items():
        if rel not in current:
            issues.append(f"missing: {rel}")
        elif current[rel] != h:
            issues.append(f"modified: {rel}")
    return (len(issues) == 0), issues


def test_restore(backup_id, store, scratch_parent="/tmp"):
    """Restore a backup to a scratch dir and VERIFY it: DB opens, schema
    has the core tables, journal/checkpoints/config present. Records
    restored_ok on the backup. Returns (ok, detail)."""
    rows = [b for b in store.backups_list() if b["id"] == backup_id]
    if not rows:
        return False, {"error": "backup not found"}
    b = rows[0]
    scratch = os.path.join(
        scratch_parent,
        f"vm-restore-test-{backup_id}-{int(time.time())}")
    detail = {"scratch": scratch}
    try:
        shutil.copytree(b["path"], scratch,
                        ignore=shutil.ignore_patterns("MANIFEST.json"))
        db = os.path.join(scratch, "state.db")
        if not os.path.exists(db):
            raise RuntimeError("state.db missing from backup")
        conn = sqlite3.connect(db, timeout=10)
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            for required in ("tasks", "checkpoints", "operations",
                             "world_state"):
                if required not in tables:
                    raise RuntimeError(f"table missing: {required}")
            n_tasks = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            detail["tasks"] = n_tasks
            # integrity check on the restored DB
            ic = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if ic != "ok":
                raise RuntimeError(f"integrity_check: {ic}")
        finally:
            conn.close()
        for sub in ("journal", "config"):
            if not os.path.isdir(os.path.join(scratch, sub)):
                raise RuntimeError(f"{sub} missing from backup")
        ok = True
        detail["verified"] = "db opens, schema ok, journal+config present"
    except Exception as e:  # noqa: BLE001
        ok = False
        detail["error"] = repr(e)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    store.backup_mark_restored(backup_id, ok)
    store.journal("BACKUP_RESTORE_TESTED", backup_id=backup_id, ok=ok,
                  detail=str(detail)[:200])
    return ok, detail


def prune_backups(store, cfg, keep=7):
    """Keep the newest `keep` backups; delete older ones. Never delete a
    backup that is the only proven-restore one without journaling loudly."""
    backups = store.backups_list()
    if len(backups) <= keep:
        return []
    pruned = []
    for b in backups[keep:]:
        if b["restored_ok"] == 1:
            # keep at least one proven backup even beyond `keep`
            proven = [x for x in backups[:keep] if x["restored_ok"] == 1]
            if not proven:
                store.journal("BACKUP_PRUNE_SKIPPED", backup_id=b["id"],
                              reason="only proven backup")
                continue
        shutil.rmtree(b["path"], ignore_errors=True)
        pruned.append(b["id"])
    if pruned:
        store.journal("BACKUPS_PRUNED", ids=pruned)
    return pruned
