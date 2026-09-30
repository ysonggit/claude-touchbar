"""`ctb install` / `uninstall` / `doctor`: user-level setup. Root-level steps live in packaging/."""

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time

from . import config as cfgmod

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK_BIN = os.path.join(ROOT, "bin", "ctb-hook")
STATUS_BIN = os.path.join(ROOT, "bin", "ctb-status")

TOOL_EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionRequest")
EVENTS = ("SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PostToolUse",
          "PostToolUseFailure", "PermissionRequest", "SubagentStart", "SubagentStop",
          "PreCompact", "PostCompact", "Notification", "Stop", "StopFailure")
HOOK_TIMEOUT = 120  # prompt_timeout_s (110) must stay below this


def settings_path():
    return os.environ.get("CTB_SETTINGS") or os.path.expanduser("~/.claude/settings.json")


def manifest_path():
    return os.path.join(os.path.dirname(cfgmod.config_path()), "install.json")


def unit_path():
    return os.path.expanduser("~/.config/systemd/user/ctb-bridge.service")


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def _is_ours(entry):
    return any(os.path.basename(h.get("command", "").split()[0] if h.get("command") else "") == "ctb-hook"
               for h in entry.get("hooks", []))


def _hook_entry(event):
    e = {"hooks": [{"type": "command", "command": HOOK_BIN,
                    "timeout": HOOK_TIMEOUT if event == "PermissionRequest" else 5}]}
    if event in TOOL_EVENTS:
        e = {"matcher": "*", **e}
    return e


def merge(settings):
    """Add our hooks/statusLine to a settings dict (in place). Returns the original statusLine."""
    hooks = settings.setdefault("hooks", {})
    for ev in EVENTS:
        lst = hooks.setdefault(ev, [])
        lst[:] = [e for e in lst if not _is_ours(e)] + [_hook_entry(ev)]
    orig = settings.get("statusLine")
    if not (orig and os.path.basename((orig.get("command") or "").split()[0]) == "ctb-status"):
        settings["statusLine"] = {"type": "command", "command": STATUS_BIN}
    return orig


def strip(settings, orig_status):
    hooks = settings.get("hooks", {})
    for ev in list(hooks):
        hooks[ev] = [e for e in hooks[ev] if not _is_ours(e)]
        if not hooks[ev]:
            del hooks[ev]
    if not hooks:
        settings.pop("hooks", None)
    sl = settings.get("statusLine")
    if sl and os.path.basename((sl.get("command") or "").split()[0]) == "ctb-status":
        if orig_status:
            settings["statusLine"] = orig_status
        else:
            del settings["statusLine"]


UNIT = """[Unit]
Description=Claude Touch Bar bridge

[Service]
ExecStart=/usr/bin/python3 -m ctb.bridge
WorkingDirectory={root}
Environment=PYTHONPATH={root}
Restart=on-failure

[Install]
WantedBy=default.target
"""


def install(args):
    path = settings_path()
    dry = "--dry-run" in args
    existed = os.path.exists(path)
    raw = open(path, "rb").read() if existed else b""
    settings = json.loads(raw) if raw.strip() else {}
    orig = merge(settings)
    out = (json.dumps(settings, indent=2) + "\n").encode()
    if dry:
        sys.stdout.write(out.decode())
        return 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        prev = json.load(open(manifest_path()))
    except (OSError, ValueError):
        prev = None
    if prev and prev.get("settings") == path:
        # re-install: keep the ORIGINAL backup / statusLine so uninstall can restore byte-for-byte
        manifest = dict(prev, written_sha=_sha(out))
        orig = prev.get("orig_statusline")
    else:
        backup = None
        if existed:
            backup = "%s.ctb-bak-%d" % (path, time.time())
            shutil.copy2(path, backup)
        manifest = {"settings": path, "backup": backup, "existed": existed,
                    "written_sha": _sha(out), "orig_statusline": orig}
    backup = manifest["backup"]
    with open(path, "wb") as f:
        f.write(out)
    os.makedirs(os.path.dirname(manifest_path()), exist_ok=True)
    json.dump(manifest, open(manifest_path(), "w"), indent=2)
    # keep the user's existing status line alive through ctb-status (R-52)
    if orig and orig.get("command") and os.path.basename(orig["command"].split()[0]) != "ctb-status":
        cp = cfgmod.config_path()
        if not os.path.exists(cp):
            with open(cp, "w") as f:
                f.write('[statusline]\npassthrough = "%s"\n' % orig["command"].replace('"', '\\"'))
            manifest["wrote_config"] = cp
            json.dump(manifest, open(manifest_path(), "w"), indent=2)
        elif manifest.get("wrote_config") != cp:
            print("note: existing statusLine %r not wired automatically; set [statusline] passthrough "
                  "in %s" % (orig["command"], cp))
    if "--no-systemd" not in args:
        os.makedirs(os.path.dirname(unit_path()), exist_ok=True)
        open(unit_path(), "w").write(UNIT.format(root=ROOT))
        if "--enable" in args:
            subprocess.run(["systemctl", "--user", "daemon-reload"])
            subprocess.run(["systemctl", "--user", "enable", "--now", "ctb-bridge.service"])
    print("Installed hooks + statusLine into %s%s" % (path, " (backup: %s)" % backup if backup else ""))
    if "--enable" not in args and "--no-systemd" not in args:
        print("Start the bridge:  systemctl --user daemon-reload && systemctl --user enable --now ctb-bridge")
    print("Root-level steps (Touch Bar drivers + ctb-bar service) are NOT done here: see packaging/README.md")
    return 0


