# SECURITY

## Privilege

- The installer does not create a dedicated system user. The service runs
  as whoever ran the installer (`User=` is set to that account; an install
  run as root runs the service as root — prefer installing as a regular
  user).
- The systemd unit sets `NoNewPrivileges=true` and `PrivateTmp=true` only.
  `ProtectHome`/`ProtectSystem` are intentionally omitted: the agent
  legitimately needs `$HOME` (its state dir may live there) and a working
  `/tmp` for tool temp files, so strict systemd sandboxing would break it.
  Confinement is by Unix user permissions, not by systemd sandboxing — the
  service can write anywhere its user can, with state kept under
  `VM_AGENT_HOME` (`/opt/vm-agent` by default).

## Secrets

- Never written to logs: the executor redacts `*token*`, `*secret*`,
  `*password*`, `*key*`, `*auth*` argument values.
- No credentials are hard-coded. Task specs should reference secret *locations*,
  not values.
- `state/` is `chmod 700`.

## Protected operations

The policy layer refuses: shutdown/reboot/halt/poweroff (including
path-qualified, `env`-wrapped, and subshell forms), disabling or
stopping the vm-agent unit, deleting the state DB / journal / checkpoints,
and killing the supervisor. The `write_file`/`mkdir` tools are likewise
refused paths under the install prefix's `state/`, `lib/vmagent/`,
`config/`, and `run/` trees (resolved against symlinks and `..`). There
is no in-agent mechanism to approve these —
refusal is the default. (An out-of-band approval token is a future extension.)

Be realistic about what this is: regex matching on shell strings and path
prefix checks are a best-effort guardrail against *accidental* damage by a
cooperating agent, not a security boundary against a hostile one. Regex
cannot sandbox a shell, and a determined adversary with shell access can
evade pattern matching. The real security boundary is the OS user the
service runs as (install as an unprivileged user) plus the systemd
hardening that ships in the unit (`NoNewPrivileges=true`,
`PrivateTmp=true`; `ProtectHome`/`ProtectSystem` are intentionally omitted
per Privilege above).

## Network

- No management interface is exposed. The optional health endpoint is
  disabled by default (`health_port: 0`); if enabled, bind it to localhost.
- The agent makes no inbound network connections; tools only make the
  outbound calls their specs declare.

## What this does NOT do

- It does not bypass VM power-off, provider suspension, or hypervisor limits.
- It does not weaken SSH, firewalls, or OS security controls to "make it work".
