"""Codex support: follow ~/.codex/sessions rollout logs and feed them to the bridge.

Codex has no hook interface we rely on, but every session appends its events to
sessions/YYYY/MM/DD/rollout-*.jsonl. This module tails the recently-modified ones and turns them
into the same events a Claude Code hook would send (tagged `ctb_agent: "codex"`), plus the metrics
the bar shows: context use, the 5h/weekly rate limits, plan and model. Display only: Codex
approvals happen in Codex's own UI, so there is nothing to answer from the bar.
"""

import asyncio
import datetime
import glob
import json
import os
import sys
import time

from . import model as M

ROOT = os.path.expanduser("~/.codex/sessions")
BACKFILL = 262144           # bytes of a newly seen log to replay (enough to recover the current state)
ACTIVE_S = 600              # a log untouched this long is a finished/closed session


def tool_event(p):
    """response_item payload → (tool_name, tool_input) for a tool call, else None."""
    t = p.get("type")
    name = p.get("name") or ""
    if t == "function_call":
        try:
            args = json.loads(p.get("arguments") or "{}")
        except ValueError:
            args = {}
        cmd = args.get("command") or args.get("cmd")
        if isinstance(cmd, list):
            cmd = " ".join(str(c) for c in cmd)
        if cmd:
            return "Bash", {"command": cmd}
        return name or "tool", {}
    if t == "custom_tool_call":
        return name or "tool", {}
    return None


def rate_metrics(rl):
    """token_count.rate_limits → the renderer's five_h/seven_d/plan keys."""
    m = {}
    for key, name in (("primary", "five_h"), ("secondary", "seven_d")):
        w = (rl or {}).get(key) or {}
        if isinstance(w.get("used_percent"), (int, float)):
            m[name] = float(w["used_percent"])
            if w.get("resets_at"):
                m[name + "_reset"] = w["resets_at"]
    plan = (rl or {}).get("plan_type")
    if plan:
        m["plan"] = str(plan).capitalize()
    return m


class Log:
    def __init__(self, path, size):
        self.path, self.offset, self.buf = path, size, b""
        self.sid = self.cwd = self.model = None
        self.tool_ids = {}                 # call_id → tool name, for matching outputs
        self.skip_partial = False          # backfill started mid-line
        self.window = 0                    # model_context_window from task_started


class Tailer:
    def __init__(self, bridge, root=ROOT):
        self.bridge, self.root = bridge, root
        self.logs = {}

    # ---------------------------------------------------------------- discovery

    def _candidates(self, now):
        days = {datetime.date.fromtimestamp(now - d * 86400) for d in (0, 1)}
        out = []
        for d in days:
            out += glob.glob(os.path.join(self.root, d.strftime("%Y/%m/%d"), "rollout-*.jsonl"))
        return out

    def poll(self, now):
        for path in self._candidates(now):
            try:
                st = os.stat(path)
            except OSError:
                continue
            if path not in self.logs:
                if now - st.st_mtime > ACTIVE_S:
                    continue
                self._open(path, st.st_size)
            elif st.st_size > self.logs[path].offset:
                self._read(self.logs[path], False)
        for path, lg in list(self.logs.items()):
            try:
                idle = now - os.stat(path).st_mtime > ACTIVE_S
            except OSError:
                idle = True
            if idle:
                self._close(lg)
                del self.logs[path]

    def _open(self, path, size):
        lg = Log(path, 0)
        try:
            with open(path, "rb") as f:
                first = f.readline()
            meta = json.loads(first).get("payload", {})
            lg.sid = "codex:" + (meta.get("session_id") or meta.get("id") or os.path.basename(path))
            lg.cwd = meta.get("cwd")
        except (OSError, ValueError):
            return
        lg.offset = max(len(first), size - BACKFILL)
        if lg.offset > len(first):
            lg.skip_partial = True
        self.logs[path] = lg
        self._read(lg, True)

    def _close(self, lg):
        if lg.sid:
            self.bridge.handle_event({"hook_event_name": "SessionEnd", "session_id": lg.sid})

    # ---------------------------------------------------------------- reading

    def _read(self, lg, backfill):
        try:
            with open(lg.path, "rb") as f:
                f.seek(lg.offset)
                data = f.read()
        except OSError:
            return
        lg.offset += len(data)
        buf = lg.buf + data
        if lg.skip_partial:          # backfill landed mid-line
            lg.skip_partial = False
            buf = buf.partition(b"\n")[2]
        *lines, lg.buf = buf.split(b"\n")
        for line in lines:
            try:
                self._line(lg, json.loads(line))
            except ValueError:
                continue
        if backfill:
            s = self.bridge.sessions.get(lg.sid)
            if s and s.state == M.DONE:
                s.state_since -= 3600                   # an old finish must not flash "Done"
                self.bridge.refresh()

    def _event(self, lg, name, **kw):
        ev = {"hook_event_name": name, "session_id": lg.sid, "cwd": lg.cwd, "ctb_agent": "codex"}
        ev.update(kw)
        self.bridge.handle_event(ev)

    def _metrics(self, lg, m):
        s = self.bridge.sessions.get(lg.sid)
        if s is None:
            self._event(lg, "SessionStart")
            s = self.bridge.sessions.get(lg.sid)
        s.metrics = dict(s.metrics, **m)
        s.metrics_at = time.time()
        limits = {k: v for k, v in m.items() if k in ("five_h", "seven_d", "five_h_reset",
                                                      "seven_d_reset", "plan")}
        if limits:
            self.bridge.codex_usage.update(limits)      # account-wide: other sessions share it
        self.bridge.refresh()

    def _line(self, lg, d):
        t, p = d.get("type"), d.get("payload") or {}
        pt = p.get("type")
        if t == "turn_context":
            lg.cwd = p.get("cwd") or lg.cwd
            lg.model = p.get("model") or lg.model
        elif t == "event_msg":
            if pt == "task_started":
                self._event(lg, "UserPromptSubmit")
                if p.get("model_context_window"):
                    lg.window = p["model_context_window"]
            elif pt == "task_complete":
                self._event(lg, "Stop")
            elif pt in ("turn_aborted", "task_aborted"):
                self._event(lg, "Stop")
            elif pt == "thread_settings_applied":
                lg.model = (p.get("thread_settings") or {}).get("model") or lg.model
            elif pt == "token_count":
                info = p.get("info") or {}
                m = rate_metrics(p.get("rate_limits"))
                win = info.get("model_context_window") or lg.window
                used = (info.get("last_token_usage") or {}).get("total_tokens")
                if win and used is not None:
                    m["ctx"] = round(100.0 * used / win, 1)
                if lg.model:
                    m["model"] = lg.model
                self._metrics(lg, m)
        elif t == "response_item":
            te = tool_event(p)
            if te:
                lg.tool_ids[p.get("call_id")] = te[0]
                self._event(lg, "PreToolUse", tool_name=te[0], tool_input=te[1],
                            tool_use_id=p.get("call_id"))
            elif pt in ("function_call_output", "custom_tool_call_output") and p.get("call_id") in lg.tool_ids:
                self._event(lg, "PostToolUse", tool_name=lg.tool_ids.pop(p["call_id"]),
                            tool_use_id=p.get("call_id"))


async def run(bridge, interval=0.5):
    """Bridge task: poll forever. Never raises."""
    tailer = Tailer(bridge)
    while True:
        try:
            tailer.poll(time.time())
        except Exception as e:                           # a bad log must not take the bridge down
            print("ctb bridge: codex tail failed: %r" % e, file=sys.stderr)
        await asyncio.sleep(interval)
