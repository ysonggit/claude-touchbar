#!/usr/bin/env bash
# One-step installer for Claude Touch Bar.   ./install.sh [--yes] | --check | --uninstall
#
# Tested only on a MacBook Pro 2016 15" (MacBookPro13,3, T1) with Ubuntu 22.04. Run it as your normal
# user from the repo folder; it asks for sudo for the root-level steps and says what each one does.
#  1. checks the machine and installs missing Python packages (python3-cairo, python3-pil)
#  2. checks the stock Touch Bar driver and blacklists hid_sensor_hub (rebuilds the initramfs)
#  3. finds or builds barkeep's kernel modules (optionally clones https://github.com/mgd34msu/barkeep)
#  4. user level: Claude Code hooks + statusLine + the ctb-bridge user service   (bin/ctb install --enable)
#  5. root level: ctb-bar.service, ctb-display, sleep hook, the "Claude Touch Bar" app
#                                                                               (packaging/install-root.sh)
# Nothing is enabled at boot: open the "Claude Touch Bar" app to take over the Touch Bar.
# --check runs steps 1-3 as checks only and changes nothing.
set -uo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
BARKEEP=${CTB_BARKEEP_DIR:-$HOME/barkeep}
YES=0
G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; B='\033[1m'; N='\033[0m'
step() { echo -e "\n${B}== $*${N}"; }
ok()   { echo -e "${G}[ok]${N} $*"; }
warn() { echo -e "${Y}[!]${N} $*"; }
die()  { echo -e "${R}[x]${N} $*"; exit 1; }
ask()  {
    if [ "${CHECK:-0}" = 1 ]; then echo "  (check only: would ask \"$1\")"; return 1; fi
    [ "$YES" = 1 ] && return 0; read -r -p "$1 [y/N] " a; [ "$a" = y ] || [ "$a" = Y ]
}

uninstall() {
    step "Removing Claude Touch Bar"
    sudo "$ROOT/packaging/uninstall-root.sh"
    "$ROOT/bin/ctb" uninstall
    ok "Removed. The hid_sensor_hub blacklist and barkeep were left in place."
    echo "Reboot to get the stock Touch Bar icons back if the app was running."
    exit 0
}

for a in "$@"; do
    case "$a" in
        --yes|-y) YES=1 ;;
        --uninstall) UNINSTALL=1 ;;
        --check) CHECK=1 ;;
        -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option $a (see --help)" ;;
    esac
done
[ "$(id -u)" != 0 ] || die "run as your normal user, not with sudo (it asks for sudo itself)"
[ "${UNINSTALL:-0}" = 1 ] && uninstall

step "1/5 Machine and packages"
model=$(cat /sys/class/dmi/id/product_name 2>/dev/null)
case "$model" in
    MacBookPro13,3) ok "$model (the tested model)" ;;
    MacBookPro13,2|MacBookPro14,2|MacBookPro14,3) warn "$model has a T1 Touch Bar but is untested" ;;
    *) warn "'$model' is not a T1 MacBook Pro; this project only drives the T1 Touch Bar"
       ask "Continue anyway?" || exit 1 ;;
esac
. /etc/os-release 2>/dev/null
[ "${VERSION_ID:-}" = 22.04 ] && [ "${ID:-}" = ubuntu ] && ok "Ubuntu 22.04" \
    || warn "${PRETTY_NAME:-unknown OS}: untested (tested on Ubuntu 22.04)"
command -v claude >/dev/null && ok "Claude Code: $(claude --version 2>/dev/null)" \
    || warn "Claude Code (claude) not found in PATH; install it before using the bar"
missing=()
python3 -c "import cairo" 2>/dev/null || missing+=(python3-cairo)
python3 -c "import PIL" 2>/dev/null || missing+=(python3-pil)
if [ ${#missing[@]} -gt 0 ]; then
    ask "Install ${missing[*]} with apt?" || die "needed: ${missing[*]}"
    sudo apt-get install -y "${missing[@]}" || die "apt-get failed"
fi
ok "Python packages"

step "2/5 Stock Touch Bar driver"
if modinfo apple_ibridge >/dev/null 2>&1 && modinfo apple_ib_tb >/dev/null 2>&1; then
    ok "apple_ibridge / apple_ib_tb installed"
else
    warn "The stock T1 driver (apple_ibridge, apple_ib_tb) is not installed. It is what the Touch Bar falls"
    warn "back to; install a DKMS build for your kernel first (see packaging/README.md, step 1)."
    ask "Continue without it?" || exit 1
fi
if grep -qsx 'blacklist hid_sensor_hub' /etc/modprobe.d/*.conf; then
    ok "hid_sensor_hub already blacklisted"
else
    echo "hid_sensor_hub grabs the Touch Bar's second interface at boot, which leaves the stock bar dead."
    if ask "Blacklist it (/etc/modprobe.d/ctb-touchbar.conf) and rebuild the initramfs?"; then
        echo "blacklist hid_sensor_hub" | sudo tee /etc/modprobe.d/ctb-touchbar.conf >/dev/null
        sudo update-initramfs -u || warn "update-initramfs failed"
        ok "blacklisted (takes effect after the next reboot)"
    else
        warn "skipped: the stock bar may stay dark after boot until packaging/ctb-display down"
    fi
fi

step "3/5 barkeep (host-drawn display)"
if modinfo barkeep_dfr >/dev/null 2>&1; then
    ok "barkeep installed (DKMS)"
elif [ -f "$BARKEEP/barkeep-dfr/barkeep-dfr.ko" ] && [ -f "$BARKEEP/barkeep-cfgsel/barkeep-cfgsel.ko" ] \
     && [ "$(modinfo -F vermagic "$BARKEEP/barkeep-dfr/barkeep-dfr.ko" | cut -d' ' -f1)" = "$(uname -r)" ]; then
    ok "barkeep modules built in $BARKEEP"
else
    if [ ! -d "$BARKEEP" ]; then
        ask "barkeep not found. Clone https://github.com/mgd34msu/barkeep into $BARKEEP?" \
            || die "barkeep is required (see packaging/README.md)"
        git clone https://github.com/mgd34msu/barkeep "$BARKEEP" || die "git clone failed"
    fi
    [ -d "/lib/modules/$(uname -r)/build" ] || { ask "Install linux-headers-$(uname -r) and build-essential?" \
        && sudo apt-get install -y "linux-headers-$(uname -r)" build-essential; } \
        || die "kernel headers are needed to build barkeep"
    bash "$BARKEEP/scripts/build.sh" || die "building barkeep failed"
    ok "barkeep built in $BARKEEP"
fi

if [ "${CHECK:-0}" = 1 ]; then
    echo -e "\n${G}${B}Checks done${N} (nothing was changed). Run ./install.sh to install."
    exit 0
fi

step "4/5 User level: Claude Code hooks, statusLine, bridge"
"$ROOT/bin/ctb" install --enable || die "bin/ctb install failed"

step "5/5 Root level: service, sleep hook, app"
sudo env CTB_BARKEEP_DIR="$BARKEEP" "$ROOT/packaging/install-root.sh" || die "packaging/install-root.sh failed"

echo -e "\n${G}${B}Done.${N} Open ${B}Claude Touch Bar${N} from Activities (it asks for your password)."
echo "Right-click it -> Stop to end it. Remove everything with: ./install.sh --uninstall"
