"""Configuration for the vm-agent runtime."""
import json
import os

DEFAULTS = {
    # Directories (overridden by install to /opt/vm-agent)
    "base_dir": os.environ.get("VM_AGENT_HOME", "/opt/vm-agent"),
    # Heartbeat
    "heartbeat_interval_s": 10,
    "heartbeat_timeout_s": 30,
    # Supervisor
    "restart_backoff_base_s": 2,
    "restart_backoff_max_s": 300,
    "max_restarts_per_hour": 12,
    # Watchdog / hang detection
    "tool_timeout_default_s": 120,
    "tool_timeout_max_s": 3600,  # absolute upper bound on any tool timeout
    "tool_timeouts": {
        "shell": 120,
        "python": 300,
        "install_pkg": 900,
        "build": 1800,
        "browser": 600,
        "http": 60,
    },
    "no_progress_timeout_s": 600,
    # Logging
    "log_max_bytes": 5 * 1024 * 1024,
    "log_backup_count": 5,
    # Resources
    "max_workers": 4,
    "max_log_disk_mb": 100,
    # Health
    "health_port": 0,  # 0 = disabled; set to e.g. 127.0.0.1:9119 to enable
    # Phases 26-60
    "lock_ttl_s": 120,            # stale lock reap threshold
    "lease_interval_s": 30,       # task lease heartbeat interval
    "safe_mode_max_l3_retries": 2,  # enters safe mode after this many L3 failures
    "intervention_ttl_hours": 24,
    "control_port": 0,           # 0 = ephemeral; control binds 127.0.0.1 only
    "control_enabled": True,
    # Phases 51-90
    "pause_verify_s": 30,        # phase 66: deadline to verify a pause
    "max_preemptions": 2,        # phase 65: starvation guard
    "backup_dir": None,          # phase 76: defaults to <base>/backups
    "backup_keep": 7,            # phase 76: retained backups
    "backup_auto_hours": 24,     # phase 76: automatic backup cadence
    "capability_ttl_s": 7 * 24 * 3600,  # phase 59: default grant lifetime
    "escalation_requires_auth": True,   # phase 59: cap escalation needs token
    "clock_drift_warn_s": 60,    # phase 70: warn threshold vs NTP
    "clock_drift_fail_s": 300,   # phase 70: refuse lease validity beyond
    "clock_fail_threshold": 3,   # consecutive bad clock checks before safe mode
    "clock_recover_threshold": 3,  # consecutive good checks to auto-clear
                                   # clock-caused safe mode
    "self_update_enabled": False,  # phase 71-75: opt-in only
    "self_update_allow": [],     # explicit version allowlist; empty=deny
    "fault_injection_enabled": False,  # phase 81: never on in production
}


def load(base_dir=None):
    cfg = dict(DEFAULTS)
    if base_dir:
        cfg["base_dir"] = base_dir
    path = os.path.join(cfg["base_dir"], "config", "config.json")
    if os.path.exists(path):
        with open(path) as f:
            cfg.update(json.load(f))
    cfg["state_db"] = os.path.join(cfg["base_dir"], "state", "state.db")
    cfg["journal_dir"] = os.path.join(cfg["base_dir"], "state", "journal")
    cfg["checkpoint_dir"] = os.path.join(cfg["base_dir"], "checkpoints")
    cfg["log_dir"] = os.path.join(cfg["base_dir"], "logs")
    cfg["run_dir"] = os.path.join(cfg["base_dir"], "run")
    return cfg
