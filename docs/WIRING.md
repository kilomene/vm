# WIRING AUDIT

Date: 2026-09-28. Method: static cross-reference (AST-based) of every call
edge between components, plus the e2e suite run against the real code.
Purpose: prove the documented wiring matches the code — every CLI
subcommand reaches a real handler, every `store.*` / `agent.*` call
resolves to a real method, every cross-module function call exists. This
is the same class of bug the phase-90 audit caught by running
(`recovery.py` calling the nonexistent `agent.pause_task()`); this pass
checks it systematically instead of by accident.

## What was checked (all green)

1. **CLI subcommands → handlers** (`src/cli.py`). Every
   `set_defaults(fn=...)` names a function defined in the module: 27
   subcommands (24 literal `add_parser` names plus the f-string-built
   `task-{pause,resume,cancel}`), including the new `task-retry` →
   `cmd_task_retry`. No dangling subcommand.
2. **Store API surface** (`src/state.py::Store`, 78 methods). Every
   `store.*` call in `agent.py`, `supervisor.py`, `recovery.py`, `cli.py`
   resolves to a real method. No missing method. (One heuristic hit —
   `s.get("tool", ...)` — was a step-dict comprehension variable, not a
   store; verified by reading the line.)
3. **AgentRuntime API surface** (`src/agent.py::AgentRuntime`, 32
   methods). Every `agent.*` call in `recovery.py`, `supervisor.py`,
   `cli.py` resolves. No missing method.
4. **Cross-module functions.** Every `module.func()` call in
   `agent.py`/`supervisor.py`/`recovery.py`/`cli.py` resolves to a real
   function or class in the named module. None missing.

## Wiring changed by the 2026-09-28 bug pass

- `task-retry --from-step N` added (`cli.py::cmd_task_retry`): resets a
  task to `PENDING` and rewinds it to the 1-based step N, i.e.
  `current_step = max(0, N - 1)`. The off-by-one the spec warned about
  (passing N straight through as `current_step`, skipping step N) is
  absent by construction; pinned by
  `test_fix12_task_retry_from_step_maps_1based_to_current_step` and
  `test_fix12_task_retry_from_step_clamps_at_zero`.
- `safe-mode exit` added (`cli.py`: `vm-agent safe-mode status|exit
  [--note]`): operator clear for safe mode; clock-caused safe mode
  auto-clears on recovery, escalation-caused never does.
- Five reported bugs were verified absent in this tree (no `budget.py`
  token check, `stop_agent`'s first `wait` already guarded, no
  `check_env_compatible` result-dict, no global `time.sleep` patch, no
  failure-history SQL fault) and pinned with regression guards instead
  of code changes. `task-retry` itself did not exist and was implemented
  per spec.

## Wiring changed by the 2026-09-28 bug pass (second pass, items 6–14)

- `Policy.authorize_path(base_dir, path)` added (`policy.py`): refuses
  `write_file`/`mkdir` under the prefix's `state/`, `lib/vmagent/`,
  `config/`, `run/` (realpath-resolved). Called from
  `agent.py::_run_step`'s retry loop and `dry_run` verdicts.
- `failure_fingerprints.operation` column added (`state.py` migration +
  fresh CREATE); `Store.recovery_record(..., operation=...)` populates
  it; `failure.record` and `RecoveryManager.recover` thread `operation`
  through; `cli.py::cmd_recover` dry-run reads the real
  `kind`/`last_method`/`operation` columns.
- `recovery.py::reconcile_on_boot`: the dead `pass` lease loop is gone;
  stuck RUNNING tasks (no live agent) are set PENDING, their leases
  released, `TASK_REQUEUED_ON_BOOT` journaled. No double-run with
  `AgentRuntime.recover()`: it resumes only RUNNING/PAUSED while
  `serve_forever` picks up only PENDING, both through `claim_lease`.
- `AgentRuntime._start_control()` extracted from `serve_forever`
  (`agent.py`); `CONTROL_FAILED` now journals
  `error_type=type(e).__name__` explicitly.
- `Executor` gains `timeout_max` from `tool_timeout_max_s` (new
  `config.py` DEFAULT, 3600s) capping `_sane_timeout`; `_run_step`
  re-reads the task row before computing `failed_steps`/`retry_count`.
- `verify.py::_process_running` is now a pure-Python `/proc` cmdline
  scan (no shell); `remote.py::ensure_token` uses atomic
  `O_CREAT|O_EXCL` mode-0600 creation; `integrity.py` gains
  `ensure_baseline()`/`recorded_count()` with `TRACKED =
  ["config/config.json"]`; `install.sh` records the integrity baseline
  at install time.
- `secrets.redact_values()` added; `_resolve_vault_refs` returns
  `(resolved_args, secret_values)`; `Executor.run(..., redact_values=...)`
  applies value-redaction in `_redact` and on `ToolResult` stdout/stderr
  before journal/return.

## Residual wiring risks (observed, not fixed)

- `supervisor.stop_agent`: the *second* `p.wait(timeout=10)` after
  SIGKILL is unwrapped. A SIGKILL-immune (D-state) agent process would
  raise `TimeoutExpired` out of `stop_agent`. The first wait is guarded
  and pinned by test; the second needs an unkillable process to
  trigger, which this environment cannot fabricate.
- `Executor._redact` redacts by argument *name* (`*token*`, `*secret*`,
  `*password*`, `*key*`, `*auth*`) for non-vault values, plus by value
  for vault-resolved secrets (fix 6). A *literal* secret typed directly
  into a step arg (not via a `vault_get` ref) is still journaled raw in
  `TOOL_STARTED`. The journal exposure scanner exists, but the write
  happens first. Do not pass literal secret values in step args — use
  the vault.

## Test evidence

Full suite after the pass: **147 passed, 6 deselected**
(`tests/test_vm.py` needs a real `/opt/vm-agent` install), ~26s.
`tests/test_e2e_wiring.py` (28 tests) pins the bug-pass behavior end to
end through real `AgentRuntime`/`Supervisor` paths.
