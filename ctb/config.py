"""Configuration defaults, paths, and a small TOML-subset reader (Python 3.10 has no tomllib)."""

import copy
import os
import re

DEFAULTS = {
    "prompts": {
        "prompt_timeout_s": 110,
        "hold_ms": 400,
        "dangerous_hold_ms": 1000,
        "always_button": False,
        "always_scope": "session",
        "dangerous": [
            "rm -rf", "sudo ", "git push --force", "git push -f",
            "git reset --hard", "| sh", "| bash", "mkfs", "dd ",
            "chmod -R 777",
        ],
    },
    "display": {
        "segments": ["chip", "activity", "ctx", "5h", "7d", "cost", "model"],
        "done_linger_s": 3,
        "manual_hold_s": 10,
        "context_window": 0,          # tokens; 0 = auto (only used without statusLine data)
    },
    "thresholds": {"warn": 70, "crit": 90},
    "fallback": {"notifications": True},
    # 5h/weekly limits via Anthropic's OAuth usage endpoint, with the Claude Code login token
    "usage": {"enabled": True, "poll_s": 60},
    "statusline": {"passthrough": ""},
    "bar": {
        "backend": "dfr0",
        "dfr_device": "/dev/dfr0",
        "rotate": "cw",
        "default_layer": "claude",
        "control_strip": ["brightness", "volume", "mute", "playpause"],
        "dim_after_s": 60,
        "off_after_s": 300,
        "font": "Sans",
        "style": "pulse",
        "touch_device": "auto",      # "auto" = iBridge USB interface 2 (config 2), or a /dev/hidrawN path
        "touch_x_offset": 0,
        "touch_x_min": 0.5,
        "touch_x_max": 1.0,
        "width": 2170,
        "height": 60,
    },
}


def runtime_dir():
    d = os.environ.get("CTB_RUNTIME_DIR") or os.environ.get("XDG_RUNTIME_DIR")
    if not d:
        d = "/tmp/ctb-%d" % os.getuid()
        os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def user_sock():
    return os.path.join(runtime_dir(), "ctb.sock")


def render_sock():
    if os.environ.get("CTB_RENDER_SOCK"):
        return os.environ["CTB_RENDER_SOCK"]
    # `sudo ctb bar` by hand: the bridge runs as the invoking user, so use their runtime dir.
    if os.geteuid() == 0 and os.environ.get("SUDO_UID"):
        return "/run/user/%s/ctb-render.sock" % os.environ["SUDO_UID"]
    return os.path.join(runtime_dir(), "ctb-render.sock")


def config_path():
    return os.environ.get("CTB_CONFIG") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "ctb", "config.toml")


# ---- minimal TOML subset: [table], key = "str" | int | float | bool | [..] | {..} ----

def _split_top(s, sep=","):
    out, depth, cur, q = [], 0, "", None
    for ch in s:
        if q:
            cur += ch
            if ch == q:
                q = None
        elif ch in "\"'":
            q = ch
            cur += ch
        elif ch in "[{":
            depth += 1
            cur += ch
        elif ch in "]}":
            depth -= 1
            cur += ch
        elif ch == sep and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return out


def _value(s):
    s = s.strip()
    if s.startswith('"') and s.endswith('"'):
        return bytes(s[1:-1], "utf-8").decode("unicode_escape")
    if s.startswith("'") and s.endswith("'"):
        return s[1:-1]
    if s in ("true", "false"):
        return s == "true"
    if s.startswith("["):
        return [_value(x) for x in _split_top(s[1:-1])]
    if s.startswith("{"):
        d = {}
        for part in _split_top(s[1:-1]):
            k, _, v = part.partition("=")
            d[k.strip()] = _value(v)
        return d
    try:
        return int(s)
    except ValueError:
        return float(s)


def parse_toml(text):
    root, cur = {}, None
    cur = root
    buf = ""
    for raw in text.splitlines():
        line = re.sub(r"(^|\s)#.*$", "", raw) if not re.search(r"[\"'].*#.*[\"']", raw) else raw
        line = line.strip()
        if not line:
            continue
        buf = (buf + " " + line) if buf else line
        if buf.count("[") != buf.count("]") and not buf.startswith("["):
            continue  # multi-line array in progress
        if buf.startswith("[") and "=" not in buf:
            cur = root
            for part in buf.strip("[] ").split("."):
                cur = cur.setdefault(part.strip(), {})
        else:
            k, _, v = buf.partition("=")
            cur[k.strip()] = _value(v)
        buf = ""
    return root


def _merge(base, over):
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load(path=None):
    cfg = copy.deepcopy(DEFAULTS)
    path = path or config_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            _merge(cfg, parse_toml(f.read()))
    except FileNotFoundError:
        pass
    return cfg
