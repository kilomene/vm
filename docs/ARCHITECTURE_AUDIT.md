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

All six were invisible to unit-style reading and surfaced only when the
agent loop actually ran the paths. This is why the phase-89 compound
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
- **6 root-required integration tests were syntax-verified only.**
  They need a real root install (`/opt/vm-agent`) and were not executed
  live in this environment.
- **`tests/test_vm.py` has 5 pre-existing failures** from the missing
  `/opt/vm-agent` binary (environment limitation, unrelated to this
  phase).

## Verdict

The architecture holds under the tests that were run: no silent data
loss (checkpoint-before-advance, op registry, reconcile-before-retry),
no unbounded retry (classification-governed, exhausted strategies
escalate), no mass-expiry on clock fault, no duplicate side effects on
reconciled ops, and self-protection checked before any other
classification. The honest-limits sections of RECOVERY.md are the
operational boundary of this audit: anything claimed there and nothing
more.
