#!/usr/bin/env bash
# vm-agent uninstaller. Task data is KEPT by default.
set -euo pipefail

PREFIX="${VM_AGENT_HOME:-$HOME/.vm-agent}"
PURGE=0
for a in "$@"; do [ "$a" = "--purge" ] && PURGE=1; done

echo "=== vm-agent uninstaller ==="

# stop supervisor (systemd or direct)
if [ -f /etc/systemd/system/vm-agent.service ]; then
  systemctl stop vm-agent.service 2>/dev/null || true
  systemctl disable vm-agent.service 2>/dev/null || true
  rm -f /etc/systemd/system/vm-agent.service
  systemctl daemon-reload 2>/dev/null || true
  echo "systemd service removed"
fi
pkill -f "vmagent.supervisor" 2>/dev/null || true
echo "supervisor stopped"

if [ "$PURGE" = "1" ]; then
  echo "PURGING all data (state, journals, checkpoints, logs)…"
  rm -rf "$PREFIX"
  echo "purged $PREFIX"
else
  for d in bin lib scripts; do rm -rf "$PREFIX/$d"; done
  echo "removed program files; KEPT task data in:"
  echo "  $PREFIX/state  $PREFIX/checkpoints  $PREFIX/logs"
  echo "re-run install.sh to reinstall; use --purge to delete everything"
fi

echo "=== uninstalled ==="
