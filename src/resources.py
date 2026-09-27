"""Phase 42/55: resource pressure + disk-full handling.

Monitor RAM, CPU load, disk, inodes, process count, FDs. Detect pressure
BEFORE the OS kills critical processes. Response order:
  1. shed optional workers
  2. pause low-priority tasks
  3. stop abandoned processes
  4. rotate logs / clean safe temp data
  5. preserve supervisor + task state above all
Never delete user data as an automatic action.
"""
import glob
import os
import time

# thresholds: (warning, critical)
THRESHOLDS = {
    "disk_free_mb": (500, 100),       # warn below 500MB, critical below 100MB
    "mem_available_mb": (200, 80),
    "load_per_cpu": (4.0, 8.0),
}


def _meminfo():
    info = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                info[k.strip()] = int(v.split()[0])  # kB
    except OSError:
        pass
    return info


def sample():
    """Return a resource snapshot dict."""
    s = {"ts": time.time()}
    try:
        st = os.statvfs("/tmp")
        s["disk_free_mb"] = st.f_bavail * st.f_frsize / 1e6
        s["disk_inode_free"] = st.f_favail
    except OSError:
        pass
    mi = _meminfo()
    if "MemAvailable" in mi:
        s["mem_available_mb"] = mi["MemAvailable"] / 1024
    try:
        s["load_1"] = os.getloadavg()[0]
        s["cpu_count"] = os.cpu_count() or 1
        s["load_per_cpu"] = s["load_1"] / s["cpu_count"]
    except OSError:
        pass
    try:
        s["process_count"] = len([p for p in os.listdir("/proc")
                                  if p.isdigit()])
    except OSError:
        pass
    return s


def pressure_level(s):
    """Return (level, reasons): ok | warning | critical."""
    reasons = []
    level = "ok"
    df = s.get("disk_free_mb")
    if df is not None:
        if df < THRESHOLDS["disk_free_mb"][1]:
            level, reasons = "critical", reasons + [f"disk {df:.0f}MB free"]
        elif df < THRESHOLDS["disk_free_mb"][0] and level == "ok":
            level, reasons = "warning", reasons + [f"disk {df:.0f}MB free"]
    ma = s.get("mem_available_mb")
    if ma is not None:
        if ma < THRESHOLDS["mem_available_mb"][1]:
            level, reasons = "critical", reasons + [f"mem {ma:.0f}MB avail"]
        elif ma < THRESHOLDS["mem_available_mb"][0] and level == "ok":
            level, reasons = "warning", reasons + [f"mem {ma:.0f}MB avail"]
    lpc = s.get("load_per_cpu")
    if lpc is not None and lpc > THRESHOLDS["load_per_cpu"][1]:
        level, reasons = "critical", reasons + [f"load {lpc:.1f}/cpu"]
    return level, reasons


def respond(store, cfg, level, reasons, agent=None):
    """Take pressure response actions. Returns list of actions taken."""
    actions = []
    base = cfg["base_dir"]
    if level == "ok":
        return actions
    store.journal("RESOURCE_PRESSURE", level=level, reasons=reasons)

    # 1. rotate logs (always safe)
    rotated = _rotate_logs(os.path.join(base, "logs"))
    if rotated:
        actions.append(f"rotated {rotated} logs")

    # 2. clean safe temp data (never user data)
    cleaned = _clean_tmp(os.path.join(base, "logs"), "*.tmp")
    if cleaned:
        actions.append(f"cleaned {cleaned} tmp files")

    if level == "critical" and agent is not None:
        # 3. pause low-priority tasks, keep supervisor + state alive
        paused = agent.pause_low_priority()
        if paused:
            actions.append(f"paused {len(paused)} low-priority tasks")
        store.journal("PRESSURE_SHED", actions=actions)
    return actions


def _rotate_logs(log_dir, max_bytes=5 * 1024 * 1024, keep=3):
    rotated = 0
    try:
        for path in glob.glob(os.path.join(log_dir, "*.out")) + \
                    glob.glob(os.path.join(log_dir, "*.log")):
            try:
                if os.path.getsize(path) > max_bytes:
                    for i in range(keep - 1, 0, -1):
                        src, dst = f"{path}.{i}", f"{path}.{i + 1}"
                        if os.path.exists(src):
                            os.rename(src, dst)
                    os.rename(path, f"{path}.1")
                    open(path, "w").close()
                    rotated += 1
            except OSError:
                continue
    except OSError:
        pass
    return rotated


def _clean_tmp(log_dir, pattern):
    n = 0
    try:
        for path in glob.glob(os.path.join(log_dir, pattern)):
            try:
                os.unlink(path)
                n += 1
            except OSError:
                continue
    except OSError:
        pass
    return n


def process_rss_mb():
    """Current process RSS in MB (phase 68: memory budget enforcement)."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    kb = int(line.split()[1])
                    return kb / 1024
    except OSError:
        pass
    return None
