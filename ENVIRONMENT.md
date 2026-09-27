# Phase 1 — Environment Report (2026-09-27)

Inspected before any code was written.

| Fact | Value |
|---|---|
| OS | Ubuntu 24.04.5 LTS (Noble Numbat) |
| Kernel | 7.0.0-38-generic |
| Arch | x86_64 |
| Init system | **systemd, PID 1, fully functional** — verified by creating, starting, and removing a probe unit (`vm-probe.service`, exit 0) |
| Package manager | apt |
| Python | 3.12.3 (stdlib + `sqlite3` module present) |
| Node.js | v24.20.0 |
| RAM | 7935 MB total, ~1973 MB available |
| Disk | 7.5 GB overlay, 80 MB used (2%) |
| User | root (uid 0) — sudo N/A |
| sqlite3 CLI | 3.45.1 |
| System cron | **not installed** (no cron.service); scheduling done by external runtime |

## Existing processes (DO NOT TOUCH)

- `hatch daemon` (PID 67) and `hatch-execd` (PID 735) — host agent infrastructure
- Phone tunnel stack: `adb_tunnel.py` (PIDs 3891, 3870), `tunnel-supervisor.sh` (PID 1969), `/tmp/fwd6.py` (PID 13701), `adb fork-server` (PID 1984)
- Parent-owned crons/watchers run outside this VM's init

## Existing projects (DO NOT TOUCH)

`~/workspace/phone-app`, `~/workspace/voice-app`, `~/SocialAgent`, all other `~/workspace/*` trees.

## Design consequences

1. **systemd IS usable** — verified working. Use a real systemd unit for auto-start/restart (not the nohup fallback).
2. Install location: `/opt/vm-agent/` (system-wide, matches Phase 23 structure, keeps `~/workspace/vm/` as the build/push tree).
3. Dependencies: Python 3 stdlib + sqlite3 only. No pip packages.
4. Run as root here (container); installer creates a dedicated `vmagent` user when possible, falls back to root with a warning.
5. No system cron — the supervisor's own scheduler tick replaces cron; `@reboot` equivalent is the systemd unit (`WantedBy=multi-user.target`).
6. Browser automation: none bundled — the tool layer exposes a `browser` tool slot that a host agent wires in; local runs mark it unavailable rather than fake it.
