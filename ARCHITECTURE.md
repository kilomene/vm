# ARCHITECTURE

## Components

### 1. Supervisor (`lib/vmagent/supervisor.py`)
Independent process. Owns an exclusive flock (`run/supervisor.lock`) so two
supervisors can never run. Starts the agent as a child (`start_new_session`).
Monitors:
- **crash**: child process exited → collect diagnostics → backoff → restart
- **hang**: heartbeat stale > `heartbeat_timeout_s` (30s) AND no task
  progress for > `no_progress_timeout_s` (600s) → restart
  (CPU usage is never used as liveness proof.)

Backoff: 2s × 2^n, capped at 300s. Refuses to restart if >12 restarts/hour
(prevents infinite rapid loops); stays down and logs `RESTART_REFUSED`.

On graceful SIGTERM: stops the agent (SIGTERM → 20s → SIGKILL), keeps state,
exits. systemd (`Restart=always`) brings it back.

### 2. Agent runtime (`lib/vmagent/agent.py`)
Task engine. Loop:
1. `recover()` on every start: find tasks in RUNNING/PAUSED, resume from
   latest checkpoint (skip verified steps via `idempotent_check`).
2. Serve: heartbeat every 10s, pick up PENDING tasks, run steps.

Each step: policy check → execute via Executor → verify via Verifier →
checkpoint **only if both pass** → next. On shutdown mid-step: mark PAUSED
(not FAILED) via the `INTERRUPTED` path.

### 3. Tool execution layer (`lib/vmagent/tools.py`)
`shell` (subprocess + timeout), `write_file`, `read_file`, `mkdir`,
`http_get`. Every call returns a `ToolResult` (exit code, stdout/stderr,
timed_out) — observed fact, not a claim. Secrets in args are redacted in
logs. `browser` is a declared slot returning UNAVAILABLE without a driver.

### 4. Verification layer (`lib/vmagent/verify.py`)
Read-only checks against the real world. A step passes only if the tool
succeeded AND all `verify` checks pass. Verified results are stored in the
task row (`last_verified_result`); the world-state kv can only be written
by the verifier path.

### 5. Policy layer (`lib/vmagent/policy.py`)
Classifies shell commands: SAFE / RESTRICTED (logged) / PROTECTED (refused).
Protected: shutdown/reboot/halt/poweroff as commands, disabling the
vm-agent unit, deleting state/journal/checkpoints, killing the supervisor.
The runtime refuses PROTECTED without an out-of-band token (not implemented
to auto-approve — refusal is the safe default).

### 6. State (`lib/vmagent/state.py`)
SQLite tables: `tasks`, `checkpoints`, `heartbeats`, `kv` (world state),
`restarts`. Plus an append-only JSONL event journal (`state/journal/`).
Thread-safe via a lock; WAL not required at this scale.

### 7. CLI (`lib/vmagent/cli.py`)
`vm-agent status|health|logs|tasks|submit|restart|diagnostics|recover`.

## Data flow (one step)

```
spec step → policy.classify → executor.run → ToolResult
                                              ↓
                                    verifier.verify_step → Verdict
                                              ↓
                              pass? → checkpoint → next step
                              fail? → retry (backoff) → FAILED
                              shutdown? → PAUSED (resume later)
```

## Heartbeat protocol

Agent writes every 10s: pid, task_id, step, operation, last_action, status,
timestamp. Supervisor reads and correlates with task progress. Workers (future)
use `worker:<id>` component names.
