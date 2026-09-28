# ARCHITECTURE AUDIT (Phase 90)

Date: 2026-09-27. Method: the full test suites were run against the real
code (`tests/test_phase3.py`, `tests/test_compound.py`,
`tests/test_reliability.py`), and every integration path was exercised
through a real `AgentRuntime` loop, not mocked. The audit's purpose is to
say what is actually proven, what was found broken, and what remains a
known gap — not to re-assert the design.

## Integration bugs found by running (all fixed)

1. **`recovery.py` called `agent.pause_task()` — it does not exist.**
   The level-8 recovery path named the strategy "pause_task"; the real
   verified API is `agent.request_pause()`. Fixed: L8 now calls
   `request_pause()` and journals `TASK_PAUSED_L8`.

2. **`agent.run_task` passed the tool name as the failure kind.**
   `recovery.recover()` received e.g. `kind="shell"`, hit the unknown-kind
   default (`retry_limit=2`), and entered safe mode on the second failure
   of any step. Fixed: the agent now passes `kind="step_failed"` so the
   matrix applies, and the step error text is carried on
   `agent._last_step_error` for escalation context.

3. **`Store` had no `checkpoints()` method.**
   `AgentRuntime._verify_snapshot_chain()` (used by `request_resume`)
   called `self.store.checkpoints(task_id)`, which never existed — resume
   would have raised `AttributeError` on any task with a checkpoint.
   Fixed: added `Store.checkpoints(task_id)` (oldest first).

4. **Checkpoint dict key mismatch.** `_verify_snapshot_chain` read
   `c.get("state")`; the `checkpoints` table stores `state_json`.
   Fixed: the agent now reads `state_json` (with `state` as fallback).

5. **Planner recovery shell interpolation.** The recovery step built its
   shell command from the diagnosis text; parentheses in the diagnosis
   broke `/bin/sh`. Fixed: the recovery command is static
   (`echo planner-recovery-ok`); the reason travels in a non-executed
   `recovery_reason` field.

6. **`secrets.py` missing `import os`** in `scan_journal` — NameError on
   any journal scan. Fixed.

7. **Vault-resolved secrets journaled raw.** `_resolve_vault_refs`
   substituted `vault_get` values into step args and `Executor._redact`
   only redacted by argument *name* — a secret passed as `command` was
   journaled raw in `TOOL_STARTED`. Fixed: `_resolve_vault_refs` now
   returns `(resolved_args, secret_values)`; `Executor.run` takes
   `redact_values` and redacts them by value in the journal entry and
   in `ToolResult.stdout/stderr` before journaling/return (so `last_err`
   and `last_verified_result` inherit the redaction). New
   `secrets.redact_values()` helper.

8. **No integrity baseline at install.** `reconcile_on_boot` warned "no
   integrity baseline recorded" on every fresh install; worse, TRACKED
   included the live SQLite `state/state.db` (always "modified") and a
   `systemd/vm-agent.service` path that is never installed under the
   prefix. Fixed: `install.sh` records the baseline at install time;
   TRACKED is `config/config.json` only; new `integrity.ensure_baseline()`
   records on first boot if missing (journaling
   `INTEGRITY_BASELINE_RECORDED`); `snapshot_config` re-records hashes
   after an authorized change.

9. **`recover --dry-run` crashed with `KeyError`.** `cmd_recover` read
   fingerprint columns (`failure_kind`, `last_method`, `operation`) that
   don't exist in the `failure_fingerprints` table. Fixed: the table
   gained the real `operation` column (migration + fresh CREATE);
   `Store.recovery_record(..., operation=...)` populates it,
   `failure.record` and `RecoveryManager.recover` thread it through, and
   the dry-run reads the real `kind`/`last_method`/`operation` columns.

10. **Control token created with a chmod-after-create race.**
    `ensure_token` wrote the token file then chmodded to 0600 — on a
    shared host another uid could read it in between. Fixed: atomic
    `O_CREAT|O_EXCL` open with mode 0600 (`FileExistsError` reads the
    existing token; half-written files unlinked); `run/` created and
    tightened to 0700.

