"""ctb-bridge: the single source of truth for sessions, prompts and what the bar shows.

Listens on two Unix sockets:
  * ctb.sock          (hooks/status → bridge; permission requests block until decided)
  * ctb-render.sock   (bar ↔ bridge; layout frames out, taps in)
"""

import asyncio
import itertools
import json
import os
import socket
import struct
import sys
import time

from . import config as cfgmod
from . import model as M

STATUS, PERMISSION, PLAN, QUESTION, TERMINAL, NONE = (
    "status", "permission", "plan", "question", "terminal", "none")
HOLD_BUTTONS = ("allow", "always", "approve")
GUARD_S = 0.3  # taps this soon after a layout change are ignored (R-42)


class Prompt:
    def __init__(self, pid, sid, kind, tool, tool_use_id, tool_input, summary, dangerous, deadline):
        self.id, self.sid, self.kind, self.tool = pid, sid, kind, tool
        self.tool_use_id, self.tool_input = tool_use_id, tool_input
        self.summary, self.dangerous, self.deadline = summary, dangerous, deadline
        self.fut = None          # asyncio.Future for blocking prompts
        self.timer = None
        self.notif = None
        self.created = time.time()
        self.prev_activity = ""

    @property
    def blocking(self):
        return self.kind in (PERMISSION, PLAN)


