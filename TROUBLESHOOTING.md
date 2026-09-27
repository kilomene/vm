# TROUBLESHOOTING

## `vm-agent health` shows supervisor down

```bash
systemctl status vm-agent.service
journalctl -u vm-agent.service -n 50
tail -30 /opt/vm-agent/logs/supervisor.out
```
Common cause: file ownership. The service runs as `vmagent`; if the DB or
dirs are root-owned, SQLite can't open. Fix: `chown -R vmagent:vmagent /opt/vm-agent`.

## Agent keeps restarting (RESTART_REFUSED in journal)

More than 12 restarts/hour — the supervisor stays down to avoid a hot loop.
Inspect `vm-agent diagnostics` and the `diag-*.json` files in `logs/`, fix the
root cause, then `systemctl restart vm-agent`.

## Task stuck in RUNNING but agent is idle

The agent heartbeats every 10s. If `vm-agent status` shows a stale heartbeat,
the supervisor's hang detector will restart it within ~10 minutes. To force:
`vm-agent restart`.

## A step keeps failing verification

Read the verdict: `vm-agent diagnostics` won't show it directly — query SQLite:
```bash
sqlite3 /opt/vm-agent/state/state.db \
  "SELECT last_verified_result FROM tasks WHERE task_id='YOUR_ID';"
```
Fix the `verify` block or the command, then re-submit (or mark the task and
let `idempotent_check` skip what's done).

## Policy refuses my command

`PROTECTED` patterns block shutdown/reboot/halt/poweroff (as commands),
`systemctl disable/stop/mask vm-agent`, deleting state, killing the supervisor.
If your legitimate command contains one of those words in a *path*, that's a
false positive — file an issue; the patterns match command position, not
substrings, but edge cases exist.

## Logs growing

`logs/agent.out` and `supervisor.out` are appended forever. Rotate:
```bash
mv /opt/vm-agent/logs/agent.out /opt/vm-agent/logs/agent.out.1
systemctl restart vm-agent
```
(Built-in rotation is on the roadmap; journal JSONL is per-day already.)

## Database locked / corrupted

SQLite is single-writer here and all access goes through one lock. If the DB
is ever corrupted, restore from the last good state: task specs are also in
the journal (`TASK_QUEUED` entries), so tasks can be re-submitted.