11. **`process_running` verifier interpolated the pattern into a shell
    pipeline** (`ps aux | grep -F '{pattern}'`) — a malicious pattern
    could inject shell. Fixed: pure-Python `/proc/*/cmdline` scan with
    the same fixed-string semantics, no shell at all.

12. **Policy missed shell forms and had no file-write guard.** Power
    actions reached via path (`/sbin/shutdown`), `env`, subshells,
    newlines, `systemctl poweroff/reboot/halt/kill`, and `init 0/6` were
    not matched; state-destruction forms beyond `rm` (`find … -delete`,
    `mv`, `truncate`) were not matched; and `write_file`/`mkdir` had no
    policy check at all — a step could overwrite
    `lib/vmagent/policy.py` or the state DB. Fixed: broadened
    `PROTECTED_PATTERNS`, new `Policy.authorize_path` (realpath-resolved,
    refuses writes under `state/`, `lib/vmagent/`, `config/`, `run/`),
    wired into `_run_step`'s retry loop and `dry_run` verdicts.
    SECURITY.md/ARCHITECTURE.md now state the policy is a best-effort
    guardrail against accidental damage, not a security boundary — the
    real boundary is the OS user plus systemd hardening.

13. **`_run_step` recorded failures from a stale task snapshot.**
    `failed_steps`/`retry_count` were computed from the `task` dict
    fetched once at `run_task` start, so a second step failure in the
    same long task overwrote the first failure's values. Fixed: re-read
    via `store.get_task(task_id)` immediately before computing.

14. **`reconcile_on_boot` never re-queued stuck RUNNING tasks.** The
    docs (RECOVERY.md) promised re-queueing; the code had a dead `pass`
    lease loop and only appended a warning. Fixed: stuck RUNNING tasks
    with no live agent are set PENDING, their stale leases released, and
    `TASK_REQUEUED_ON_BOOT` journaled. No double-run with
    `AgentRuntime.recover()`: `recover()` resumes only RUNNING/PAUSED,
    `serve_forever` picks up only PENDING, and both funnel through
    `claim_lease`.

15. **No upper bound on spec-supplied `timeout_s`; control failure hid
    the exception type.** Fixed: `tool_timeout_max_s` (default 3600s,
    in config DEFAULTS) caps `Executor._sane_timeout` on top of the
    existing non-positive clamp; and `serve_forever`'s control-start
    block was extracted to `AgentRuntime._start_control()`, which
    journals `CONTROL_FAILED` with `error_type=type(e).__name__`
    explicitly.

All fifteen were invisible to unit-style reading and surfaced only when
the agent loop actually ran the paths. This is why the phase-89 compound
suite and phase-79 fault-injection exist.

## What the tests prove (counts from the 2026-09-27 run)

- `tests/test_phase3.py`: 67 tests — every phase 51–90 module exercised
  through its real API, plus agent-loop integration (planner rebuild via
  `run_task`, pause verification, capability denial, preemption).
- `tests/test_compound.py`: 5 tests — compound failures: stale lock +
  expired lease; ambiguous crash + stale lock with reconcile and no
  duplicate side effect; unreliable clock fails closed; dep failure +
  step failure both fingerprinted and bounded; intervention preserves
  verified state.
- `tests/test_reliability.py`: 37 tests — phases 26–60, re-run after the
  agent/recovery fixes to confirm no regression.

## Bug-fix pass (2026-09-28) — 14 items

Seven real fixes, one missing feature implemented, five reported bugs
verified absent (pinned with regression guards), one doc correction:

- Fixed: supervisor hang detection re-baselined `_last_progress` every
  tick so idle time never accumulated; three-valued op reconcile
  (ambiguous ops pause instead of retrying); clock safe-mode hysteresis
  + `safe-mode exit` operator command; error classification on real
  stderr with permanent errors running exactly once; per-step retry caps
  with repeated-failure escalation to human; `redact_text` consulting the
  canonical module vault; non-positive tool timeouts clamped to default.
- Implemented: `task-retry --from-step N` (did not exist) with the
  1-based → `current_step` mapping correct by construction.
- Verified absent (regression tests added, no code change): failure
  history SQL fault, budget off-by-one, `stop_agent` first-`wait`
  `TimeoutExpired`, `check_env_compatible` result-dict, global
  `time.sleep` patch.
