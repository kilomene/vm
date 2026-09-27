# RECOVERY

## What survives what

| Event | Behavior |
|---|---|
| SSH disconnect | Nothing happens. Supervisor/agent are children of systemd, not the SSH session. |
| Terminal closed | Same — no dependency on any terminal, tmux, or screen. |
| Agent crash (SIGKILL) | Supervisor detects exited child → diagnostics → backoff → restart → agent `recover()` resumes RUNNING tasks from checkpoints. |
| Agent hang | Heartbeat stale + no progress → supervisor restarts it. |
| Supervisor crash | systemd `Restart=always` (10s delay) restarts it. |
| VM reboot | systemd starts supervisor at boot → agent starts → `recover()` resumes. |
| VM powered off | Nothing runs (fundamental limit). Recovery begins at next boot. |

## Resume semantics

- On start, the agent queries tasks with status RUNNING or PAUSED.
- For each, it loads the latest checkpoint and re-runs from the next step.
- Steps with `idempotent_check` that pass are skipped (no duplicate side effects).
- Steps without `idempotent_check` re-execute — declare idempotency in the spec.
- Graceful shutdown mid-step marks the task PAUSED (never FAILED).

## Verifying recovery yourself

```bash
# 1. SSH-disconnect survival
vm-agent submit task.json --id t1
# close your SSH session entirely, reconnect later
vm-agent tasks   # task kept running

# 2. Crash recovery
vm-agent health  # note agent_pid
kill -9 <agent_pid>
sleep 15
vm-agent health  # new pid, restarts_last_hour incremented

# 3. Reboot resume
vm-agent submit long-task.json --id t2
sudo systemctl stop vm-agent   # or actually reboot the VM
sudo systemctl start vm-agent  # (or wait for boot)
vm-agent tasks   # t2 resumes from checkpoint and completes
```

## Event journal

Every lifecycle event is appended to `state/journal/YYYY-MM-DD.jsonl`:
`TASK_STARTED/RESUMED/PAUSED/COMPLETED/FAILED`, `STEP_*`, `CHECKPOINT_CREATED`,
`AGENT_STARTED/CRASHED/HUNG/RESTARTING`, `DIAGNOSTICS_COLLECTED`,
`RECOVERY_STARTED/COMPLETED`, `SUPERVISOR_STARTED/STOPPED`.
Reconstruct post-crash timelines with `vm-agent diagnostics`.
