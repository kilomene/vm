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

## Advanced reliability layer (phases 26–60)

### What IS guaranteed

- **No duplicate side effects for registered operations.** Every step with an
  `op_id` is recorded in the operation registry (`COMPLETED`/`RUNNING`/
  `UNKNOWN`/`FAILED`). A `COMPLETED` op is never re-executed after a crash —
  the runner skips it. Verified by `test_op_registry_idempotent_skip`.
- **Ambiguous operations pause, never guess.** An op in `UNKNOWN` state with
  no way to reconcile (no `idempotent_check`) pauses the task and opens a
  human intervention instead of retrying blindly. Verified by
  `test_op_registry_unknown_no_reconcile_pauses`.
- **Reconcile-before-retry.** An op in `UNKNOWN` state *with* an
  `idempotent_check` is verified against real state first: if the effect
  exists it is marked `COMPLETED` (skip); if not, it is marked `PENDING`
  (safe to retry). Verified by `test_op_registry_unknown_reconciles_*`.
- **Transactional steps roll back.** `run_txn(prepare/execute/verify/
  rollback)` runs verify independently of the executor's claims; on verify
  failure the rollback function runs and the op is marked `FAILED` (not
  silently `COMPLETED`). Verified by `test_txn_verify_fail_rolls_back`.
- **Stale locks are reaped, live locks are not.** Locks carry a TTL and owner
  pid; only expired locks whose owner is dead are reclaimed. Verified by
  `test_stale_lock_reaped` / `test_live_lock_not_reaped`.
- **Deadlocks are detected and broken least-destructively.** The wait-graph
  cycle detector releases the victim's non-contended locks and backs off
  before ever restarting a worker. Verified by
  `test_deadlock_cycle_detected` / `test_deadlock_detector_recovery`.
- **One worker per task.** Task leases prevent two agents from running the
  same task; a live lease cannot be stolen. Verified by
  `test_lease_claim_and_heartbeat`.
- **Startup reconciliation before resume.** On boot the supervisor reaps
  stale locks/leases, re-queues `RUNNING` tasks left by a dead agent, checks
  dependency health and config integrity *before* the agent resumes work.
- **Escalating recovery, then safe mode.** Consecutive failures escalate
  L1 (retry) → L8 (intervention); at L7+ the runtime enters safe mode — no
  dangerous actions, state preserved — instead of looping forever. Verified
  by `test_recovery_escalates_to_safe_mode`.
- **Auth failures are never retried blindly.** Network and model failure
  classifiers mark auth/credential failures non-retryable; they open an
  intervention immediately. Verified by `test_net_retry_policy_shape`.
- **Budgets stop runaway tasks.** Per-task `tool_calls` / `duration_s`
  budgets pause the task and open an intervention on exhaustion — no silent
  infinite loops. Verified by `test_budget_consume_and_exhaust`.
- **State-aware cancellation.** `task-cancel` walks
  `CANCEL_REQUESTED → STOPPING → CLEANUP → CHECKPOINT → CANCELLED` — the
  current step finishes safely and a final checkpoint is written, instead of
  a SIGKILL mid-write.
- **Model output is schema-validated.** Every model-proposed action is
  validated against a strict tool/args schema before execution; malformed
  actions are rejected and journaled, never executed.
- **World state is verifier-only.** `world_state` is written only by
  verification probes; model assertions go to a separate `model_claims`
  store and are reconciled against observations — the model can never
  overwrite verified reality.
- **Audit trail is append-only.** Every privileged action is journaled with
  actor, args, and result.

### What is NOT guaranteed (honest limits)

- **No absolute reliability.** A kernel panic, disk failure, or power loss
  mid-write can still lose the last un-checkpointed step. The design bounds
  the loss to one step, it does not eliminate it.
- **Reconciliation is only as good as the `idempotent_check`.** If a step
  declares no check, resume re-executes it. Non-idempotent steps without
  checks can double-apply — declare checks for every side-effecting step.
- **Safe mode needs a human.** The runtime stops and preserves state, but
  only an operator (`vm-agent diagnose`, then fix, then resume) clears it.
- **The control API is localhost-only by design.** Remote access requires an
  SSH tunnel; there is no built-in TLS or multi-user auth.
- **Hang detection is heuristic.** A task making slow-but-real progress can
  look hung; the no-progress timeout is tunable, not magic.
- **Browser recovery needs a host driver.** Without one wired in, the
  browser subsystem reports `DOWN` and browser steps fail fast with a clear
  error instead of hanging.