- Doc correction: SECURITY.md described aspirational systemd sandboxing
  (`ProtectHome`/`ProtectSystem`, dedicated user) — now describes the
  real unit.

## Bug-fix pass (2026-09-28) — items 6–14 (second pass)

Each item below was reproduced with a failing regression test through
the real `AgentRuntime`/`Supervisor` path before the fix, then verified
passing after; the full suites (`test_reliability.py`,
`test_phase3.py`, `test_e2e_wiring.py`, `test_compound.py`) were green
after every commit (162 passed at the end of the pass;
`tests/test_vm.py` needs a live `/opt/vm-agent` install and was not run).

- Implemented: value-based redaction of vault-resolved secrets
  (`redact_values` through `_resolve_vault_refs` → `Executor.run` →
  journal + tool results); integrity baselines recorded at
  install/first-boot with `TRACKED=[config/config.json]`;
  `failure_fingerprints.operation` column wired through record →
  recover → `recover --dry-run`; `Policy.authorize_path` guarding
  `write_file`/`mkdir` against the install prefix's protected trees.
- Fixed: `recover --dry-run` `KeyError`; control-token
  chmod-after-create race (atomic `O_CREAT|O_EXCL` 0600, `run/` 0700);
  `process_running` shell interpolation (pure-Python `/proc` scan);
  policy pattern gaps (power actions, `init`, state-destruction forms);
  stale task snapshot in `_run_step`; dead lease loop in
  `reconcile_on_boot` (now actually re-queues RUNNING → PENDING);
  unbounded spec `timeout_s` (new `tool_timeout_max_s` cap);
  `CONTROL_FAILED` now logs the exception type explicitly.
- Doc corrections: SECURITY.md §Protected operations and
  ARCHITECTURE.md §5 now state the policy layer is a best-effort
  guardrail against accidental damage, not a security boundary — the
  real boundary is the OS user the service runs as plus the systemd
  hardening that ships (`NoNewPrivileges`, `PrivateTmp`).
- Deliberately not changed: `tests/test_vm.py` (needs a live install;
  out of scope); pid-recycled heartbeats in reconcile (pre-existing
  limitation, unchanged semantics).

## Known gaps (documented, not fixed — see RECOVERY.md)

- **Capability enforcement lives in the agent loop, not the executor.**
  A direct `Executor.run()` caller bypasses capability checks.
- **The vault is process-local by design.** Secrets die with the process;
  they must be re-provisioned after every restart.
- **Preemption is cooperative.** It happens at step boundaries; one very
  long step delays preemption until it finishes or its tool timeout fires.
- **Failure fingerprints are error-class based.** Distinct root causes
  with the same error class share a signature (task_id is deliberately
  excluded; pids/timestamps normalized away).
- **Stall detection needs observable writes.** Steps must record progress
  in `progress.*` world keys or they look stalled after 3 unchanged
  cycles.
- **Fault injection runs against isolated temp homes.** It proves the
  recovery logic, not the installed runtime on a real host.
- **Self-update canary is a smoke test.** It cannot prove the absence of
  behavioral regressions; the pipeline is opt-in and allowlist-gated.
- **6 root-required integration tests were syntax-verified only**
  (`tests/test_vm.py`). They need a real root install (`/opt/vm-agent`)
  and were not executed live in this environment; they are deselected
  here (the old "5 pre-existing failures" note had a stale count).
- **`supervisor.stop_agent`'s post-SIGKILL wait is unwrapped.** The first
  `p.wait()` is guarded (SIGTERM-ignoring child → SIGKILL, pinned by
  test); the second `p.wait(timeout=10)` after `p.kill()` would raise
  `TimeoutExpired` out of `stop_agent` for a SIGKILL-immune (D-state)
  process.

## Verdict

The architecture holds under the tests that were run: no silent data
loss (checkpoint-before-advance, op registry, reconcile-before-retry),
no unbounded retry (classification-governed, exhausted strategies
escalate), no mass-expiry on clock fault, no duplicate side effects on
reconciled ops, and self-protection checked before any other
classification. The honest-limits sections of RECOVERY.md are the
operational boundary of this audit: anything claimed there and nothing
more.
