# INSTALL

## Requirements

- Linux with systemd (verified working) — otherwise the installer falls back
  to a `@reboot` cron entry (best effort)
- Python 3.8+ with `sqlite3` stdlib module
- Root (or a user with permission to write `/opt/vm-agent` and systemd units)

No pip packages. No network access needed at install time.

## Install

```bash
./install.sh
```

Idempotent: re-running repairs/upgrades without touching task data.
Creates `/opt/vm-agent`, a `vmagent` system user (falls back to root),
the systemd unit, enables it, starts it, and runs a health check.

## Uninstall

```bash
./uninstall.sh          # keeps task data (state, journals, checkpoints, logs)
./uninstall.sh --purge  # deletes everything including task data
```

## Files

| Path | Purpose |
|---|---|
| `/opt/vm-agent` | install root |
| `/etc/systemd/system/vm-agent.service` | systemd unit |
| `/usr/local/bin/vm-agent` | CLI symlink |
