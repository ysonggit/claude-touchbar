import asyncio
import unittest

from helpers import Env  # noqa: F401  (sets sys.path)
from ctb.bar.backends import NullBackend
from ctb.bar.daemon import BarDaemon
from ctb.bar.inputs import FakeKeys, QueueTouch


def perm(cmd="touch x", tuid="t1", sid="s1"):
    return {"hook_event_name": "PermissionRequest", "session_id": sid, "cwd": "/home/u/ARX",
            "tool_name": "Bash", "tool_input": {"command": cmd}, "tool_use_id": tuid}


class BarEnv:
    def __init__(self, env, **bar_cfg):
        cfg = env.bridge.cfg
        cfg["bar"].update(bar_cfg)
        self.backend, self.touch, self.keys = NullBackend(), QueueTouch(), FakeKeys()
        self.bar = BarDaemon(cfg, self.backend, self.touch, self.keys)
        self.task = asyncio.ensure_future(self.bar.run())

    async def stop(self):
        self.task.cancel()
        try:
            await self.task
        except asyncio.CancelledError:
            pass

    def region(self, rid):
        return next(r for r in self.bar.regions if r.id == rid)

    def center(self, rid):
        r = self.region(rid)
        return int((r.x0 + r.x1) / 2)

    async def touch_down(self, rid):
        self.touch.put("down", self.center(rid))

    def up(self):
        self.touch.put("up")


