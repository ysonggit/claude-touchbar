#!/bin/sh
# Removes what install-root.sh installed and gives the Touch Bar back to the stock drivers.
set -eu
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)"; exit 1; }
systemctl disable --now ctb-bar.service 2>/dev/null || true   # ExecStopPost reloads the stock drivers
# In case the service was never running: this also rebinds both iBridge HID interfaces.
"$(dirname "$0")/ctb-display" down || true
rm -f /etc/systemd/system/ctb-bar.service /usr/lib/systemd/system-sleep/ctb-bar
rm -rf /usr/local/lib/ctb
systemctl daemon-reload
echo "Removed. /etc/ctb/bar.toml was kept (delete it yourself if you like)."
