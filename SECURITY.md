# SECURITY

## Privilege

- The installer creates a dedicated `vmagent` system user and runs the
  service as that user (falls back to root only if user creation fails).
- `NoNewPrivileges=true`, `PrivateTmp=true`, `ProtectSystem=strict`,
  `ProtectHome=true` in the systemd unit. The service can write only to
  `/opt/vm-agent`.

## Secrets

- Never written to logs: the executor redacts `*token*`, `*secret*`,
  `*password*`, `*key*`, `*auth*` argument values.
- No credentials are hard-coded. Task specs should reference secret *locations*,
  not values.
- `state/` is `chmod 700`.

## Protected operations

The policy layer refuses: shutdown/reboot/halt/poweroff, disabling or
stopping the vm-agent unit, deleting the state DB / journal / checkpoints,
and killing the supervisor. There is no in-agent mechanism to approve these —
refusal is the default. (An out-of-band approval token is a future extension.)

## Network

- No management interface is exposed. The optional health endpoint is
  disabled by default (`health_port: 0`); if enabled, bind it to localhost.
- The agent makes no inbound network connections; tools only make the
  outbound calls their specs declare.

## What this does NOT do

- It does not bypass VM power-off, provider suspension, or hypervisor limits.
- It does not weaken SSH, firewalls, or OS security controls to "make it work".
