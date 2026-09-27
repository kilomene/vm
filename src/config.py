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
