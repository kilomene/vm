"""Phase 70: time synchronization.

System time affects leases, locks, schedules, certificates, logs, recovery
timestamps, and external API auth. This module:

  - checks time synchronization at startup and periodically
  - detects unreliable clocks (large jumps, implausible dates)
  - protects time-sensitive operations: leases/locks are never expired
    because of a clock fault

`safe_now()` is the clock the runtime should use for lease/lock math:
it detects backward jumps and freezes expiry decisions while the clock
is unreliable, instead of mass-expiring everything.
"""
import os
import subprocess
import time

_last_monotonic = time.monotonic()
_last_wall = time.time()
_unreliable = False


def check_sync():
    """Return (reliable: bool, detail: dict). Best-effort, stdlib only."""
    detail = {}
    # 1. plausibility: wall clock must be sane
    wall = time.time()
    detail["wall"] = wall
    if wall < 1700000000:  # before Nov 2023: clearly wrong
        return False, {**detail, "reason": "wall clock implausible (pre-2023)"}
    # 2. monotonic vs wall consistency: detect jumps since last check
    global _last_monotonic, _last_wall, _unreliable
    mono = time.monotonic()
    wall_delta = wall - _last_wall
    mono_delta = mono - _last_monotonic
    _last_monotonic, _last_wall = mono, wall
    skew = abs(wall_delta - mono_delta)
    detail["skew_s"] = round(skew, 2)
    if skew > 120:
        _unreliable = True
        return False, {**detail, "reason": f"clock jumped ~{skew:.0f}s"}
    # 3. ask the OS if NTP sync is known (best effort)
    for cmd in (["timedatectl", "show", "-p", "NTPSynchronized"],
                ["chronyc", "tracking"]):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=5)
            if p.returncode == 0:
                detail["os_time_check"] = (p.stdout or "").strip()[:120]
                break
        except (OSError, subprocess.TimeoutExpired):
            continue
    _unreliable = False
    return True, detail


def clock_unreliable():
    return _unreliable


def mark_unreliable(reason, journal=None):
    global _unreliable
    _unreliable = True
    if journal:
        journal("CLOCK_UNRELIABLE", reason=reason)


def safe_now():
    """Wall-clock time for display; lease math should use this plus the
    unreliable flag — never expire leases while the clock is suspect."""
    return time.time()


def lease_still_valid(expires_at):
    """True if the lease should be treated as valid. When the clock is
    unreliable we FAIL CLOSED: treat the lease as still valid rather than
    mass-expiring (which could hand one task to two workers)."""
    if _unreliable:
        return True
    return time.time() < expires_at


def lease_valid(ts, max_age_s=None, check_clock=True):
    """Fail-closed lease/heartbeat validity.

    If the clock is unreliable (check_sync fails), freshness cannot be
    trusted -> return False so callers treat the heartbeat/lease as
    unverifiable (e.g. supervisor marks the agent hung rather than
    believing a stale beat). With check_clock=False, age alone decides.
    """
    if check_clock:
        ok, _detail = check_sync()
        if not ok:
            return False
    if max_age_s is None:
        return True
    return (time.time() - ts) <= max_age_s
