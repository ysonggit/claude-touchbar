#!/bin/sh
# Installs the ROOT-level pieces of Claude Touch Bar. Run by hand:  sudo packaging/install-root.sh
# It does NOT install barkeep, does NOT touch your loaded drivers, and does NOT enable the service.
set -eu
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)"; exit 1; }
HERE=$(cd "$(dirname "$0")" && pwd); ROOT=$(dirname "$HERE")
UID_=${SUDO_UID:-1000}
# barkeep's built source tree, used when its modules are not installed with DKMS.
BARKEEP=${CTB_BARKEEP_DIR:-$(getent passwd "$UID_" | cut -d: -f6)/barkeep}

if modinfo barkeep_dfr >/dev/null 2>&1 && modinfo barkeep_cfgsel >/dev/null 2>&1; then
    echo "barkeep: using the installed (DKMS) modules"
elif [ -f "$BARKEEP/barkeep-dfr/barkeep-dfr.ko" ] && [ -f "$BARKEEP/barkeep-cfgsel/barkeep-cfgsel.ko" ]; then
    echo "barkeep: using the modules built in $BARKEEP"
else
    echo "barkeep kernel modules not found (not installed, and not built in $BARKEEP)."
    echo "Build them (bash $BARKEEP/scripts/build.sh) or set CTB_BARKEEP_DIR. See packaging/README.md."
    echo "Continuing anyway: the service will fail to start until they exist."
fi
install -D -m 0755 "$HERE/ctb-display" /usr/local/lib/ctb/ctb-display
sed -e "s|@ROOT@|$ROOT|g" -e "s|@UID@|$UID_|g" -e "s|@BARKEEP@|$BARKEEP|g" \
    "$HERE/ctb-bar.service.in" > /etc/systemd/system/ctb-bar.service
install -D -m 0755 "$HERE/ctb-sleep" /usr/lib/systemd/system-sleep/ctb-bar
# The "Claude Touch Bar" app: launcher, icon, and a polkit-guarded root helper that only starts/stops ctb-bar.
install -D -m 0755 "$HERE/ctb-launch" /usr/local/lib/ctb/ctb-launch
install -D -m 0644 "$HERE/com.github.ysonggit.claude-touchbar.policy" \
    /usr/share/polkit-1/actions/com.github.ysonggit.claude-touchbar.policy
install -D -m 0644 "$HERE/claude-touchbar.svg" /usr/share/icons/hicolor/scalable/apps/claude-touchbar.svg
sed -e "s|@ROOT@|$ROOT|g" "$HERE/claude-touchbar.desktop.in" > /usr/share/applications/claude-touchbar.desktop
gtk-update-icon-cache -q /usr/share/icons/hicolor 2>/dev/null || true
update-desktop-database -q /usr/share/applications 2>/dev/null || true
[ -e /etc/ctb/bar.toml ] || install -D -m 0644 "$HERE/bar.toml" /etc/ctb/bar.toml
systemctl daemon-reload
echo "Installed ctb-bar.service (NOT enabled or started), /usr/local/lib/ctb/, sleep hook, /etc/ctb/bar.toml,"
echo "and the \"Claude Touch Bar\" app (Activities -> search \"Claude Touch Bar\")."
echo "Open the app to start it (asks for your password); right-click it -> Stop to end it."
