import json
import os
import tempfile
import time
import unittest

from helpers import Env
from ctb import codex


def line(t, payload):
    return json.dumps({"timestamp": "2026-10-04T12:00:00Z", "type": t, "payload": payload}) + "\n"


RL = {"primary": {"used_percent": 12.0, "window_minutes": 300, "resets_at": 1791133941},
      "secondary": {"used_percent": 34.0, "window_minutes": 10080, "resets_at": 1791662808},
      "plan_type": "plus"}


class CodexTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = os.path.join(self.tmp.name, time.strftime("%Y/%m/%d"))
        os.makedirs(d)
        self.path = os.path.join(d, "rollout-x.jsonl")
        self.f = open(self.path, "w")
        self.put("session_meta", {"session_id": "abc", "cwd": "/home/u/proj"})

    def tearDown(self):
        self.f.close()
        self.tmp.cleanup()

    def put(self, t, payload):
        self.f.write(line(t, payload))
        self.f.flush()

    async def test_state_tools_and_metrics(self):
        async with Env() as e:
            b = e.bridge
            b.usage = {"five_h": 99.0}                  # Claude's limits must not leak into Codex
            tail = codex.Tailer(b, self.tmp.name)
            self.put("event_msg", {"type": "task_started", "model_context_window": 200000})
            tail.poll(time.time())
            lay = b.layout
            self.assertEqual((lay["agent"], lay["state"], lay["activity"]), ("codex", "working", "Thinking"))
            self.put("response_item", {"type": "function_call", "name": "shell", "call_id": "c1",
                                       "arguments": json.dumps({"command": ["ls", "-l"]})})
            tail.poll(time.time())
            self.assertEqual(b.layout["activity"], "Bash: ls -l")
            self.put("response_item", {"type": "function_call_output", "call_id": "c1"})
            self.put("event_msg", {"type": "token_count", "rate_limits": RL, "info": {
                "model_context_window": 200000, "last_token_usage": {"total_tokens": 50000}}})
            tail.poll(time.time())
            m = b.layout["metrics"]
            self.assertEqual((m["five_h"], m["seven_d"], m["ctx"], m["plan"]), (12.0, 34.0, 25.0, "Plus"))
            self.put("event_msg", {"type": "task_complete"})
            tail.poll(time.time())
            self.assertEqual(b.layout["state"], "done")

    async def test_backfill_does_not_flash_done_and_log_expiry_ends_session(self):
        async with Env() as e:
            b = e.bridge
            self.put("event_msg", {"type": "task_started"})
            self.put("event_msg", {"type": "task_complete"})
            tail = codex.Tailer(b, self.tmp.name)
            tail.poll(time.time())
            self.assertEqual(b.layout["state"], "idle")
            tail.poll(time.time() + codex.ACTIVE_S + 5)
            self.assertEqual(b.layout["mode"], "none")

    async def test_claude_session_keeps_claude_limits(self):
        async with Env() as e:
            e.bridge.usage = {"five_h": 77.0}
            e.send("UserPromptSubmit")
            self.assertEqual(e.bridge.layout["agent"], "claude")
            self.assertEqual(e.bridge.layout["metrics"]["five_h"], 77.0)


if __name__ == "__main__":
    unittest.main()
