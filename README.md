# vm-agent — persistent self-recovering computer-agent runtime

Makes an agent remain operational for as long as the VM runs. Survives
terminal/SSH disconnects, recovers from crashes, detects hangs, preserves
task state in SQLite, verifies tool results independently, and resumes
unfinished work after reboot.

## The one fundamental limitation

**If the VM itself is powered off, nothing inside the VM can execute.**
Automatic recovery begins after the VM boots again: systemd starts the
supervisor → the supervisor starts the agent → the agent reloads SQLite
state → unfinished tasks resume from their last verified checkpoint.

## Quick start

```bash
./install.sh                 # install (idempotent — safe to re-run)
vm-agent status              # supervisor + agent + tasks overview
vm-agent health              # JSON health (exits 0 only if fully up)
vm-agent submit task.json    # queue a task spec
vm-agent tasks               # list tasks
vm-agent logs                # tail agent log
vm-agent diagnostics         # heartbeats + recent journal
vm-agent recover             # show unfinished tasks (auto-resume on start)
vm-agent restart             # graceful restart via supervisor
vm-agent diagnose            # full one-command status summary (phase 59)
vm-agent locks               # list held locks
vm-agent interventions       # open human interventions
vm-agent world               # verified world state
vm-agent task-pause <id>     # pause a task (step boundary)
vm-agent task-resume <id>    # re-queue a paused/failed task
vm-agent task-retry <id> --from-step N  # re-queue from 1-based step N
vm-agent task-cancel <id>    # state-aware cancel (checkpoint, then stop)
```

## Architecture

```
systemd (vm-agent.service)
  └─ supervisor (independent process, holds exclusive lock)
       └─ agent runtime (child process)
            ├─ task engine (executes steps from SQLite, never from memory)
            ├─ tool workers (subprocess tools with timeouts)
            ├─ verifier (independently checks every step result)
            └─ heartbeats → SQLite
       └─ watchdog (inside supervisor: heartbeat + progress monitoring)
```

**Boundaries (explicit):**
- The LLM proposes steps. The runtime executes. The **verifier** decides success.
- The **supervisor** owns process continuity. The agent never supervises itself.
- **SQLite** is the source of task continuity. LLM context is never trusted.
- The **VM** is the ultimate execution boundary.

## Task spec format

```json
{"steps": [
  {"name": "install nginx",
   "tool": "shell",
   "args": {"command": "apt-get install -y nginx"},
   "verify": [{"check": "command_ok", "command": "nginx -v"}],
   "idempotent_check": {"check": "command_ok", "command": "which nginx"},
   "retries": 2}
]}
```

`idempotent_check` makes resume-after-crash safe: if it passes, the step is
skipped. Steps without one re-run on resume (by design — declare idempotency).

Verify checks: `file_exists`, `file_contains`, `command_ok`,
`port_listening`, `http_ok`, `process_running`.

## Project layout (installed at /opt/vm-agent)

```
bin/vm-agent          CLI
lib/vmagent/          supervisor, agent, state, tools, verify, policy, cli
config/               config.json
state/state.db        SQLite (tasks, checkpoints, heartbeats, kv, restarts)
state/journal/        append-only event journal (JSONL per day)
checkpoints/          (checkpoints live in SQLite; dir reserved)
logs/                 agent.out, supervisor.out, diag-*.json
run/                  supervisor.lock, supervisor.pid
```

## Docs

- INSTALL.md — installation & uninstall
- ARCHITECTURE.md — component design
- RECOVERY.md — recovery behavior
- TROUBLESHOOTING.md — common issues
- SECURITY.md — security model
- ENVIRONMENT.md — this VM's inspection report
