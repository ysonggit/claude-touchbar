"""Account usage limits (5h session / weekly) from Anthropic's OAuth usage endpoint.

For Claude Code clients that run hooks but not the statusLine command (the desktop app's Code tab),
which is the only other place these numbers come from. Same approach as claude-pulse's fallback:
the Claude Code login token is read from ~/.claude/.credentials.json on every poll (Claude Code
refreshes it) and sent only to api.anthropic.com. It is never logged or stored.
"""

import json
import os
import time
import urllib.error
import urllib.request

URL = "https://api.anthropic.com/api/oauth/usage"
CREDENTIALS = os.path.expanduser("~/.claude/.credentials.json")


def _oauth():
    try:
        with open(CREDENTIALS) as f:
            return json.load(f).get("claudeAiOauth") or {}
    except (OSError, ValueError):
        return {}


def plan_name(oauth):
    """rateLimitTier "default_claude_max_20x" → "Max 20x"; else subscriptionType "pro" → "Pro"."""
    tier = (oauth.get("rateLimitTier") or "").lower()
    if "max_20x" in tier:
        return "Max 20x"
    if "max_5x" in tier:
        return "Max 5x"
    sub = oauth.get("subscriptionType") or ""
    return sub.capitalize() if sub else None


def parse(d):
    """Endpoint JSON → the metric keys the renderer already uses."""
    m = {}
    for key, name in (("five_hour", "five_h"), ("seven_day", "seven_d")):
        w = d.get(key) or {}
        if isinstance(w.get("utilization"), (int, float)):
            m[name] = float(w["utilization"])
            if w.get("resets_at"):
                m[name + "_reset"] = w["resets_at"]
    return m


def fetch(timeout=10):
    """Returns (metrics, error). Never raises."""
    oauth = _oauth()
    tok = oauth.get("accessToken")
    if not tok:
        return {}, "no Claude Code login"
    if oauth.get("expiresAt") and oauth["expiresAt"] / 1000 < time.time():
        return {}, "login token expired (Claude Code refreshes it on its next request)"
    req = urllib.request.Request(URL, headers={
        "Authorization": "Bearer " + tok, "anthropic-beta": "oauth-2025-04-20", "User-Agent": "ctb"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            m = parse(json.load(r))
    except urllib.error.HTTPError as e:
        return {}, "HTTP %d" % e.code
    except (OSError, ValueError) as e:
        return {}, type(e).__name__
    plan = plan_name(oauth)
    if plan:
        m["plan"] = plan
    return m, None
