"""Account usage limits (5h session / weekly) from Anthropic's OAuth usage endpoint.

For Claude Code clients that run hooks but not the statusLine command (the desktop app's Code tab),
which is the only other place these numbers come from. Same approach as claude-pulse's fallback:
the Claude Code login token is read from ~/.claude/.credentials.json on every poll and sent only to
api.anthropic.com; when it has expired it is refreshed at platform.claude.com, as Claude Code does,
and written back to the same file. Tokens are never logged.
"""

import json
import os
import time
import urllib.error
import urllib.request

URL = "https://api.anthropic.com/api/oauth/usage"
CREDENTIALS = os.path.expanduser("~/.claude/.credentials.json")
# Claude Code's own OAuth client and token endpoint (taken from its binary, v2.1.286)
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"


def _load():
    try:
        with open(CREDENTIALS) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _oauth():
    return _load().get("claudeAiOauth") or {}


def refresh(oauth, timeout=30):
    """Refresh an expired access token the way Claude Code does, and write it back.

    The desktop app keeps its own login and never refreshes ~/.claude/.credentials.json, so with
    only the desktop app in use this token goes stale ~8 h after the CLI last ran. Writes back
    only if the file still holds the refresh token we used (so a refresh the CLI did meanwhile
    always wins), atomically and 0600. Returns the new oauth dict, or None. Never logs tokens.
    """
    rt = oauth.get("refreshToken")
    if not rt:
        return None
    body = {"grant_type": "refresh_token", "refresh_token": rt, "client_id": CLIENT_ID,
            "scope": " ".join(oauth.get("scopes") or [])}
    req = urllib.request.Request(TOKEN_URL, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "ctb"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.load(r)
    except (OSError, ValueError):
        return None
    if not d.get("access_token") or not d.get("expires_in"):
        return None
    now_ms = int(time.time() * 1000)
    new = dict(oauth, accessToken=d["access_token"], refreshToken=d.get("refresh_token") or rt,
               expiresAt=now_ms + int(d["expires_in"]) * 1000)
    if d.get("refresh_token_expires_in"):
        new["refreshTokenExpiresAt"] = now_ms + int(d["refresh_token_expires_in"]) * 1000
    if d.get("scope"):
        new["scopes"] = d["scope"].split()
    creds = _load()                                    # re-read: did the CLI refresh meanwhile?
    if (creds.get("claudeAiOauth") or {}).get("refreshToken") != rt:
        return creds.get("claudeAiOauth")
    creds["claudeAiOauth"] = new
    backup = CREDENTIALS + ".ctb-bak"                  # one-time copy of the original file
    if not os.path.exists(backup):
        with open(CREDENTIALS, "rb") as src:
            data = src.read()
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    tmp = CREDENTIALS + ".ctb-tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(creds, f)
    os.replace(tmp, CREDENTIALS)
    return new


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
    if oauth.get("expiresAt") and oauth["expiresAt"] / 1000 < time.time() + 300:
        oauth = refresh(oauth) or {}
        tok = oauth.get("accessToken")
        if not tok or oauth.get("expiresAt", 0) / 1000 < time.time():
            return {}, "login token expired and could not be refreshed (run `claude auth login`)"
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
