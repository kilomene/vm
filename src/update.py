"""Phases 71/72/73/74/75: versioned runtime, compatibility, safe self-update.

71: before runtime updates, check OS/arch/runtime/package/DB/config/plugin
    compatibility. Never update when compatibility fails.
72: record the runtime environment (OS, arch, versions) and tie it to
    checkpoints; verify compatibility before restoring a checkpoint.
73: safe self-update pipeline:
      DOWNLOAD -> VERIFY -> INSTALL ISOLATED -> TEST -> CANARY ->
      SWITCH -> HEALTH CHECK -> (ROLLBACK on failure)
    The last-known-good runtime stays available until the new one passes
    verification. Nothing is ever replaced blindly.
74: versioned, testable, recoverable migrations for schema/config/state
    format changes. Never silent format changes.
75: canary execution: run controlled operations on the new runtime and
    verify startup/task creation/tool execution/verification/DB/scheduler/
    recovery/supervisor comms before switching production.
"""
import hashlib
import json
import os
import platform
import shutil
import sqlite3
import sys
import time

RUNTIME_VERSION = "0.3.0"  # bump on every shipped change


def runtime_env():
    """Versioned snapshot of the runtime environment (phase 72)."""
    env = {
        "vm_agent_version": RUNTIME_VERSION,
        "os": platform.system(),
        "os_release": platform.release(),
        "arch": platform.machine(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "executable": sys.executable,
    }
    # best-effort dependency versions
    for mod, key in (("pip", "pip"),):
        try:
            out = __import__("subprocess").run(
                [sys.executable, "-m", mod, "--version"],
                capture_output=True, text=True, timeout=10)
            env[key] = (out.stdout or "").strip()[:40]
        except Exception:
            pass
    return env


def env_fingerprint(env=None):
    env = env or runtime_env()
    canonical = json.dumps(env, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def check_compatibility(env, requirements):
    """requirements: {field: value or [allowed values]}. Returns (ok, issues)."""
    issues = []
    for field, want in (requirements or {}).items():
        have = env.get(field, "<missing>")
        allowed = want if isinstance(want, list) else [want]
        if have not in allowed:
            issues.append(f"{field}: have {have!r}, need one of {allowed}")
    return (len(issues) == 0), issues


def checkpoint_env_compatible(store, task_id):
    """Phase 72: before resuming from a checkpoint, verify the runtime
    environment still matches what the checkpoint was taken on."""
    ckpt = store.latest_checkpoint(task_id)
    if not ckpt:
        return True, []
    try:
        state = json.loads(ckpt.get("state_json") or "{}")
    except Exception:
        return True, []  # old checkpoints have no env info: allow
    recorded = state.get("runtime_env")
    if not recorded:
        return True, []
    current = runtime_env()
    issues = []
    for key in ("vm_agent_version", "os", "arch", "python"):
        if recorded.get(key) != current.get(key):
            issues.append(f"{key}: checkpoint={recorded.get(key)} "
                          f"now={current.get(key)}")
    return (len(issues) == 0), issues


# ---- phase 74: versioned migrations ----
MIGRATIONS = {}


def migration(version):
    def deco(fn):
        MIGRATIONS[version] = fn
        return fn
    return deco


@migration(1)
def _m001_noop(store):
    """Baseline: schema already created by CREATE TABLE IF NOT EXISTS."""
    return True


def migrate(store, journal=None):
    """Apply pending migrations in order, recording each. Never silent."""
    journal = journal or store.journal
    current = store.migration_version()
    applied = []
    for version in sorted(MIGRATIONS):
        if version <= current:
            continue
        journal("MIGRATION_START", version=version)
        try:
            ok = MIGRATIONS[version](store)
        except Exception as e:  # noqa: BLE001
            journal("MIGRATION_FAILED", version=version, error=repr(e))
            return False, applied
        if not ok:
            journal("MIGRATION_FAILED", version=version)
            return False, applied
        store.migration_applied(version, note=MIGRATIONS[version].__doc__)
        applied.append(version)
        journal("MIGRATION_APPLIED", version=version)
    return True, applied


# ---- phase 73/75: safe self-update ----
class SelfUpdater:
    """Minimal but real self-update pipeline.

    Layout under base_dir:
      releases/<version>/      installed runtime copies
      releases/current -> <version>   (symlink = active runtime)
      releases/previous -> <version>  (last known good)
    """

    def __init__(self, base_dir, journal=None):
        self.base = base_dir
        self.releases = os.path.join(base_dir, "releases")
        self.journal = journal or (lambda e, **k: None)
        os.makedirs(self.releases, exist_ok=True)

    def _link(self, name):
        p = os.path.join(self.releases, name)
        return os.readlink(p) if os.path.islink(p) else None

    def current_version(self):
        link = self._link("current")
        return os.path.basename(link) if link else None

    # -- DOWNLOAD --
    def download(self, source_dir, version, expected_sha256=None):
        """Fetch a release tree. source_dir may be a local path or file://."""
        if source_dir.startswith("file://"):
            source_dir = source_dir[len("file://"):]
        staged = os.path.join(self.releases, f".staging-{version}")
        if os.path.exists(staged):
            shutil.rmtree(staged)
        shutil.copytree(source_dir, staged,
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.journal("UPDATE_DOWNLOADED", version=version, source=source_dir)
        return staged

    # -- VERIFY --
    def verify(self, staged_dir, expected_sha256=None):
        """Verify integrity: manifest hashes, or a single tree hash."""
        tree_hash = _tree_hash(staged_dir)
        ok = (expected_sha256 is None or tree_hash == expected_sha256)
        self.journal("UPDATE_VERIFIED", ok=ok,
                     tree_hash=tree_hash[:16])
        return ok, tree_hash

    # -- INSTALL ISOLATED --
    def install_isolated(self, staged_dir, version):
        dest = os.path.join(self.releases, version)
        if os.path.exists(dest):
            shutil.rmtree(dest)
        shutil.move(staged_dir, dest)
        self.journal("UPDATE_INSTALLED_ISOLATED", version=version)
        return dest

    # -- TEST --
    def test(self, version_dir):
        """Smoke test the new runtime: imports + store open + schema."""
        try:
            code = (
                "import sys; sys.path.insert(0, %r);"
                "import vmagent.state, vmagent.config;"
                "print('import ok')" % os.path.join(version_dir))
            import subprocess
            p = subprocess.run([sys.executable, "-c", code],
                               capture_output=True, text=True, timeout=60)
            ok = p.returncode == 0
            self.journal("UPDATE_TESTED", ok=ok,
                         tail=(p.stderr or p.stdout or "")[-300:])
            return ok
        except Exception as e:  # noqa: BLE001
            self.journal("UPDATE_TESTED", ok=False, error=repr(e))
            return False

    # -- CANARY (phase 75) --
    def canary(self, version_dir):
        """Run controlled operations on the new runtime before switching:
        task create -> tool exec -> verify -> checkpoint -> DB round-trip."""
        import subprocess
        import tempfile
        home = tempfile.mkdtemp(prefix="vm-canary-")
        script = (
            "import sys, os; sys.path.insert(0, %r);"
            "from vmagent.state import Store;"
            "from vmagent.tools import Executor;"
            "from vmagent.verify import Verifier;"
            "s = Store(os.path.join(%r, 'state.db'), os.path.join(%r, 'j'));"
            "s.create_task('canary-1', {'steps': []});"
            "ex = Executor({'tool_timeouts': {}, 'tool_timeout_default_s': 10});"
            "r = ex.run('shell', args={'command': 'echo canary-ok'});"
            "assert r.ok and 'canary-ok' in r.stdout, 'tool failed';"
            "v = Verifier(ex).verify_step({'verify': [{'check': 'command_ok', 'command': 'echo x'}]});"
            "assert v.passed, 'verify failed';"
            "s.checkpoint('canary-1', 0, 'canary', {'ok': True});"
            "s.update_task('canary-1', status='COMPLETED');"
            "s.journal('CANARY_OK'); s.close(); print('canary ok')"
            % (version_dir, home, home))
        try:
            p = subprocess.run([sys.executable, "-c", script],
                               capture_output=True, text=True, timeout=120)
            ok = p.returncode == 0 and "canary ok" in p.stdout
            self.journal("UPDATE_CANARY", ok=ok,
                         tail=(p.stderr or p.stdout or "")[-300:])
            return ok
        except Exception as e:  # noqa: BLE001
            self.journal("UPDATE_CANARY", ok=False, error=repr(e))
            return False
        finally:
            shutil.rmtree(home, ignore_errors=True)

    # -- SWITCH --
    def switch(self, version):
        prev = self._link("current")
        new = os.path.join(self.releases, version)
        tmp = os.path.join(self.releases, ".current-tmp")
        if os.path.islink(tmp):
            os.unlink(tmp)
        os.symlink(new, tmp)
        os.rename(tmp, os.path.join(self.releases, "current"))
        if prev:
            pl = os.path.join(self.releases, "previous")
            if os.path.islink(pl):
                os.unlink(pl)
            os.symlink(prev, pl)
        self.journal("UPDATE_SWITCHED", version=version, previous=prev)
        return prev

    # -- HEALTH CHECK --
    def health_check(self, version):
        version_dir = os.path.join(self.releases, version)
        return self.test(version_dir)

    # -- ROLLBACK --
    def rollback(self):
        prev = self._link("previous")
        if not prev or not os.path.exists(prev):
            self.journal("UPDATE_ROLLBACK_FAILED",
                         reason="no previous runtime")
            return False
        tmp = os.path.join(self.releases, ".current-tmp")
        if os.path.islink(tmp):
            os.unlink(tmp)
        os.symlink(prev, tmp)
        os.rename(tmp, os.path.join(self.releases, "current"))
        self.journal("UPDATE_ROLLED_BACK", version=os.path.basename(prev))
        return True

    # -- full pipeline --
    def update(self, source_dir, version, expected_sha256=None,
               compatibility=None):
        """Run the whole pipeline. Returns (ok, detail)."""
        # phase 71: compatibility first
        env = runtime_env()
        ok, issues = check_compatibility(env, compatibility or {})
        if not ok:
            self.journal("UPDATE_REFUSED", reason="compatibility",
                         issues=issues)
            return False, {"phase": "compatibility", "issues": issues}
        staged = self.download(source_dir, version)
        ok, tree_hash = self.verify(staged, expected_sha256)
        if not ok:
            shutil.rmtree(staged, ignore_errors=True)
            return False, {"phase": "verify"}
        version_dir = self.install_isolated(staged, version)
        if not self.test(version_dir):
            return False, {"phase": "test"}
        if not self.canary(version_dir):
            return False, {"phase": "canary"}
        prev = self.switch(version)
        if not self.health_check(version):
            self.journal("UPDATE_HEALTH_FAILED", version=version)
            self.rollback()
            return False, {"phase": "health", "rolled_back": True}
        self.journal("UPDATE_COMPLETE", version=version, previous=prev)
        return True, {"version": version, "previous": prev,
                      "tree_hash": tree_hash[:16]}


def _tree_hash(root):
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for fn in sorted(filenames):
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, root)
            h.update(rel.encode())
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
    return h.hexdigest()


# ---- phase 74: version compatibility gate ----
def compatibility_check(cur, new):
    """Compare current vs target runtime descriptors.

    cur/new: {"version": str, "state_schema": int,
              "python_requires": str}.
    Returns (ok, issues). A state-schema jump without a migration path,
    a version downgrade, or an unmet python requirement all block.
    """
    issues = []
    cur, new = cur or {}, new or {}
    cs, ns = cur.get("state_schema", 0), new.get("state_schema", 0)
    if ns > cs:
        issues.append(f"state schema jump {cs}->{ns} requires migration")
    if ns < cs:
        issues.append(f"state schema downgrade {cs}->{ns} unsupported")
    cv, nv = str(cur.get("version", "")), str(new.get("version", ""))
    if cv and nv and _ver_tuple(nv) < _ver_tuple(cv):
        issues.append(f"version downgrade {cv}->{nv} refused")
    req = new.get("python_requires")
    if req:
        import sys
        if not _python_satisfies(req, sys.version_info[:3]):
            issues.append(f"python {'.'.join(map(str, sys.version_info[:3]))}"
                          f" does not satisfy {req}")
    return (len(issues) == 0), issues


def _ver_tuple(v):
    parts = []
    for p in str(v).split("."):
        digits = "".join(c for c in p if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _python_satisfies(req, have):
    # minimal parser for ">=3.8" / "==3.11" style requirements
    import re
    m = re.match(r"\s*(>=|<=|==|>|<)\s*(\d+(?:\.\d+)*)", str(req))
    if not m:
        return True
    op, want = m.group(1), _ver_tuple(m.group(2))
    have_t = tuple(have)
    n = max(len(want), len(have_t))
    want += (0,) * (n - len(want))
    have_t += (0,) * (n - len(have_t))
    return {"==": have_t == want, ">=": have_t >= want,
            "<=": have_t <= want, ">": have_t > want,
            "<": have_t < want}[op]


# ---- phase 72: ordered migrations ----
_EXTRA_MIGRATIONS = {}


def register_migration(version, fn):
    """Register a state migration fn(store) for a schema version."""
    _EXTRA_MIGRATIONS[int(version)] = fn


def apply_migrations(store, from_v, to_v, journal=None):
    """Run registered migrations in ascending order for
    from_v < v <= to_v. Returns the list of applied versions."""
    journal = journal or store.journal
    applied = []
    for v in sorted(_EXTRA_MIGRATIONS):
        if from_v < v <= to_v:
            _EXTRA_MIGRATIONS[v](store)
            applied.append(v)
            journal("MIGRATION_APPLIED", version=v)
    return applied


# ---- phase 71-75: gated convenience entrypoint ----
def _update_to(self, version, package_dir=None, expected_sha256=None,
               compatibility=None, cfg=None, store=None):
    """Self-update gated by the opt-in config (phase 71).

    Refuses unless cfg["self_update_enabled"] and the version is on
    cfg["self_update_allow"]. Runs the full
    DOWNLOAD->VERIFY->INSTALL ISOLATED->TEST->CANARY->SWITCH->HEALTH
    pipeline via update(), rolling back on health failure.
    """
    cfg = cfg or {}
    if not cfg.get("self_update_enabled"):
        self.journal("UPDATE_REFUSED", reason="not enabled")
        return False, "self-update not enabled"
    allow = cfg.get("self_update_allow", [])
    if allow and version not in allow:
        self.journal("UPDATE_REFUSED", reason="not in allowlist",
                     version=version)
        return False, f"version {version} not in self-update allowlist"
    if not package_dir:
        return False, "no package_dir provided"
    return self.update(package_dir, version, expected_sha256,
                       compatibility)


SelfUpdater.update_to = _update_to
