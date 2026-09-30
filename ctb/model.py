"""Session model: tool summaries, danger detection, metrics extraction, state names."""

import json
import os
import re
import subprocess
import time

# session states
IDLE, THINKING, TOOL, INPUT, TERMINAL, DONE, ERROR = (
    "idle", "thinking", "tool", "input", "terminal", "done", "error")

_CURL_PIPE = re.compile(r"(curl|wget)[^|]*\|\s*(sudo\s+)?(ba|z)?sh\b")


def truncate(s, n):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: max(0, n - 1)] + "…"


def tool_summary(name, tool_input, limit=60):
    ti = tool_input or {}
    if name == "Bash":
        cmd = (ti.get("command") or "").strip().splitlines()
        s = cmd[0] if cmd else ""
    elif name in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit"):
        s = os.path.basename(ti.get("file_path") or ti.get("notebook_path") or "")
    elif name in ("Grep", "Glob"):
        s = ti.get("pattern") or ""
    elif name == "WebFetch":
        m = re.match(r"https?://([^/]+)", ti.get("url") or "")
        s = m.group(1) if m else ti.get("url") or ""
    elif name == "WebSearch":
        s = ti.get("query") or ""
    elif name in ("Task", "Agent"):
        s = ti.get("subagent_type") or ti.get("description") or ""
    elif name.startswith("mcp__"):
        parts = name.split("__")
        return truncate("%s:%s" % (parts[1], "__".join(parts[2:])), limit)
    else:
        return name
    return truncate("%s: %s" % (name, s) if s else name, limit)


def is_dangerous(name, tool_input, cwd, patterns):
    ti = tool_input or {}
    if name == "Bash":
        cmd = (ti.get("command") or "")
        low = cmd.lower()
        if any(p.lower() in low for p in patterns) or _CURL_PIPE.search(low):
            return True
        return False
    if name in ("Write", "Edit", "MultiEdit", "NotebookEdit") and cwd:
        p = ti.get("file_path") or ti.get("notebook_path") or ""
        if p:
            p = os.path.realpath(p)
            c = os.path.realpath(cwd)
            return not (p == c or p.startswith(c + os.sep))
    return False


def always_rule(name, tool_input):
    """Permission rule for the 'Always' button, or None when it should be hidden (Bash only)."""
    if name != "Bash":
        return None
    cmd = ((tool_input or {}).get("command") or "").strip()
    if not cmd or any(c in cmd for c in "|;&<>$`\n"):
        return None
    toks = cmd.split()
    head = toks[:2] if len(toks) > 1 and not toks[1].startswith("-") else toks[:1]
    return "Bash(%s *)" % " ".join(head)


def _dig(d, *path):
    for p in path:
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return d


_branch_cache = {}


def git_branch(cwd):
    if not cwd:
        return None
    now = time.monotonic()
    hit = _branch_cache.get(cwd)
    if hit and now - hit[0] < 5:
        return hit[1]
    try:
        out = subprocess.run(["git", "-C", cwd, "branch", "--show-current"],
                             capture_output=True, text=True, timeout=1).stdout.strip() or None
    except Exception:
        out = None
    _branch_cache[cwd] = (now, out)
    return out


def extract_metrics(d):
    """statusLine JSON → metrics dict. Missing fields are simply absent (never 0)."""
    m = {}
    pairs = {
        "ctx": ("context_window", "used_percentage"),
        "five_h": ("rate_limits", "five_hour", "used_percentage"),
        "seven_d": ("rate_limits", "seven_day", "used_percentage"),
        "five_h_reset": ("rate_limits", "five_hour", "resets_at"),
        "seven_d_reset": ("rate_limits", "seven_day", "resets_at"),
        "cost": ("cost", "total_cost_usd"),
        "model": ("model", "display_name"),
        "effort": ("effort", "level"),
    }
    for key, path in pairs.items():
        v = _dig(d, *path)
        if v is not None:
            m[key] = v
    cwd = _dig(d, "workspace", "current_dir") or d.get("cwd")
    if cwd:
        m["cwd"] = cwd
    return m


def model_name(model_id):
    """claude-opus-5-5 → "Opus 5.5", claude-haiku-4-5-20251001 → "Haiku 4.5"."""
    parts = [p for p in (model_id or "").split("-") if p and p != "claude" and not (p.isdigit() and len(p) == 8)]
    if not parts:
        return None
    nums = [p for p in parts[1:] if p.isdigit()]
    return parts[0].capitalize() + (" " + ".".join(nums) if nums else "")


def transcript_metrics(path, window=0, tail=262144):
    """Context use + model from the newest assistant usage record in a Claude Code transcript.

    For clients that run hooks but not the statusLine command (the desktop app's Code tab).
    window 0 = auto: 200k for Haiku, else 1M (and never less than what the transcript shows used).
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - tail))
            lines = f.read().splitlines()
    except OSError:
        return {}
    for line in reversed(lines):
        if b'"usage"' not in line or b'"assistant"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        msg = d.get("message") if isinstance(d, dict) else None
        u = msg.get("usage") if isinstance(msg, dict) else None
        if d.get("type") != "assistant" or not isinstance(u, dict):
            continue
        used = sum(int(u.get(k) or 0) for k in
                   ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        mid = msg.get("model") or ""
        win = window or (200_000 if "haiku" in mid else 1_000_000)
        if used > win:
            win = 1_000_000
        m = {"ctx": round(100.0 * used / win, 1)}
        if model_name(mid):
            m["model"] = model_name(mid)
        return m
    return {}


class Session:
    def __init__(self, sid, cwd=None):
        self.sid = sid
        self.cwd = cwd
        self.label = os.path.basename((cwd or "").rstrip("/")) or sid[:6]
        self.state = IDLE
        self.activity = ""
        self.turn_start = None
        self.subagents = 0
        self.compacting = False
        self.metrics = {}
        self.metrics_at = None
        self.last_event = time.time()
        self.state_since = time.time()
        self.flash_error_until = 0.0

    def set_state(self, st, activity=None):
        if st != self.state:
            self.state_since = time.time()
        self.state = st
        if activity is not None:
            self.activity = activity
