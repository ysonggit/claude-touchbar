"""ctb-status: statusLine command. Tees the JSON to the bridge, then prints the terminal status line."""

import json
import os
import socket
import subprocess
import sys


def _send(raw):
    try:
        data = json.loads(raw)
        d = os.environ.get("CTB_RUNTIME_DIR") or os.environ.get("XDG_RUNTIME_DIR") or "/tmp/ctb-%d" % os.getuid()
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.05)
        s.connect(os.path.join(d, "ctb.sock"))
        s.sendall((json.dumps({"type": "status", "data": data}) + "\n").encode())
        s.close()
        return data
    except Exception:
        return None


def minimal_line(d):
    from . import model as M
    m = M.extract_metrics(d or {})
    parts = []
    if "model" in m:
        parts.append("[%s]" % m["model"])
    if "ctx" in m:
        parts.append("ctx %d%%" % round(m["ctx"]))
    if "five_h" in m:
        parts.append("5h %d%%" % round(m["five_h"]))
    if "cost" in m:
        parts.append("$%.2f" % m["cost"])
    return " ".join(parts)


def main():
    raw = sys.stdin.read()
    data = _send(raw)
    passthrough = os.environ.get("CTB_PASSTHROUGH")
    if passthrough is None:
        from . import config
        passthrough = config.load()["statusline"]["passthrough"]
    if passthrough:
        try:
            out = subprocess.run(passthrough, shell=True, input=raw, capture_output=True, text=True, timeout=5)
            sys.stdout.write(out.stdout)
            return 0
        except Exception:
            pass
    print(minimal_line(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