class BarTests(unittest.IsolatedAsyncioTestCase):
    async def test_offline_shows_function_keys_and_keys_work(self):
        async with Env() as e:
            await e.bridge.stop()  # no bridge at all
            b = BarEnv(e)
            await asyncio.sleep(0.3)
            self.assertIsNone(b.bar.layout)
            self.assertGreaterEqual(b.bar.frames, 1)
            await b.touch_down("key:mute"); b.up()
            await asyncio.sleep(0.1)
            self.assertEqual(b.keys.pressed, ["mute"])       # R-66: function row works with no bridge
            await b.stop()

    async def test_hold_allow_fires_after_hold_only(self):
        async with Env() as e:
            b = BarEnv(e)
            fut = e.bridge.add_permission(perm()).fut
            await asyncio.sleep(0.6)
            self.assertEqual(b.bar.layout["mode"], "permission")
            # early release (200 ms): nothing
            await b.touch_down("allow"); await asyncio.sleep(0.2); b.up()
            await asyncio.sleep(0.2)
            self.assertFalse(fut.done())
            # full hold: fires while the finger is still down
            await b.touch_down("allow"); await asyncio.sleep(0.65)
            self.assertTrue(fut.done())
            self.assertEqual(fut.result()["decision"]["behavior"], "allow")
            b.up()
            await b.stop()

    async def test_deny_tap_and_layout_returns_to_status(self):
        async with Env() as e:
            b = BarEnv(e)
            fut = e.bridge.add_permission(perm()).fut
            await asyncio.sleep(0.6)
            await b.touch_down("deny"); b.up()
            await asyncio.sleep(0.2)
            self.assertEqual(fut.result()["decision"]["behavior"], "deny")
            self.assertEqual(b.bar.layout["mode"], "status")
            await b.stop()

    async def test_sliding_off_cancels_hold(self):
        async with Env() as e:
            b = BarEnv(e)
            fut = e.bridge.add_permission(perm()).fut
            await asyncio.sleep(0.6)
            x = b.center("allow")
            b.touch.put("down", x); await asyncio.sleep(0.15)
            b.touch.put("move", x + 120)          # > 40 px slop
            await asyncio.sleep(0.6)
            self.assertFalse(fut.done())
            await b.stop()

    async def test_layout_change_under_finger_cancels(self):
        async with Env() as e:
            b = BarEnv(e)
            e.bridge.add_permission(perm(tuid="a", sid="s1"))
            fut2 = e.bridge.add_permission(perm(tuid="b", sid="s2")).fut
            await asyncio.sleep(0.6)
            await b.touch_down("allow"); await asyncio.sleep(0.2)
            e.bridge.prompts[0].fut.cancel()
            e.bridge._finish(e.bridge.prompts[0], None)   # prompt vanishes → new prompt/buttons
            await asyncio.sleep(0.7)
            self.assertFalse(fut2.done())                # the finger must not approve the NEW prompt
            await b.stop()

    async def test_fn_shows_function_row(self):
        async with Env() as e:
            b = BarEnv(e)
            e.send("UserPromptSubmit")
            await asyncio.sleep(0.3)
            b.bar.set_fn(True)
            await asyncio.sleep(0.2)
            ids = {r.id for r in b.bar.regions}
            self.assertTrue({"key:F1", "key:F12", "key:ESC"} <= ids)
            await b.touch_down("key:F5"); b.up()
            await asyncio.sleep(0.1)
            self.assertEqual(b.keys.pressed, ["F5"])
            b.bar.set_fn(False)
            await asyncio.sleep(0.2)
            self.assertIn("chip", {r.id for r in b.bar.regions})
            await b.stop()

    async def test_no_sessions_shows_media_keys(self):
        async with Env() as e:
            b = BarEnv(e)
            await asyncio.sleep(0.3)
            self.assertEqual(b.bar.layout["mode"], "none")
            self.assertIn("key:playpause", {r.id for r in b.bar.regions})
            await b.stop()

    async def test_idle_sends_no_frames(self):
        async with Env() as e:
            b = BarEnv(e)
            e.send("SessionStart")
            await asyncio.sleep(0.4)
            n = b.bar.frames
            await asyncio.sleep(0.8)
            self.assertEqual(b.bar.frames, n)             # R-07: nothing changes → nothing sent
            await b.stop()

    async def test_working_animates_at_most_10fps(self):
        async with Env() as e:
            b = BarEnv(e)
            e.send("UserPromptSubmit")
            await asyncio.sleep(0.4)
            n = b.bar.frames
            await asyncio.sleep(1.0)
            fps = b.bar.frames - n
            self.assertTrue(5 <= fps <= 12, fps)
            await b.stop()

    async def test_dim_off_and_prompt_wakes(self):
        async with Env() as e:
            b = BarEnv(e, dim_after_s=0.3, off_after_s=0.7)
            e.send("SessionStart")
            await asyncio.sleep(0.2)
            self.assertAlmostEqual(b.bar.brightness(), 1.0, places=2)
            await asyncio.sleep(0.3)
            self.assertAlmostEqual(b.bar.brightness(), 0.3, places=2)
            await asyncio.sleep(0.6)
            self.assertEqual(b.bar.brightness(), 0.0)
            self.assertEqual(set(b.backend.frames[-1]), {0})   # all-black frame when off
            e.bridge.add_permission(perm())                    # new prompt wakes it (R-33)
            await asyncio.sleep(0.12)
            self.assertAlmostEqual(b.bar.brightness(), 1.0, places=2)
            await b.stop()

    async def test_bridge_restart_bar_reconnects(self):
        async with Env() as e:
            b = BarEnv(e)
            await asyncio.sleep(0.3)
            self.assertIsNotNone(b.bar.layout)
            await e.bridge.stop()
            for w in list(e.bridge.bars):
                w.close()
            await asyncio.sleep(0.3)
            self.assertIsNone(b.bar.layout)                 # R-06: back to function row + "offline"
            await e.bridge.start()
            await asyncio.sleep(2.6)
            self.assertIsNotNone(b.bar.layout)              # reconnects within ~2 s
            await b.stop()


if __name__ == "__main__":
    unittest.main()


class BackendFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_backend_error_is_not_fatal_and_retried(self):
        async with Env() as e:
            b = BarEnv(e)
            real = b.backend.present
            fail = {"n": 2}

            def flaky(rgb):
                if fail["n"] > 0:
                    fail["n"] -= 1
                    raise OSError("device busy")
                real(rgb)
            b.backend.present = flaky
            e.send("UserPromptSubmit")                 # forces redraws
            await asyncio.sleep(0.6)
            self.assertFalse(b.task.done())            # daemon still alive
            self.assertGreaterEqual(len(b.backend.frames), 1)   # and recovered
            await b.stop()
