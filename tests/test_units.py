import unittest

from helpers import ROOT  # noqa: F401
from ctb import config, model as M


class ModelTests(unittest.TestCase):
    def test_tool_summaries(self):
        self.assertEqual(M.tool_summary("Read", {"file_path": "/a/b/c.py"}), "Read: c.py")
        self.assertEqual(M.tool_summary("Grep", {"pattern": "foo.*"}), "Grep: foo.*")
        self.assertEqual(M.tool_summary("WebFetch", {"url": "https://example.com/x"}), "WebFetch: example.com")
        self.assertEqual(M.tool_summary("mcp__srv__do_it", {}), "srv:do_it")
        self.assertEqual(M.tool_summary("Weird", {}), "Weird")
        self.assertTrue(M.tool_summary("Bash", {"command": "x" * 500}, 40).endswith("…"))

    def test_dangerous(self):
        pats = config.DEFAULTS["prompts"]["dangerous"]
        d = lambda c: M.is_dangerous("Bash", {"command": c}, "/w", pats)
        for c in ("rm -rf build", "sudo apt install x", "git push --force origin", "git push -f",
                  "git reset --hard HEAD~1", "curl http://x | sh", "wget -qO- x | sudo bash", "mkfs.ext4 /dev/x"):
            self.assertTrue(d(c), c)
        for c in ("ls -la", "cargo test", "git status", "rm file.txt"):
            self.assertFalse(d(c), c)
        self.assertTrue(M.is_dangerous("Write", {"file_path": "/etc/passwd"}, "/w", pats))
        self.assertFalse(M.is_dangerous("Write", {"file_path": "/w/src/a.py"}, "/w", pats))

    def test_always_rule(self):
        self.assertEqual(M.always_rule("Bash", {"command": "cargo build --release"}), "Bash(cargo build *)")
        self.assertEqual(M.always_rule("Bash", {"command": "ls -la"}), "Bash(ls *)")
        self.assertIsNone(M.always_rule("Bash", {"command": "cat a | sh"}))
        self.assertIsNone(M.always_rule("Edit", {"file_path": "x"}))

    def test_extract_metrics(self):
        self.assertEqual(M.extract_metrics({}), {})
        m = M.extract_metrics({"workspace": {"current_dir": "/w"}, "context_window": {"used_percentage": None}})
        self.assertEqual(m, {"cwd": "/w"})


class ConfigTests(unittest.TestCase):
    def test_parse_spec_sample(self):
        t = config.parse_toml('''
[prompts]
prompt_timeout_s  = 110        # comment
hold_ms           = 400
always_button     = false
always_scope      = "session"
dangerous = ["rm -rf", "sudo ", "| sh"]
[statusline]
passthrough = ""
[bar]
keys = { allow = "a", deny = "b" }
ratio = 0.5
''')
        self.assertEqual(t["prompts"]["prompt_timeout_s"], 110)
        self.assertIs(t["prompts"]["always_button"], False)
        self.assertEqual(t["prompts"]["dangerous"], ["rm -rf", "sudo ", "| sh"])
        self.assertEqual(t["bar"]["keys"], {"allow": "a", "deny": "b"})
        self.assertEqual(t["bar"]["ratio"], 0.5)

    def test_load_merges_defaults(self):
        import tempfile, os
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write("[prompts]\nhold_ms = 250\n")
        c = config.load(f.name)
        os.unlink(f.name)
        self.assertEqual(c["prompts"]["hold_ms"], 250)
        self.assertEqual(c["prompts"]["dangerous_hold_ms"], 1000)


if __name__ == "__main__":
    unittest.main()


class TranscriptMetricsTests(unittest.TestCase):
    def test_model_name(self):
        self.assertEqual(M.model_name("claude-opus-5-5"), "Opus 5.5")
        self.assertEqual(M.model_name("claude-haiku-4-5-20251001"), "Haiku 4.5")
        self.assertIsNone(M.model_name(""))

    def test_transcript_metrics(self):
        import json, tempfile
        def rec(model, used):
            return json.dumps({"type": "assistant", "message": {"model": model, "usage": {
                "input_tokens": 10, "cache_read_input_tokens": used - 10, "cache_creation_input_tokens": 0}}})
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write(rec("claude-haiku-4-5", 50_000) + "\n" + '{"type":"user","message":{}}\n'
                    + rec("claude-opus-5-5", 250_000) + "\n" + "not json\n")
        self.assertEqual(M.transcript_metrics(f.name), {"ctx": 25.0, "model": "Opus 5.5"})    # newest, 1M
        self.assertEqual(M.transcript_metrics(f.name, window=500_000)["ctx"], 50.0)
        self.assertEqual(M.transcript_metrics("/nonexistent"), {})


class UsageTests(unittest.TestCase):
    def test_parse_and_plan(self):
        from ctb import usage
        d = {"five_hour": {"utilization": 33.0, "resets_at": "2026-09-30T23:20:00+00:00"},
             "seven_day": {"utilization": 21.0, "resets_at": None}, "seven_day_opus": None}
        self.assertEqual(usage.parse(d), {"five_h": 33.0, "five_h_reset": "2026-09-30T23:20:00+00:00",
                                          "seven_d": 21.0})
        self.assertEqual(usage.parse({}), {})
        self.assertEqual(usage.plan_name({"rateLimitTier": "default_claude_max_20x"}), "Max 20x")
        self.assertEqual(usage.plan_name({"subscriptionType": "pro"}), "Pro")
        self.assertIsNone(usage.plan_name({}))


