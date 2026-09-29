#!/usr/bin/env bash
# vm-agent installer — idempotent: safe to run again to repair/upgrade.
#
# Install location: defaults to $HOME/.vm-agent (persistent across reboots
# in container environments where /opt and /etc are ephemeral).
# Override with VM_AGENT_HOME=/your/path
set -euo pipefail

PREFIX="${VM_AGENT_HOME:-$HOME/.vm-agent}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WHO="$(whoami)"

echo "=== vm-agent installer ==="
echo "prefix: $PREFIX"
echo "source: $SRC_DIR"
echo "user: $WHO"

# 1. Detect environment
echo "--- environment ---"
uname -a
python3 --version || { echo "FATAL: python3 required"; exit 1; }
python3 -c "import sqlite3" || { echo "FATAL: python3 sqlite3 required"; exit 1; }
if [ -d /run/systemd/system ]; then
  echo "init: systemd (usable)"
  HAVE_SYSTEMD=1
else
  echo "init: no systemd"
  HAVE_SYSTEMD=0
fi

# 2/3. Requirements / dependencies — stdlib only, nothing to install.

# 4. Create directories
echo "--- directories ---"
for d in bin lib config state/journal checkpoints logs run; do
  mkdir -p "$PREFIX/$d"
done

# 5. Install code
echo "--- installing code ---"
mkdir -p "$PREFIX/lib/vmagent"
cp -r "$SRC_DIR/src/." "$PREFIX/lib/vmagent/"
# cli entry point (uses its own location to find PREFIX)
cat > "$PREFIX/bin/vm-agent" <<EOF
#!/usr/bin/env bash
BIN_DIR="\$(cd "\$(dirname "\${BASH_SOURCE[0]}")" && pwd)"
export VM_AGENT_HOME="\${VM_AGENT_HOME:-\$(dirname "\$BIN_DIR")}"
export PYTHONPATH="\$VM_AGENT_HOME/lib"
exec /usr/bin/python3 -m vmagent.cli "\$@"
EOF
chmod +x "$PREFIX/bin/vm-agent"

# default config (never overwrite an existing one)
if [ ! -f "$PREFIX/config/config.json" ]; then
  echo '{}' > "$PREFIX/config/config.json"
fi

# 8. Initialize database (creates schema on first Store open)
VM_AGENT_HOME="$PREFIX" PYTHONPATH="$PREFIX/lib" python3 -c "
from vmagent.state import Store
from vmagent import config, integrity
cfg = config.load('$PREFIX')
s = Store(cfg['state_db'], cfg['journal_dir'])
s.journal('INSTALL', version='1.0.0')
# install-time integrity baseline: verify() compares against these
# hashes, so without this the boot-time integrity check is vacuous.
recorded = integrity.record(s, '$PREFIX')
print('integrity baseline recorded:', sorted(recorded))
s.close()
print('database initialized:', cfg['state_db'])
"
chmod 700 "$PREFIX/state"

# 9/10. Supervisor + watchdog live in lib/vmagent (installed above).

# 11. Automatic startup
SYSTEMD_UNIT=""
if [ "$HAVE_SYSTEMD" = "1" ] && [ -w /etc/systemd/system ]; then
  echo "--- systemd (system) ---"
  sed -e "s|/opt/vm-agent|$PREFIX|g" \
      "$SRC_DIR/systemd/vm-agent.service" > /etc/systemd/system/vm-agent.service
  # run as current user when not root
  if [ "$WHO" != "root" ]; then
    sed -i "/^\[Service\]/a User=$WHO" /etc/systemd/system/vm-agent.service
  fi
  systemctl daemon-reload
  systemctl enable vm-agent.service && echo "enabled vm-agent.service"
  SYSTEMD_UNIT="system"
elif [ "$HAVE_SYSTEMD" = "1" ]; then
  echo "--- systemd present but /etc not writable; skipping unit install ---"
fi

# Boot script (always installed — used by systemd ExecStart or manually)
mkdir -p "$PREFIX/scripts"
cat > "$PREFIX/scripts/boot.sh" <<EOF2
#!/usr/bin/env bash
# Start the supervisor (what systemd runs on boot).
export VM_AGENT_HOME="$PREFIX"
export PYTHONPATH="$PREFIX/lib"
exec /usr/bin/python3 -m vmagent.supervisor
EOF2
chmod +x "$PREFIX/scripts/boot.sh"

# 12. Start services
if [ "$SYSTEMD_UNIT" = "system" ]; then
  systemctl restart vm-agent.service || systemctl start vm-agent.service
  sleep 3
else
  echo "--- starting supervisor directly (no persistent systemd) ---"
  # kill any stale supervisor first (idempotent)
  pkill -f "vmagent.supervisor" 2>/dev/null || true
  sleep 1
  nohup "$PREFIX/scripts/boot.sh" > "$PREFIX/logs/supervisor.out" 2>&1 &
  sleep 3
fi

# 13. Health checks
echo "--- health check ---"
if "$PREFIX/bin/vm-agent" health; then
  echo "HEALTH: OK"
else
  echo "HEALTH: not yet up (supervisor may still be starting the agent)"
fi

# 14. Final status
echo "=== installed ==="
echo "install dir : $PREFIX"
echo "cli         : $PREFIX/bin/vm-agent"
if [ "$SYSTEMD_UNIT" = "system" ]; then
echo "service     : vm-agent.service (systemd)"
else
echo "service     : supervisor running directly (no persistent systemd here)"
echo "boot script : $PREFIX/scripts/boot.sh"
fi
echo "status      : $PREFIX/bin/vm-agent status"
echo ""
echo "Add to PATH: export PATH=\"\$PATH:$PREFIX/bin\""
echo ""
echo "Limitation: if the VM itself is powered off, nothing runs."
echo "On boot, the supervisor starts (via systemd or boot.sh), then the agent,"
echo "which recovers unfinished tasks from SQLite."