def peer_uid(writer):
    sock = writer.get_extra_info("socket")
    try:
        _pid, uid, _gid = struct.unpack("3i", sock.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
        return uid
    except Exception:
        return None


class Bridge:
    def __init__(self, cfg=None, notifier=None):
        self.cfg = cfg or cfgmod.load()
        self.sessions = {}
        self.prompts = []
        self.bars = set()
        self.notifier = notifier
        self.layout_seq = 0
        self.layout_created = 0.0
        self._sig = None
        self._isig = None
        self.layout = {}
        self.manual = None  # (sid, until)
        self.usage = {}     # 5h/weekly limits + plan from ctb.usage (hooks-only clients)
        self._ids = itertools.count(1)
        self._servers = []
        self.refresh()

    # ---------------------------------------------------------------- sessions

    def _session(self, data):
        sid = data.get("session_id") or "unknown"
        s = self.sessions.get(sid)
        if s is None:
            s = self.sessions[sid] = M.Session(sid, data.get("cwd"))
        elif data.get("cwd") and not s.cwd:
            s.cwd = data["cwd"]
        s.last_event = time.time()
        return s

    def _effective(self, s, now):
        if s.state == M.DONE and now - s.state_since >= self.cfg["display"]["done_linger_s"]:
            return M.IDLE
        return s.state

    def _wake_at(self, ts):
        try:
            asyncio.get_running_loop().call_later(max(0.0, ts - time.time()) + 0.05, self.refresh)
        except RuntimeError:
            pass

    # ---------------------------------------------------------------- hook events

    def handle_event(self, data):
        ev = data.get("hook_event_name")
        if ev == "SessionEnd":
            sid = data.get("session_id")
            self._drop_prompts(sid, None)
            self.sessions.pop(sid, None)
            self.refresh()
            return
        s = self._session(data)
        now = time.time()
        self._transcript_metrics(s, data, now)
        if ev == "SessionStart":
            s.set_state(M.IDLE, "")
        elif ev == "UserPromptSubmit":
            self._drop_prompts(s.sid, None)
            s.turn_start = now
            s.set_state(M.THINKING, "Thinking")
        elif ev == "PreToolUse":
            if data.get("tool_name") == "AskUserQuestion":
                self._add_question(s, data)
            else:
                s.set_state(M.TOOL, M.tool_summary(data.get("tool_name", ""), data.get("tool_input")))
        elif ev in ("PostToolUse", "PostToolUseFailure"):
            self._drop_prompts(s.sid, data.get("tool_use_id"))
            s.set_state(M.THINKING, "Thinking")
            if ev == "PostToolUseFailure":
                s.flash_error_until = now + 1
                self._wake_at(s.flash_error_until)
        elif ev == "SubagentStart":
            s.subagents += 1
        elif ev == "SubagentStop":
            s.subagents = max(0, s.subagents - 1)
        elif ev == "PreCompact":
            s.compacting = True
        elif ev == "PostCompact":
            s.compacting = False
        elif ev == "Notification":
            nt = data.get("notification_type")
            has = any(p.sid == s.sid and p.blocking for p in self.prompts)
            if nt in ("permission_prompt", "agent_needs_input") and not has:
                s.set_state(M.TERMINAL, "Waiting in terminal")
            elif nt == "idle_prompt":
                s.set_state(M.IDLE, "")
        elif ev == "Stop":
            self._drop_prompts(s.sid, None)
            s.set_state(M.DONE, "Done")
            s.turn_start = None
            self._wake_at(now + self.cfg["display"]["done_linger_s"])
        elif ev == "StopFailure":
            self._drop_prompts(s.sid, None)
            s.set_state(M.ERROR, "Error")
        self.refresh()

    def _transcript_metrics(self, s, data, now):
        """Hooks-only clients (no statusLine): read context/model from the transcript instead.
        Status-line metrics win while fresh; transcript values only fill in what is missing."""
        path = data.get("transcript_path")
        if not path or now - getattr(s, "transcript_at", 0) < 2:
            return
        if s.metrics_at is not None and now - s.metrics_at < 60:
            return
        s.transcript_at = now
        tm = M.transcript_metrics(path, self.cfg["display"].get("context_window", 0))
        if tm:
            s.metrics = dict(s.metrics, **tm)

    def handle_status(self, data):
        sid = data.get("session_id")
        s = self.sessions.get(sid) if sid else None
        if s is None and not sid:
            cwd = (data.get("workspace") or {}).get("current_dir") or data.get("cwd")
            s = next((x for x in self.sessions.values() if x.cwd == cwd), None)
        if s is None:
            s = self._session(dict(data, cwd=(data.get("workspace") or {}).get("current_dir") or data.get("cwd")))
        s.metrics = M.extract_metrics(data)
        s.metrics_at = time.time()
        if s.metrics.get("cwd") and not s.cwd:
            s.cwd = s.metrics["cwd"]
        self.refresh()

    # ---------------------------------------------------------------- prompts

    def _new_prompt(self, s, kind, data, summary, dangerous):
        timeout = self.cfg["prompts"]["prompt_timeout_s"]
        p = Prompt("p%d" % next(self._ids), s.sid, kind, data.get("tool_name", ""),
                   data.get("tool_use_id"), data.get("tool_input") or {}, summary, dangerous,
                   time.time() + timeout)
        p.prev_activity = s.activity
        self.prompts.append(p)
        s.set_state(M.INPUT, "Needs input")
        return p

    def add_permission(self, data):
        s = self._session(data)
        ti = data.get("tool_input") or {}
        if data.get("tool_name") == "ExitPlanMode":
            plan = ti.get("plan") or ""
            title = next((ln.lstrip("# ").strip() for ln in plan.splitlines() if ln.startswith("#")), "") \
                or M.truncate(plan, 80) or "Plan"
            p = self._new_prompt(s, PLAN, data, title, False)
        else:
            dang = M.is_dangerous(data.get("tool_name", ""), ti, s.cwd or data.get("cwd"),
                                  self.cfg["prompts"]["dangerous"])
            p = self._new_prompt(s, PERMISSION, data, M.tool_summary(data.get("tool_name", ""), ti, 160), dang)
        p.fut = asyncio.get_running_loop().create_future()
        p.timer = asyncio.get_running_loop().call_later(
            self.cfg["prompts"]["prompt_timeout_s"], self._expire, p)
        self._maybe_notify(p)
        self.refresh()
        return p

    def _add_question(self, s, data):
        qs = (data.get("tool_input") or {}).get("questions") or []
        q = qs[0] if qs else {}
        p = self._new_prompt(s, QUESTION, data, M.truncate(q.get("question", "Question"), 120), False)
        p.tool_input = {"options": [o.get("label", "") for o in q.get("options", [])][:4],
                        "pager": "1/%d" % len(qs) if len(qs) > 1 else ""}

    def _drop_prompts(self, sid, tool_use_id):
        """Prompt resolved elsewhere (terminal won the race, or the turn moved on)."""
        for p in list(self.prompts):
            if p.sid != sid:
                continue
            if tool_use_id and p.tool_use_id and p.tool_use_id != tool_use_id:
                continue
            self._finish(p, None)

    def _finish(self, p, reply):
        if p in self.prompts:
            self.prompts.remove(p)
        if p.timer:
            p.timer.cancel()
        if self.notifier and p.notif:
            self.notifier.stop(p)
            p.notif = None
        if p.fut and not p.fut.done():
            p.fut.set_result(reply)

    def _expire(self, p):
        if p in self.prompts:
            self._finish(p, None)
            s = self.sessions.get(p.sid)
            if s:
                s.set_state(M.TERMINAL, "Waiting in terminal")
            self.refresh()

    def abandon(self, p):
        """The hook process died (terminal answered first)."""
        if p in self.prompts:
            self._finish(p, None)
            self.refresh()

    def resolve(self, p, button):
        """Apply a validated user decision. Returns True if applied."""
        s = self.sessions.get(p.sid)
        if p.kind == QUESTION:
            if button == "terminal":
                self._finish(p, None)
                if s:
                    s.set_state(M.THINKING, "Thinking")
                self.refresh()
                return True
            return False
        if button in ("allow", "approve"):
            reply = {"decision": {"behavior": "allow"}}
        elif button == "always":
            rule = M.always_rule(p.tool, p.tool_input)
            if not rule or p.dangerous or not self.cfg["prompts"]["always_button"]:
                return False
            reply = {"decision": {"behavior": "allow", "updatedPermissions": {
                "rule": rule, "scope": self.cfg["prompts"]["always_scope"]}}}
        elif button == "deny":
            reply = {"decision": {"behavior": "deny", "message": "Denied from Touch Bar"}}
        elif button == "reject":
            reply = {"decision": {"behavior": "deny",
                                  "message": "Plan rejected from Touch Bar — please revise."}}
        else:
            return False
        self._finish(p, reply)
        if s:
            ok = reply["decision"]["behavior"] == "allow"
            s.set_state(M.TOOL if ok else M.THINKING, p.prev_activity if ok else "Thinking")
        self.refresh()
        return True

    # ---------------------------------------------------------------- notifications (R-36)

    def _maybe_notify(self, p):
        """Alert only: a notification never carries a decision (see ctb/notify.py)."""
        if self.notifier and not self.bars and self.cfg["fallback"]["notifications"] and p.blocking:
            s = self.sessions.get(p.sid)
            p.notif = self.notifier.start(p, s.label if s else "?")

    # ---------------------------------------------------------------- layout

    def _pick(self, now):
        if self.manual and self.manual[1] > now and self.manual[0] in self.sessions:
            return self.sessions[self.manual[0]]
        if not self.sessions:
            return None

        def rank(s):
            st = self._effective(s, now)
            if st in (M.INPUT, M.TERMINAL):
                return (0, s.state_since)
            if st == M.ERROR:
                return (1, -s.last_event)
            if st in (M.THINKING, M.TOOL):
                return (2, -s.last_event)
            if st == M.DONE:
                return (3, -s.last_event)
            return (4, -s.last_event)
        return min(self.sessions.values(), key=rank)

    def cycle(self, direction=1):
        now = time.time()
        if not self.sessions:
            return
        order = sorted(self.sessions)
        cur = self._pick(now)
        i = order.index(cur.sid) if cur else -1
        self.manual = (order[(i + direction) % len(order)], now + self.cfg["display"]["manual_hold_s"])
        self._wake_at(self.manual[1])

    def compute_layout(self, now=None):
        now = now or time.time()
        head = self.prompts[0] if self.prompts else None
        s = self.sessions.get(head.sid) if head else self._pick(now)
        if s is None:
            return {"type": "layout", "mode": NONE, "sessions": 0, "buttons": [], "attention": False}
        st = self._effective(s, now)
        name = {M.IDLE: "idle", M.THINKING: "working", M.TOOL: "working", M.INPUT: "input",
                M.TERMINAL: "input", M.DONE: "done", M.ERROR: "error"}[st]
        if s.flash_error_until > now:
            name = "error"
        waiting = sum(1 for x in self.sessions.values()
                      if x is not s and self._effective(x, now) in (M.INPUT, M.TERMINAL))
        lay = {
            "type": "layout", "mode": STATUS, "sessions": len(self.sessions),
            "chip": {"label": s.label, "waiting": waiting},
            "state": name, "activity": s.activity if st != M.IDLE else "",
            "timer_start": s.turn_start if st in (M.THINKING, M.TOOL, M.INPUT) else None,
            "subagents": s.subagents, "compacting": s.compacting,
            # account-wide limits from the usage poller fill in what the status line didn't send
            "metrics": dict(self.usage, **s.metrics), "metrics_at": s.metrics_at,
            "branch": M.git_branch(s.cwd), "buttons": [{"id": "chip", "hold_ms": 0}],
            "attention": False,
            "thresholds": self.cfg["thresholds"],
        }
        pc = self.cfg["prompts"]
        if head:
            lay["attention"] = True
            lay["mode"] = head.kind
            hold = pc["dangerous_hold_ms"] if head.dangerous else pc["hold_ms"]
            lay["prompt"] = {"id": head.id, "tool": head.tool, "summary": head.summary,
                             "dangerous": head.dangerous, "index": 1, "total": len(self.prompts)}
            if head.kind == PERMISSION:
                b = [{"id": "deny", "label": "Deny", "style": "danger", "hold_ms": 0},
                     {"id": "allow", "label": "Allow", "style": "primary", "hold_ms": hold}]
                if pc["always_button"] and not head.dangerous and M.always_rule(head.tool, head.tool_input):
                    b.append({"id": "always", "label": "Always", "style": "plain", "hold_ms": hold})
            elif head.kind == PLAN:
                b = [{"id": "reject", "label": "Reject", "style": "danger", "hold_ms": 0},
                     {"id": "approve", "label": "Approve", "style": "primary", "hold_ms": hold}]
            else:
                lay["prompt"].update(options=head.tool_input.get("options", []),
                                     pager=head.tool_input.get("pager", ""))
                b = [{"id": "terminal", "label": "Terminal", "style": "plain", "hold_ms": 0}]
            lay["buttons"] = b + [{"id": "chip", "hold_ms": 0}]
        elif st == M.TERMINAL:
            lay["mode"] = TERMINAL
        return lay

    def refresh(self):
        lay = self.compute_layout()
        sig = json.dumps(lay, sort_keys=True, default=str)
        if sig == self._sig:
            return
        self._sig = sig
        # seq / guard track only the *interactive* part (mode, prompt, buttons), so that frequent
        # display-only updates (status line metrics) can't invalidate a tap or hold in progress.
        isig = json.dumps([lay["mode"], lay.get("prompt", {}).get("id"), lay["buttons"]], sort_keys=True)
        if isig != self._isig:
            self._isig = isig
            self.layout_seq += 1
            self.layout_created = time.time()
        lay["seq"] = self.layout_seq
        self.layout = lay
        data = (json.dumps(lay) + "\n").encode()
        for w in list(self.bars):
            try:
                w.write(data)
            except Exception:
                self.bars.discard(w)

    # ---------------------------------------------------------------- taps

    def handle_tap(self, msg, now=None):
        """Validate and apply a tap from the bar. Returns a reason string (for tests/debug)."""
        now = now or time.time()
        if msg.get("seq") != self.layout_seq:
            return "stale"
        bid = msg.get("button")
        spec = next((b for b in self.layout.get("buttons", []) if b["id"] == bid), None)
        if spec is None:
            return "unknown"
        if bid == "chip":
            self.cycle()
            self.refresh()
            return "ok"
        if now - self.layout_created < GUARD_S:
            return "guard"
        if int(msg.get("held_ms", 0)) < spec["hold_ms"]:
            return "hold"
        p = self.prompts[0] if self.prompts else None
        if p is None or p.id != self.layout.get("prompt", {}).get("id"):
            return "stale"
        return "ok" if self.resolve(p, bid) else "refused"

    # ---------------------------------------------------------------- networking

    async def _on_user(self, reader, writer):
        try:
            if peer_uid(writer) not in (os.getuid(), 0):
                return
            line = await asyncio.wait_for(reader.readline(), 2)
            msg = json.loads(line)
            t = msg.get("type")
            if t == "event":
                self.handle_event(msg["data"])
            elif t == "status":
                self.handle_status(msg["data"])
            elif t == "permission":
                p = self.add_permission(msg["data"])
                eof = asyncio.ensure_future(reader.read())
                done, _ = await asyncio.wait({p.fut, eof}, return_when=asyncio.FIRST_COMPLETED)
                if p.fut in done:
                    reply = p.fut.result() or {"none": True}
                    writer.write((json.dumps(reply) + "\n").encode())
                    await writer.drain()
                else:
                    self.abandon(p)
                eof.cancel()
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _on_bar(self, reader, writer):
        try:
            if peer_uid(writer) not in (os.getuid(), 0):
                return
            self.bars.add(writer)
            while True:
                line = await reader.readline()
                if not line:
                    break
                msg = json.loads(line)
                if msg.get("type") == "hello":
                    writer.write((json.dumps(self.layout) + "\n").encode())
                elif msg.get("type") == "tap":
                    self.handle_tap(msg)
        except Exception:
            pass
        finally:
            self.bars.discard(writer)
            try:
                writer.close()
            except Exception:
                pass
            if not self.bars:  # bar went away: fall back to notifications for pending prompts
                for p in self.prompts:
                    if not p.notif:
                        self._maybe_notify(p)

    async def start(self, user_path=None, render_path=None):
        for path, handler in ((user_path or cfgmod.user_sock(), self._on_user),
                              (render_path or cfgmod.render_sock(), self._on_bar)):
            if os.path.exists(path):
                os.unlink(path)
            srv = await asyncio.start_unix_server(handler, path=path)
            os.chmod(path, 0o600)
            self._servers.append(srv)

    async def usage_loop(self):
        """Poll account limits while any session exists; back off on errors (R-usage)."""
        from . import usage
        cfg = self.cfg.get("usage", {})
        if not cfg.get("enabled", True):
            return
        delay = cfg.get("poll_s", 60)
        while True:
            if not self.sessions:              # nothing to show yet: check again soon
                await asyncio.sleep(2)
                continue
            m, err = await asyncio.to_thread(usage.fetch)
            if m:
                self.usage, delay = m, cfg.get("poll_s", 60)
                self.refresh()
            elif err:
                delay = min(600, delay * 2)
                print("ctb bridge: usage poll failed: %s (retry in %ds)" % (err, delay), file=sys.stderr)
            await asyncio.sleep(delay)

    async def stop(self):
        for p in list(self.prompts):
            self._finish(p, None)
        for srv in self._servers:
            srv.close()


def main():
    from .notify import Notifier
    bridge = Bridge(notifier=Notifier())

    async def run():
        await bridge.start()
        await bridge.usage_loop()
        await asyncio.Event().wait()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
