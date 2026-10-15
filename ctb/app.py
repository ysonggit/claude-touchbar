"""`ctb app start|stop`: what the "Claude Touch Bar" desktop launcher runs (as the user).

Makes sure the bridge is up, asks polkit to start or stop ctb-bar.service through the root helper,
and reports the result as a desktop notification.
"""

import os
import subprocess
import time

HELPER = "/usr/local/lib/ctb/ctb-launch"


def _notify(summary, body=""):
    subprocess.run(["notify-send", "-a", "Claude Touch Bar", "-i", "claude-touchbar", summary, body],
                   check=False)


def _service_active():
    return subprocess.run(["systemctl", "is-active", "--quiet", "ctb-bar.service"], check=False).returncode == 0


def t1_in_recovery():
    """True when the T1 shows up as 05ac:1281 (recovery mode) instead of the iBridge: no Touch Bar at all."""
    base = "/sys/bus/usb/devices"
    for d in os.listdir(base) if os.path.isdir(base) else ():
        try:
            with open(f"{base}/{d}/idVendor") as v, open(f"{base}/{d}/idProduct") as p:
                if (v.read().strip(), p.read().strip()) == ("05ac", "1281"):
                    return True
        except OSError:
            continue
    return False


def _helper(action):
    return subprocess.run(["pkexec", HELPER, action], check=False,
                          capture_output=True, text=True)


def main(args):
    action = args[0] if args else "start"
    if action not in ("start", "stop"):
        print("usage: ctb app start|stop")
        return 2
    if not os.path.exists(HELPER):
        _notify("Claude Touch Bar is not installed",
                "Run: sudo packaging/install-root.sh (in the claude-touchbar folder)")
        return 1
    if action == "start" and t1_in_recovery():
        _notify("The Touch Bar chip is in recovery mode",
                "Its OS is loaded from macOS, which is no longer on this disk. Reinstall macOS alongside "
                "Linux; see the README section \"Touch Bar is dead\".")
        return 1
    if action == "start":
        subprocess.run(["systemctl", "--user", "start", "ctb-bridge.service"], check=False)
        if _service_active():                # a second click must not restart (and blank) the bar
            _notify("Claude Touch Bar is already on")
            return 0
    r = _helper(action)
    if r.returncode in (126, 127):        # password dialog dismissed / not authorised
        _notify("Claude Touch Bar: cancelled")
        return 1
    if action == "stop":
        _notify("Claude Touch Bar stopped",
                "The keys work again; the stock Touch Bar icons come back after a reboot.")
        return r.returncode
    for _ in range(30):                   # `ctb-display up` re-enumerates USB (~10 s)
        if _service_active() and os.path.exists("/dev/dfr0"):
            _notify("Claude Touch Bar is on", "Open Claude Code to see its status on the bar.")
            return 0
        time.sleep(1)
    _notify("Claude Touch Bar did not start",
            "See: journalctl -u ctb-bar -b. The stock Touch Bar has been restored.")
    return 1