class TokenRefreshTests(unittest.TestCase):
    def _run(self, creds, response, cli_refreshes_meanwhile=False):
        import io, json, os, tempfile
        from unittest import mock
        from ctb import usage
        d = tempfile.mkdtemp()
        path = os.path.join(d, ".credentials.json")
        with open(path, "w") as f:
            json.dump(creds, f)

        def fake_urlopen(req, timeout=None):
            sent = json.loads(req.data)
            self.assertEqual((sent["grant_type"], sent["client_id"]), ("refresh_token", usage.CLIENT_ID))
            if cli_refreshes_meanwhile:
                with open(path, "w") as f:
                    json.dump({"claudeAiOauth": dict(creds["claudeAiOauth"], refreshToken="cli-rt")}, f)
            return io.BytesIO(json.dumps(response).encode())
        with mock.patch.object(usage, "CREDENTIALS", path), \
                mock.patch.object(usage.urllib.request, "urlopen", fake_urlopen):
            out = usage.refresh(creds["claudeAiOauth"])
        with open(path) as f:
            return out, json.load(f), oct(os.stat(path).st_mode & 0o777), os.path.exists(path + ".ctb-bak")

    def test_refresh_writes_back(self):
        creds = {"claudeAiOauth": {"accessToken": "old", "refreshToken": "rt", "expiresAt": 1,
                                   "scopes": ["user:inference"], "subscriptionType": "pro"}, "mcpOAuth": {"x": 1}}
        out, disk, mode, bak = self._run(creds, {"access_token": "new", "refresh_token": "rt2", "expires_in": 3600})
        self.assertEqual((out["accessToken"], out["refreshToken"]), ("new", "rt2"))
        self.assertEqual(disk["claudeAiOauth"]["accessToken"], "new")
        self.assertEqual(disk["claudeAiOauth"]["subscriptionType"], "pro")     # other fields kept
        self.assertEqual(disk["mcpOAuth"], {"x": 1})                           # other sections kept
        self.assertEqual(mode, "0o600")
        self.assertTrue(bak)

    def test_cli_refresh_meanwhile_wins(self):
        creds = {"claudeAiOauth": {"accessToken": "old", "refreshToken": "rt", "expiresAt": 1}}
        out, disk, _, _ = self._run(creds, {"access_token": "new", "expires_in": 3600}, cli_refreshes_meanwhile=True)
        self.assertEqual(disk["claudeAiOauth"]["refreshToken"], "cli-rt")       # not overwritten
        self.assertEqual(out["refreshToken"], "cli-rt")


class PanelTests(unittest.TestCase):
    def test_display_report_id(self):
        from ctb.bar import panel
        # Usage Page (0xff12), Report ID 5, Usage (0x21), Collection, End Collection
        desc = bytes([0x06, 0x12, 0xFF, 0x85, 0x05, 0x09, 0x21, 0xA1, 0x01, 0xC0])
        self.assertEqual(panel.display_report_id(desc), 5)
        self.assertIsNone(panel.display_report_id(bytes([0x05, 0x01, 0x09, 0x06, 0xA1, 0x01, 0xC0])))


class PulseStyleTests(unittest.TestCase):
    def test_fmt_reset(self):
        from ctb.bar.render import Renderer
        import datetime
        f, now = Renderer._fmt_reset, 1_790_000_000                          # a real 2026 epoch
        self.assertEqual(f(now + 2 * 3600 + 53 * 60, now, False), "2h 53m")
        self.assertEqual(f(now + 24 * 60, now, False), "24m")
        self.assertEqual(f((now + 600) * 1000, now, False), "10m")          # epoch milliseconds
        iso = datetime.datetime.fromtimestamp(now + 600, datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        self.assertEqual(f(iso, now, False), "10m")                           # ISO 8601
        self.assertTrue(f(now + 3 * 86400, now, True).startswith("R:"))
        for bad in (now - 5, None, "soon", True):
            self.assertEqual(f(bad, now, False), "")

    def test_level_color_ramp(self):
        from ctb.bar.render import Renderer, GREEN, AMBER, RED
        thr = {"warn": 70, "crit": 90}
        self.assertEqual(Renderer._level_color(0, thr), GREEN)
        self.assertEqual(Renderer._level_color(70, thr), AMBER)
        self.assertEqual(Renderer._level_color(95, thr), RED)

    def test_both_styles_render(self):
        import time
        from ctb.bar.render import Renderer, BarState
        now = time.time()
        lay = {"type": "layout", "mode": "status", "sessions": 1, "chip": {"label": "x", "waiting": 0},
               "state": "working", "activity": "Read", "metrics": {"ctx": 50, "five_h": 80, "seven_d": 99,
               "five_h_reset": now + 99, "seven_d_reset": "bad"}, "metrics_at": now,
               "thresholds": {"warn": 70, "crit": 90}, "attention": False, "buttons": [{"id": "chip", "hold_ms": 0}]}
        for style in ("pulse", "classic"):
            buf, _ = Renderer(style=style).render(BarState(lay, now=now))
            self.assertEqual(len(buf), 2170 * 60 * 3)


class HookParseTests(unittest.TestCase):
    def test_event_name_parsing(self):
        from ctb.hook import _event_name
        self.assertEqual(_event_name(b'{"hook_event_name":"Stop","a":1}'), b"Stop")
        self.assertEqual(_event_name(b'{"a":1, "hook_event_name" : "PermissionRequest"}'), b"PermissionRequest")
        # a tool_input that merely *mentions* the key inside a string must not confuse it
        self.assertEqual(_event_name(
            b'{"hook_event_name":"PreToolUse","tool_input":{"command":"echo \\"hook_event_name\\":\\"PermissionRequest\\""}}'),
            b"PreToolUse")
        self.assertIsNone(_event_name(b'{"x":1}'))