def uninstall(args):
    try:
        man = json.load(open(manifest_path()))
    except FileNotFoundError:
        print("not installed (no manifest)")
        return 1
    path = man["settings"]
    cur = open(path, "rb").read() if os.path.exists(path) else None
    if cur is not None and _sha(cur) == man["written_sha"]:
        # untouched since install → restore the original bytes exactly
        if man["existed"] and man["backup"] and os.path.exists(man["backup"]):
            shutil.copy2(man["backup"], path)
            os.unlink(man["backup"])
        elif not man["existed"]:
            os.unlink(path)
    elif cur is not None:
        settings = json.loads(cur)
        strip(settings, man.get("orig_statusline"))
        open(path, "w").write(json.dumps(settings, indent=2) + "\n")
    if man.get("wrote_config") and os.path.exists(man["wrote_config"]):
        os.unlink(man["wrote_config"])
    if os.path.exists(unit_path()):
        subprocess.run(["systemctl", "--user", "disable", "--now", "ctb-bridge.service"],
                       capture_output=True)
        os.unlink(unit_path())
    os.unlink(manifest_path())
    print("Uninstalled. (Root-level parts, if you installed them, are removed with packaging/uninstall-root.sh)")
    return 0


# ---------------------------------------------------------------- doctor

def _modules():
    try:
        return {ln.split()[0] for ln in open("/proc/modules")}
    except OSError:
        return set()


def doctor(args):
    rows = []

    def chk(ok, name, detail="", warn=False):
        rows.append(("OK  " if ok else "WARN" if warn else "FAIL", name, detail))

    sp = settings_path()
    try:
        s = json.load(open(sp))
        have = [ev for ev in EVENTS if any(_is_ours(e) for e in s.get("hooks", {}).get(ev, []))]
        chk(len(have) == len(EVENTS), "settings.json hooks", "%d/%d events wired in %s" % (len(have), len(EVENTS), sp))
        sl = (s.get("statusLine") or {}).get("command", "")
        chk(os.path.basename(sl.split()[0] if sl else "") == "ctb-status", "statusLine", sl or "not set")
    except (OSError, ValueError) as e:
        chk(False, "settings.json", str(e))
    try:
        t = time.perf_counter()
        sk = socket.socket(socket.AF_UNIX); sk.settimeout(0.2); sk.connect(cfgmod.user_sock()); sk.close()
        chk(True, "bridge socket", "%s (%.1f ms)" % (cfgmod.user_sock(), (time.perf_counter() - t) * 1000))
    except OSError as e:
        chk(False, "bridge socket", "%s: %s (hooks fail open; nothing shown)" % (cfgmod.user_sock(), e))
    chk(os.path.exists(cfgmod.render_sock()), "render socket", cfgmod.render_sock(), warn=True)
    mods = _modules()
    stock = {"apple_ibridge", "apple_ib_tb"} <= mods
    bark = any(m.startswith("barkeep") for m in mods)
    chk(stock or bark, "Touch Bar driver", "stock apple-ib-tb loaded (firmware strip)" if stock else
        "barkeep loaded (host-drawn bar)" if bark else "neither stock nor barkeep modules loaded")
    chk(os.path.exists("/dev/dfr0"), "/dev/dfr0", "barkeep display node" if os.path.exists("/dev/dfr0")
        else "absent: the bar cannot draw (expected while the stock drivers are loaded)", warn=True)
    chk(os.access("/dev/uinput", os.W_OK), "/dev/uinput writable", "needed by ctb-bar for function keys (root)", warn=True)
    cfg = cfgmod.load()
    chk(bool(cfg["bar"]["touch_device"]), "touch device configured", cfg["bar"]["touch_device"] or
        "bar.touch_device unset: run `ctb probe touch`", warn=True)
    for level, name, detail in rows:
        print("%s %-26s %s" % (level, name, detail))
    return 1 if any(r[0] == "FAIL" for r in rows) else 0
