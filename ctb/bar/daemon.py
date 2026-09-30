"""ctb-bar: owns the display and touch. Draws the function row + the Claude region."""

import asyncio
import hashlib
import json
import sys
import time

from .. import config as cfgmod
from .render import BarState, Renderer

MIN_FRAME_S = 1 / 30
SPINNER_S = 0.1
TOUCH_SLOP_PX = 40           # sliding further than this cancels a tap/hold (R-47)
RECONNECT_S = 2


class BarDaemon:
    def __init__(self, cfg, backend, touch, keys, render_path=None, clock=time.monotonic):
        b = cfg["bar"]
        self.cfg, self.backend, self.touch, self.keys = cfg, backend, touch, keys
        self.render_path = render_path or cfgmod.render_sock()
        self.r = Renderer(b["width"], b["height"], b["font"], b["default_layer"], b["control_strip"],
                          b.get("style", "pulse"))
        self.clock = clock
        self.layout = None
        self.fn_held = False
        self.regions = []
        self.cur = None            # active touch: dict(region, x0, t0, fired)
        self.last_activity = clock()
        self._last_hash = None
        self._last_frame = 0.0
        self._dirty = asyncio.Event()
        self._writer = None
        self._prompt_id = None
        self.frames = 0
        self._warned = False

    # ------------------------------------------------------------ state

    def wake(self):
        self.last_activity = self.clock()
        self._dirty.set()

    def set_fn(self, held):
        self.fn_held = held
        self.wake()

    def brightness(self):
        idle = self.clock() - self.last_activity
        b = self.cfg["bar"]
        level = 1.0 if idle < b["dim_after_s"] else 0.3 if idle < b["off_after_s"] else 0.0
        # Only idle dimming here: the T1 drives the panel backlight from its own light sensor.
        return level

    def _holding(self):
        c = self.cur
        if c and c["region"].hold_ms > 0 and not c["fired"]:
            return (c["region"].id, min(1.0, (self.clock() - c["t0"]) * 1000 / c["region"].hold_ms))
        return None

    # ------------------------------------------------------------ touch

    def on_touch(self, kind, x):
        self.wake()
        if kind == "down":
            reg = self.r.hit(x)
            self.cur = {"region": reg, "x0": x, "t0": self.clock(), "fired": False} if reg else None
        elif kind == "move":
            if self.cur and abs(x - self.cur["x0"]) > TOUCH_SLOP_PX:
                self.cur = None
        elif kind == "up":
            c, self.cur = self.cur, None
            if c and not c["fired"] and c["region"].hold_ms == 0:
                self._fire(c["region"], int((self.clock() - c["t0"]) * 1000))

    def _tick_hold(self):
        c = self.cur
        if c and c["region"].hold_ms > 0 and not c["fired"]:
            held = int((self.clock() - c["t0"]) * 1000)
            if held >= c["region"].hold_ms:
                c["fired"] = True
                self._fire(c["region"], held)

    def _fire(self, region, held_ms):
        if region.kind == "key":
            self.keys.tap(region.key)
        elif self._writer is not None and self.layout is not None:
            msg = {"type": "tap", "seq": self.layout["seq"], "button": region.id, "held_ms": held_ms}
            try:
                self._writer.write((json.dumps(msg) + "\n").encode())
            except Exception:
                pass

    # ------------------------------------------------------------ bridge link

    def on_layout(self, lay):
        if self.cur and (self.layout or {}).get("seq") != lay.get("seq"):
            self.cur = None                  # buttons changed under the finger: cancel (R-42/R-47)
        self.layout = lay
        pid = (lay.get("prompt") or {}).get("id")
        if lay.get("attention") and pid != self._prompt_id:
            self.wake()                      # a new prompt always wakes the bar to full brightness
        self._prompt_id = pid
        self._dirty.set()

    async def bridge_loop(self):
        while True:
            try:
                reader, writer = await asyncio.open_unix_connection(self.render_path)
            except (OSError, asyncio.TimeoutError):
                self.layout = None
                self._dirty.set()
                await asyncio.sleep(RECONNECT_S)
                continue
            self._writer = writer
            writer.write(b'{"type":"hello","width":%d,"height":%d,"backend":"%s"}\n' % (
                self.r.w, self.r.h, self.cfg["bar"]["backend"].encode()))
            try:
                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    msg = json.loads(line)
                    if msg.get("type") == "layout" and msg.get("mode"):
                        self.on_layout(msg)
            except (OSError, ValueError):
                pass
            self._writer = None
            self.layout = None
            self._dirty.set()
            await asyncio.sleep(RECONNECT_S)

    async def touch_loop(self):
        while True:
            kind, x = await self.touch.queue.get()
            self.on_touch(kind, x)

    # ------------------------------------------------------------ drawing

    def _tick_s(self):
        """How soon the picture may change by itself (None = only on events)."""
        if self.cur and self.cur["region"].hold_ms and not self.cur["fired"]:
            return MIN_FRAME_S                      # hold progress fill
        lay = self.layout
        if lay and lay.get("chip"):
            if lay.get("state") == "working" or lay["chip"]["waiting"]:
                return SPINNER_S                    # spinner / pulsing +N badge
            if lay.get("timer_start"):
                return 1.0                          # m:ss timer
        return self._next_dim_s()

    def draw_frame(self, now=None):
        """Render and present if the picture changed. Returns True if a frame was sent."""
        self._tick_hold()
        st = BarState(self.layout, self.fn_held, self._holding(), self.brightness(),
                      now if now is not None else time.time())
        rgb, self.regions = self.r.render(st)
        h = hashlib.blake2b(rgb, digest_size=8).digest()
        if h == self._last_hash:
            return False
        try:
            self.backend.present(rgb)
        except OSError as e:
            # a failed display write must not kill the bar (the function row has to keep working)
            if not self._warned:
                print("ctb-bar: display write failed: %s (will retry)" % e, file=sys.stderr, flush=True)
                self._warned = True
            return False
        self._warned = False
        self._last_hash = h
        self.frames += 1
        return True

    async def render_loop(self):
        while True:
            try:
                await asyncio.wait_for(self._dirty.wait(), self._tick_s())
            except asyncio.TimeoutError:
                pass
            self._dirty.clear()
            wait = MIN_FRAME_S - (self.clock() - self._last_frame)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_frame = self.clock()
            self.draw_frame()

    def _next_dim_s(self):
        b = self.cfg["bar"]
        idle = self.clock() - self.last_activity
        for edge in (b["dim_after_s"], b["off_after_s"]):
            if idle < edge:
                return edge - idle + 0.05
        return None  # fully off: nothing to do until something happens

    async def run(self, fn_watcher=None):
        loop = asyncio.get_running_loop()
        self.touch.start(loop)
        if fn_watcher:
            fn_watcher.start(loop)
        await asyncio.gather(self.bridge_loop(), self.touch_loop(), self.render_loop())
