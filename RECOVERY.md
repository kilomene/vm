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

## Advanced reliability layer (phases 51–90)

### What IS guaranteed

- **Dependencies self-heal only when safe.** `dep_status()` reports
  required/installed version, availability, compatibility, health, and
  last-verification per dependency. `heal()` records every attempt in the
  recovery history, reinstalls only when the failure classifies temporary,
  and never touches a healthy dependency. Verified by
  `test_dep_heal_skips_healthy` / `test_dep_classify_temp_vs_permanent`.
- **Retry is governed by classification, not a fixed count.**
  `classify()` maps (source, kind) → (TRANSIENT/RETRYABLE/PERMANENT/
  POLICY_BLOCKED/HUMAN_REQUIRED, max retries, backoff). PERMANENT,
  POLICY_BLOCKED, and HUMAN_REQUIRED are never retried by the agent's step
  loop. Verified by `test_classify_permanent_never_retried` /
  `test_classify_policy_blocked`.
- **Recovery never repeats a failed method.** The failure fingerprint
  excludes the task id, so the same failure across tasks shares one
  signature; `FailureAnalyzer.choose_strategy()` skips methods already
  failed for that signature and escalates when all are exhausted. Verified
  by `test_failure_fingerprint_stable` /
  `test_failure_analyzer_escalates_when_exhausted`.
- **Every failure kind has a matrix entry.** The 24-entry recovery matrix
  (`matrix.py`) is runtime policy: each entry carries detection →
  classification → recovery → verification → retry limit → escalation.
  Unknown kinds get a bounded default (retry_limit ≤ 2), never unbounded
  retries. Verified by `test_matrix_complete` /
  `test_matrix_unknown_kind_has_bounded_default`.
- **The planner is monitored too.** Repetitive/oscillating plans are
  diagnosed, the plan is rebuilt from verified world state (completed ops
  pruned), validated, and resumed; rebuilds are refused after 2 per 5 min.
  Verified by `test_planner_rebuild_replaces_failed_step` /
  `test_planner_rebuild_guard` / `test_planner_recovery_via_agent`.
- **Progress is measured from observable state, never model output.**
  Store-backed fingerprints track `progress.*` world keys; 3 identical
  fingerprints in a row marks the task STALLED. Verified by
  `test_progress_stall_detected` / `test_progress_no_stall_when_changing`.
- **Tasks cannot touch each other's operations or resources.**
  Operation ownership (claim/heartbeat/expiry), resource ownership with
  leases, and external-resource ownership are enforced; stale owners are
  reaped as UNKNOWN (never assumed failed). Verified by
  `test_ownership_claim_and_heartbeat` / `test_ownership_stale_reaped` /
  `test_external_ownership_exclusive`.
- **Capabilities are least-privilege with TTL.** A task gets exactly the
  capabilities for the tools its spec uses; the agent checks per step
  before execution; grants expire (lazy expiry on check). Escalation
  capabilities always need an out-of-band token. Verified by
  `test_caps_denied_by_default` / `test_caps_ttl_expiry`.
- **Secrets never touch disk.** The vault is process-local in-memory only
  (owner-scoped, TTL leases); refs resolve at execution time; the journal
  scanner detects exposure. Verified by `test_vault_scoped_access` /
  `test_vault_ttl` / `test_scan_for_exposure`.
- **Config is versioned and restorable.** Every config change is
  snapshotted; restore versions the current content first (restores are
  reversible); `mark_config_good` pins the last known-good. Verified by
  `test_config_version_and_restore` / `test_config_mark_good_pins_version`.
- **Snapshots are content-verified.** `snapshot_save` hashes content;
  `snapshot_verify` detects tampering; resume verifies the chain.
  Verified by `test_snapshot_verify_detects_tamper`.
- **External operations reconcile, never assume.** `reconcile_external()`
  maps done→synced-done, in_progress→resuming, failed→retry,
  unknown→unknown-open. Verified by `test_reconcile_external_done` /
  `test_reconcile_external_in_progress_resuming`.
- **CRITICAL work preempts, but cannot starve others.** At most 2
  preemptions per task; state is checkpointed before preemption. Verified
  by `test_preemption_triggered`.
- **Pause/resume are verified, not assumed.** `request_pause` confirms the
  task left RUNNING within a deadline; `request_resume` verifies the
  snapshot chain first. Verified by `test_pause_verified` /
  `test_resume_verifies_snapshot_chain`.
- **Resource budgets stop runaway tasks.** Duration, tool-call, and memory
  (RSS) budgets pause the task on exhaustion. Verified by
  `test_memory_budget_pauses`.
- **The clock is never trusted blindly.** `lease_valid` fails closed on an
  unreliable clock; the supervisor marks the agent hung rather than
  believing a stale beat; leases are never mass-expired on clock fault.
  Verified by `test_lease_valid_fails_closed_on_bad_clock`.
- **Self-update is opt-in and gated.** Refused unless `self_update_enabled`
  and the version is on the allowlist; compatibility (schema/python/
  version) is checked first; the pipeline is
  DOWNLOAD→VERIFY→INSTALL ISOLATED→TEST→CANARY→SWITCH→HEALTH with rollback
  on health failure. Verified by `test_update_blocks_without_allowlist` /
  `test_update_compat_rejects`.
- **Backups are proven, not just written.** Manifests hash every file;
  `test_restore` restores to a temp dir and marks the backup proven;
  `prune_backups` never deletes the only proven backup. Verified by
  `test_backup_create_verify_restore` / `test_backup_prune_keeps_proven`.
- **Self-protection is checked first.** `self_protection_check()` runs
  before any other classification: log/journal/backup deletion, safety
  source-file modification, safety-feature disablement, capability
  self-grant, and `VM_AGENT_HOME` escape are all blocked. Verified by
  `test_self_protection_blocks_log_deletion` (and siblings).
- **Fault injection proves it, not just asserts it.** 11 injections run
  against isolated temp homes; the compound suite (phase 89) combines
  stale-lock + expired-lease + ambiguous-crash and asserts no duplicate
  side effects. Verified by `test_faultinject_scenario_all_recover` and
  `tests/test_compound.py`.
- **Dry-run changes nothing.** `dry_run()` validates schema, policy,
  capabilities, and budget feasibility per step without executing or
  mutating state. Verified by `test_dry_run_blocks_protected` /
  `test_dry_run_passes_safe`.

### What is NOT guaranteed (honest limits)

- **The executor does not independently enforce capabilities.** The agent
  checks per step before calling the executor, but a direct caller of
  `Executor.run()` bypasses capability checks. Capability enforcement is a
  property of the agent loop, not the tool layer.
- **The vault dies with the process.** Secrets must be re-provisioned after
  every agent restart; there is deliberately no durable secret store.
- **Failure fingerprints are error-class based.** Two different root
  causes with the same error class share a signature; volatile details
  (pids, timestamps) are normalized away, which can merge distinct
  failures.
- **Stall detection needs observable writes.** Tasks whose progress isn't
  reflected in `progress.*` world keys look stalled after 3 unchanged
  cycles; steps must record observable progress.
- **Priority preemption is cooperative.** Preemption happens at step
  boundaries; a single very long step delays preemption until it finishes
  or its tool timeout fires.
- **Self-update canary is a smoke test, not proof.** The canary runs
  controlled operations on the new runtime; it cannot prove the absence of
  behavioral regressions.
- **The 6 root-required integration tests were syntax-verified only.**
  They need a real root install (`/opt/vm-agent`) and were not executed
  live in this environment.
