# Gap Analysis: Phases 26–60 vs Current Implementation

Date: 2026-09-27. Base build (phases 1–25) is live at ~/.vm-agent and on kilomene/vm main (20 files).

## What already exists (reuse, don't rewrite)
- Supervisor: crash/hang detection, exp backoff, diagnostics, exclusive flock
- Agent task engine: checkpoints, idempotent resume via idempotent_check, heartbeats
- Executor: per-op timeouts, ToolResult observations
- Verifier: file/command/port/http/process checks; PASS/FAIL verdicts
- Policy: SAFE/RESTRICTED/PROTECTED classification + authorize()
- State: SQLite tasks/checkpoints/heartbeats/kv/journal/restarts
- CLI: status/health/logs/tasks/submit/restart/diagnostics/recover
- Installer/uninstaller/systemd unit; 6 integration tests

## Gaps by phase
| Phase | Needed | Status |
|-------|--------|--------|
| 26 self-healing deps | dep check/diagnose/repair/verify cycle | MISSING |
| 27 deadlock detection | wait-graph + cycle detection | MISSING |
| 28 stale lock recovery | locks with owner/pid/heartbeat/expiry | MISSING (no lock table) |
| 29 transactional tools | prepare/execute/verify/commit/rollback | PARTIAL (verify only) |
| 30 idempotent op IDs | global operation registry | PARTIAL (per-step idempotent_check only) |
| 31 exactly/at-least-once | UNKNOWN state + reconcile | MISSING |
| 32 persistent leases | lease table + claim protocol | MISSING |
| 33 startup reconciliation | env-vs-state reconcile on boot | PARTIAL (task resume only) |
| 34 explicit world state | verifier-only world_state table | PARTIAL (generic kv only) |
| 35 claims vs observations | separate model_claims store | MISSING |
| 36 context recovery | recovery summary save/reload | MISSING |
| 37 model failure recovery | retry/fallback/safe-state | MISSING (no model adapter yet) |
| 38 model output validation | strict schema validation | MISSING |
| 39 action budgets | per-task limits + pause on exceed | MISSING |
| 40 escalating recovery L1–L8 | recovery manager | MISSING |
| 41 safe mode | safe-mode state + behavior | MISSING |
| 42 resource pressure | monitor + shed load | PARTIAL (config limits only) |
| 43 network recovery | failure classification + backoff | MISSING |
| 44 browser recovery | browser subsystem w/ restart+verify | PARTIAL (UNAVAILABLE slot only) |
| 45 file integrity | sha256 store + verify | MISSING |
| 46 config protection | hash + auth for protected config | PARTIAL (policy patterns only) |
| 47 recovery snapshots | pre-op snapshot/restore | MISSING |
| 48 intervention queue | persistent queue table | MISSING |
| 49 audit trail | structured audit, secrets redacted | PARTIAL (journal only) |
| 50 remote control | localhost + token auth API | MISSING |
| 51 priority queues | CRITICAL/HIGH/NORMAL/LOW + fair sched | MISSING |
| 52 state-aware cancel | REQUESTED→STOPPING→CLEANUP→CHECKPOINT→CANCELLED | PARTIAL (PAUSED only) |
| 53 graceful shutdown | full 9-step sequence | PARTIAL (SIGTERM→PAUSED only) |
| 54 DB recovery | integrity check + backup + restore | MISSING |
| 55 disk-full recovery | detect + pause + rotate + clean | MISSING |
| 56 test matrix | ~25 tests | PARTIAL (6 exist) |
| 57 failure injection | kill/corrupt/fill/network-loss tests | PARTIAL |
| 58 guarantees docs | what IS/ISN'T guaranteed | MISSING |
| 59 observability | one full diagnostic command | PARTIAL (health only) |
| 60 integration | single supervisor/DB/verifier/recovery | TO VERIFY |

## Build plan (new modules, all extending existing)
- `src/reliability.py` — RecoveryManager (L1–L8 escalation), safe mode, deadlock detection, startup reconciliation
- `src/locks.py` — lock + lease manager (tables: locks, leases)
- `src/deps.py` — dependency self-healing (check/diagnose/repair/verify)
- `src/world.py` — explicit world state (verifier-only) + model claims store
- `src/txn.py` — transactional tool execution + operation registry (op IDs, UNKNOWN reconcile)
- `src/budgets.py` — action budgets
- `src/net.py` — network failure classifier + retry policies
- `src/browserx.py` — browser recovery subsystem
- `src/integrity.py` — file integrity hashes + config protection
- `src/intervene.py` — human intervention queue
- `src/resources.py` — resource pressure monitor + response
- `src/remote.py` — localhost control API with token auth
- `src/model.py` — model adapter stub: failure recovery, output validation, context recovery
- Extend: state.py (new tables), agent.py (integrate), supervisor.py (recovery mgr hook), cli.py (diagnose/locks/etc), config.py (new knobs)
- Tests: extend tests/test_vm.py; Docs: RECOVERY.md guarantees, ARCHITECTURE.md update
