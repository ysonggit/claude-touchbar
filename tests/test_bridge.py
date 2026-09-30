import asyncio
import json
import os
import subprocess
import time
import unittest

from helpers import Env, ROOT


def perm(cmd="touch x", tool="Bash", tuid="t1", sid="s1", **extra):
    d = {"hook_event_name": "PermissionRequest", "session_id": sid, "cwd": "/home/u/ARX",
         "tool_name": tool, "tool_input": {"command": cmd}, "tool_use_id": tuid}
    d.update(extra)
    return d


async def result(proc, timeout=3):
    out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    return out.decode()


class StateTests(unittest.IsolatedAsyncioTestCase):
    async def test_activity_states(self):
        async with Env() as e:
            e.send("SessionStart")
            self.assertEqual(e.bridge.layout["state"], "idle")
            e.send("UserPromptSubmit")
            self.assertEqual(e.bridge.layout["state"], "working")
            self.assertEqual(e.bridge.layout["activity"], "Thinking")
            self.assertIsNotNone(e.bridge.layout["timer_start"])
            e.send("PreToolUse", tool_name="Bash", tool_input={"command": "cargo test\nsecond line"})
            self.assertEqual(e.bridge.layout["activity"], "Bash: cargo test")
            e.send("SubagentStart")
            e.send("SubagentStart")
            e.send("SubagentStop")
            self.assertEqual(e.bridge.layout["subagents"], 1)
            e.send("PreCompact")
            self.assertTrue(e.bridge.layout["compacting"])
            e.send("PostCompact")
            e.send("PostToolUse", tool_name="Bash")
            self.assertEqual(e.bridge.layout["activity"], "Thinking")
            e.send("Stop")
            self.assertEqual(e.bridge.layout["state"], "done")
            e.send("StopFailure")
            self.assertEqual(e.bridge.layout["state"], "error")
            e.send("SessionEnd")
            self.assertEqual(e.bridge.layout["mode"], "none")

    async def test_done_lingers_then_idle(self):
        async with Env() as e:
            e.bridge.cfg["display"]["done_linger_s"] = 0.15
            e.send("UserPromptSubmit")
            e.send("Stop")
            self.assertEqual(e.bridge.layout["state"], "done")
            await asyncio.sleep(0.35)  # scheduled wake-up refreshes without any event
            self.assertEqual(e.bridge.layout["state"], "idle")

    async def test_metrics_hidden_when_missing(self):
        async with Env() as e:
            e.send("UserPromptSubmit")
            e.bridge.handle_status({"session_id": "s1", "model": {"display_name": "Opus"},
                                    "context_window": {"used_percentage": 62.0},
                                    "rate_limits": {"five_hour": {"used_percentage": 41, "resets_at": 1}},
                                    "cost": {"total_cost_usd": 1.24}, "effort": {"level": "high"}})
            m = e.bridge.layout["metrics"]
            self.assertEqual((m["ctx"], m["five_h"], m["cost"], m["model"], m["effort"]),
                             (62.0, 41, 1.24, "Opus", "high"))
            self.assertNotIn("seven_d", m)  # never shown as 0

    async def test_status_does_not_invalidate_interactive_seq(self):
        async with Env() as e:
            fut = asyncio.ensure_future(e.bridge.add_permission(perm()).fut)
            seq = e.bridge.layout_seq
            e.bridge.handle_status({"session_id": "s1", "context_window": {"used_percentage": 10}})
            self.assertEqual(e.bridge.layout_seq, seq)  # display-only update
            fut.cancel()

    async def test_sessions_priority_and_waiting_badge(self):
        async with Env() as e:
            e.send("UserPromptSubmit", session_id="a", cwd="/x/A")
            e.send("UserPromptSubmit", session_id="b", cwd="/x/B")
            e.send("Stop", session_id="b", cwd="/x/B")
            self.assertEqual(e.bridge.layout["chip"]["label"], "A")  # working beats done
            e.send("Notification", session_id="b", cwd="/x/B", notification_type="permission_prompt")
            self.assertEqual(e.bridge.layout["chip"]["label"], "B")  # needs input beats working
            self.assertEqual(e.bridge.layout["mode"], "terminal")
            self.assertEqual(e.bridge.layout["chip"]["waiting"], 0)
            e.bridge.manual = None
            e.send("Notification", session_id="a", cwd="/x/A", notification_type="permission_prompt")
            self.assertEqual(e.bridge.layout["chip"]["waiting"], 1)

    async def test_chip_tap_cycles_and_holds(self):
        async with Env() as e:
            bar = await e.bar()
            e.send("UserPromptSubmit", session_id="a", cwd="/x/A")
            e.send("UserPromptSubmit", session_id="b", cwd="/x/B")
            await bar.settle()
            first = bar.layout["chip"]["label"]
            await bar.tap("chip")
            self.assertNotEqual(bar.layout["chip"]["label"], first)
            e.send("PreToolUse", session_id=first and "a", cwd="/x/A", tool_name="Read", tool_input={})
            self.assertNotEqual(e.bridge.layout["chip"]["label"], first)  # manual choice holds
            await bar.close()


class PromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_allow_round_trip_with_real_hook(self):
        async with Env() as e:
            bar = await e.bar()
            hook = await e.run_hook(perm())
            await asyncio.sleep(0.5)
            lay = bar.layout
            self.assertEqual(lay["mode"], "permission")
            self.assertEqual([b["id"] for b in lay["buttons"]], ["deny", "allow", "chip"])
            self.assertEqual(lay["prompt"]["summary"], "Bash: touch x")
            await bar.tap("allow", held_ms=400)
            out = json.loads(await result(hook))
            self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PermissionRequest")
            self.assertEqual(out["hookSpecificOutput"]["decision"], {"behavior": "allow"})
            self.assertEqual(bar.layout["mode"], "status")
            await bar.close()

    async def test_deny_single_tap(self):
        async with Env() as e:
            bar = await e.bar()
            hook = await e.run_hook(perm())
            await asyncio.sleep(0.5)
            await bar.tap("deny")
            d = json.loads(await result(hook))["hookSpecificOutput"]["decision"]
            self.assertEqual(d["behavior"], "deny")
            self.assertIn("Denied from Touch Bar", d["message"])
            await bar.close()

    async def test_short_hold_refused_bridge_rechecks(self):
        async with Env() as e:
            bar = await e.bar()
            fut = e.bridge.add_permission(perm()).fut
            await asyncio.sleep(0.4)
            await bar.tap("allow", held_ms=399)
            self.assertFalse(fut.done())
            self.assertEqual(e.bridge.handle_tap({"seq": bar.layout["seq"], "button": "allow", "held_ms": 399}), "hold")
            await bar.tap("allow", held_ms=400)
            self.assertTrue(fut.done())
            await bar.close()

    async def test_stale_and_guard(self):
        async with Env() as e:
            bar = await e.bar()
            p = e.bridge.add_permission(perm())
            await asyncio.sleep(0.05)
            seq = bar.layout["seq"]
            self.assertEqual(e.bridge.handle_tap({"seq": seq, "button": "deny", "held_ms": 0}), "guard")
            self.assertFalse(p.fut.done())
            self.assertEqual(e.bridge.handle_tap({"seq": seq - 1, "button": "deny", "held_ms": 0}), "stale")
            await asyncio.sleep(0.3)
            self.assertEqual(e.bridge.handle_tap({"seq": seq, "button": "deny", "held_ms": 0}), "ok")
            # replaying the same tap after the prompt is gone does nothing (and never allows)
            self.assertEqual(e.bridge.handle_tap({"seq": seq, "button": "allow", "held_ms": 9999}), "stale")
            await bar.close()

    async def test_timeout_falls_back_to_terminal(self):
        async with Env(prompt_timeout_s=0.3) as e:
            bar = await e.bar()
            hook = await e.run_hook(perm())
            self.assertEqual(await result(hook), "")  # no output → Claude shows its own prompt
            self.assertEqual(bar.layout["mode"], "terminal")
            self.assertEqual(e.bridge.prompts, [])
            await bar.close()

    async def test_terminal_answers_first_clears_prompt(self):
        async with Env() as e:
            bar = await e.bar()
            hook = await e.run_hook(perm(tuid="t1"))
            await asyncio.sleep(0.5)
            e.send("PostToolUse", tool_name="Bash", tool_use_id="other")  # different tool: keep
            self.assertEqual(len(e.bridge.prompts), 1)
            e.send("PostToolUse", tool_name="Bash", tool_use_id="t1")
            self.assertEqual(await result(hook), "")
            self.assertEqual(e.bridge.prompts, [])
            await bar.tap("allow", held_ms=500)  # late tap on the gone prompt: ignored
            await bar.close()

    async def test_hook_dies_abandons_prompt(self):
        async with Env() as e:
            hook = await e.run_hook(perm())
            await asyncio.sleep(0.5)
            self.assertEqual(len(e.bridge.prompts), 1)
            hook.kill()
            await hook.wait()
            await asyncio.sleep(0.2)
            self.assertEqual(e.bridge.prompts, [])

    async def test_dangerous_needs_longer_hold_and_no_always(self):
        async with Env(always_button=True) as e:
            bar = await e.bar()
            fut = e.bridge.add_permission(perm("rm -rf build/")).fut
            await asyncio.sleep(0.4)
            allow = next(b for b in bar.layout["buttons"] if b["id"] == "allow")
            self.assertEqual(allow["hold_ms"], 1000)
            self.assertTrue(bar.layout["prompt"]["dangerous"])
            self.assertNotIn("always", [b["id"] for b in bar.layout["buttons"]])
            await bar.tap("allow", held_ms=500)
            self.assertFalse(fut.done())
            await bar.tap("allow", held_ms=1000)
            self.assertTrue(fut.done())
            await bar.close()

    async def test_always_builds_session_rule(self):
        async with Env(always_button=True) as e:
            bar = await e.bar()
            fut = e.bridge.add_permission(perm("cargo build --release")).fut
            await asyncio.sleep(0.4)
            self.assertIn("always", [b["id"] for b in bar.layout["buttons"]])
            await bar.tap("always", held_ms=400)
            d = fut.result()["decision"]
            self.assertEqual(d["behavior"], "allow")
            self.assertEqual(d["updatedPermissions"], {"rule": "Bash(cargo build *)", "scope": "session"})
            await bar.close()

    async def test_plan_prompt(self):
        async with Env() as e:
            bar = await e.bar()
            fut = e.bridge.add_permission(perm(tool="ExitPlanMode", tuid="t9")).fut
            e.bridge._session({"session_id": "s1"})
            p = e.bridge.prompts[0]
            self.assertEqual(p.kind, "plan")
            e.bridge._finish(p, None)
            fut = e.bridge.add_permission({**perm(tool="ExitPlanMode"), "tool_input": {
                "plan": "intro\n# Write SPEC.md for Touch Bar\n- a"}}).fut
            await asyncio.sleep(0.4)
            self.assertEqual(bar.layout["mode"], "plan")
            self.assertEqual(bar.layout["prompt"]["summary"], "Write SPEC.md for Touch Bar")
            await bar.tap("reject")
            d = fut.result()["decision"]
            self.assertEqual(d["behavior"], "deny")
            self.assertIn("Plan rejected", d["message"])
            await bar.close()

    async def test_question_is_read_only_with_terminal_dismiss(self):
        async with Env() as e:
            bar = await e.bar()
            e.send("PreToolUse", tool_name="AskUserQuestion", tool_input={"questions": [
                {"question": "Which OS?", "options": [{"label": "macOS"}, {"label": "Linux"}]}]})
            await asyncio.sleep(0.4)
            self.assertEqual(bar.layout["mode"], "question")
            self.assertEqual(bar.layout["prompt"]["options"], ["macOS", "Linux"])
            self.assertEqual([b["id"] for b in bar.layout["buttons"]], ["terminal", "chip"])
            await bar.tap("terminal")
            self.assertEqual(bar.layout["mode"], "status")
            await bar.close()

    async def test_fifo_across_sessions(self):
        async with Env() as e:
            bar = await e.bar()
            f1 = e.bridge.add_permission(perm("touch a", sid="s1", tuid="a")).fut
            f2 = e.bridge.add_permission(perm("touch b", sid="s2", tuid="b")).fut
            await asyncio.sleep(0.4)
            self.assertEqual(bar.layout["prompt"]["summary"], "Bash: touch a")
            self.assertEqual(bar.layout["prompt"]["total"], 2)
            await bar.tap("deny")
            self.assertTrue(f1.done() and not f2.done())
            await asyncio.sleep(0.4)
            self.assertEqual(bar.layout["prompt"]["summary"], "Bash: touch b")
            await bar.close()

    async def test_bridge_down_fails_open(self):
        env = dict(os.environ, CTB_RUNTIME_DIR="/nonexistent-dir")
        t = time.perf_counter()
        r = subprocess.run([os.path.join(ROOT, "bin", "ctb-hook")], input=json.dumps(perm()).encode(),
                           capture_output=True, env=env)
        self.assertEqual((r.returncode, r.stdout), (0, b""))
        self.assertLess(time.perf_counter() - t, 0.5)


class FallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_notification_when_no_bar_is_alert_only(self):
        async with Env() as e:
            p = e.bridge.add_permission(perm())
            self.assertEqual(e.notifier.started, [(p.id, "ARX")])
            # the notifier has no way to resolve a prompt: there is no callback into the bridge
            self.assertFalse(p.fut.done())
            e.bridge._finish(p, None)
            self.assertIn(p.id, e.notifier.stopped)   # alert is withdrawn when resolved elsewhere

    async def test_no_notification_when_bar_connected(self):
        async with Env() as e:
            bar = await e.bar()
            e.bridge.add_permission(perm())
            self.assertEqual(e.notifier.started, [])
            await bar.close()

    async def test_bar_disconnect_triggers_notifications(self):
        async with Env() as e:
            bar = await e.bar()
            e.bridge.add_permission(perm())
            await bar.close()
            await asyncio.sleep(0.2)
            self.assertEqual(len(e.notifier.started), 1)


class SocketSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_socket_modes(self):
        async with Env() as e:
            for path in (os.path.join(e.tmp.name, "ctb.sock"), e.render_path):
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
