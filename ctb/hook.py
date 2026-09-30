"""ctb-hook: forward a Claude Code hook event to the bridge. Always fails open.

This runs on every tool call, so startup cost matters (SPEC R-60): no `json`/`socket` imports on the
common non-blocking path. The raw event bytes are forwarded untouched inside a small envelope and
the bridge does the parsing; only the rare permission reply is parsed here.
"""

import os
import sys
from _socket import AF_UNIX, SOCK_STREAM, socket

CONNECT_TIMEOUT = 0.05


def _sock_path():
    d = os.environ.get("CTB_RUNTIME_DIR") or os.environ.get("XDG_RUNTIME_DIR") or "/tmp/ctb-%d" % os.getuid()
    return d + "/ctb.sock"


def _event_name(raw):
    i = raw.find(b'"hook_event_name"')
    if i < 0:
        return None
    rest = raw[i + 17:].lstrip()[1:].lstrip()  # skip ':' and whitespace
    return rest[1:rest.find(b'"', 1)] if rest[:1] == b'"' else None


def main():
    raw = sys.stdin.buffer.read().strip()
    if not raw:
        return 0
    blocking = _event_name(raw) == b"PermissionRequest"
    try:
        s = socket(AF_UNIX, SOCK_STREAM)
        s.settimeout(CONNECT_TIMEOUT)
        s.connect(_sock_path())
        s.settimeout(None)
        s.sendall(b'{"type":"%s","data":%s}\n' % (b"permission" if blocking else b"event", raw))
        if not blocking:
            s.close()
            return 0
        # the bridge answers (or gives up) within prompt_timeout_s, which is below the hook timeout
        s.settimeout(float(os.environ.get("CTB_HOOK_WAIT", "118")))
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(4096)
            if not chunk:
                return 0
            buf += chunk
        import json
        reply = json.loads(buf)
        if reply.get("decision"):
            sys.stdout.write(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PermissionRequest", "decision": reply["decision"]}}))
            sys.stdout.flush()
    except Exception:
        pass  # fail open: no output → Claude Code shows its normal prompt
    return 0


if __name__ == "__main__":
    sys.exit(main())
