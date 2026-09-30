"""`ctb bar` entry point and hardware probes."""

import asyncio
import os
import struct
import sys

from .. import config as cfgmod
from . import backends, inputs
from .daemon import BarDaemon
from .render import BarState, Renderer


class StdinTouch(inputs.QueueTouch):
    """Simulator: lines 'tap X', 'hold X MS', 'down X', 'up' on stdin."""

    def start(self, loop):
        loop.add_reader(sys.stdin, self._line)

    def _line(self):
        parts = sys.stdin.readline().split()
        if not parts:
            return
        loop = asyncio.get_running_loop()
        cmd, args = parts[0], [int(p) for p in parts[1:]]
        if cmd == "tap":
            self.put("down", args[0]); self.put("up")
        elif cmd == "hold":
            self.put("down", args[0]); loop.call_later(args[1] / 1000, self.put, "up")
        elif cmd in ("down", "move"):
            self.put(cmd, args[0])
        elif cmd == "up":
            self.put("up")


def run(args):
    cfg = cfgmod.load(os.environ.get("CTB_BAR_CONFIG") or None)
    b = cfg["bar"]
    sim = "--sim" in args
    if "--png" in args:
        b["backend"] = "png"
        os.environ["CTB_PNG"] = args[args.index("--png") + 1]
    backend = backends.make(cfg)
    if sim:
        touch, keys = StdinTouch(), inputs.FakeKeys()
        fn = None
    else:
        dev = inputs.find_digitizer() if b["touch_device"] == "auto" else b["touch_device"]
        if not dev:
            print("ctb bar: no touch device (bar.touch_device = %r); touch is disabled" % b["touch_device"],
                  file=sys.stderr)
            touch = inputs.QueueTouch()
        else:
            print("ctb bar: touch from %s" % dev, file=sys.stderr)
            touch = inputs.HidrawTouch(dev, b["width"], b["touch_x_offset"],
                                       b["touch_x_min"], b["touch_x_max"], b.get("touch_flip", False))
        keys = inputs.UinputKeys()
        fn = None
    bar = BarDaemon(cfg, backend, touch, keys)
    if not sim:
        fn = inputs.FnWatcher(bar.set_fn)
    try:
        asyncio.run(bar.run(fn))
    except KeyboardInterrupt:
        pass
    finally:
        backend.close()
        keys.close()
    return 0


def probe(args):
    """M0 hardware probes.  `probe pattern` → test pattern on the configured backend;
    `probe touch /dev/hidrawN` → print raw reports with float32 decodes."""
    if args and args[0] == "pattern":
        cfg = cfgmod.load(os.environ.get("CTB_BAR_CONFIG") or None)
        backend = backends.make(cfg)
        import cairo
        w, h = cfg["bar"]["width"], cfg["bar"]["height"]
        r = Renderer(w, h)
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        c = cairo.Context(surf)
        g = cairo.LinearGradient(0, 0, w, 0)
        g.add_color_stop_rgb(0, 1, 0, 0); g.add_color_stop_rgb(0.5, 0, 1, 0); g.add_color_stop_rgb(1, 0, 0, 1)
        c.set_source(g); c.paint()
        c.set_source_rgb(1, 1, 1); c.set_font_size(18)
        for x in range(0, w, 100):
            c.move_to(x + 2, 0); c.line_to(x + 2, 14 if x % 500 else 30); c.set_line_width(2); c.stroke()
            c.move_to(x + 4, 50); c.show_text(str(x))
        c.set_font_size(26); c.move_to(w / 2 - 120, 34); c.show_text("LEFT  ◄  ctb pattern  ►  RIGHT")
        backend.present(r._to_rgb(surf))
        print("pattern sent (%dx%d). Check orientation/colour order; adjust bar.rotate (cw|ccw)." % (w, h))
        return 0
    if args and args[0] == "touch":
        dev = args[1] if len(args) > 1 and args[1] != "auto" else inputs.find_digitizer()
        if not dev:
            print("no iBridge digitizer found: run `sudo packaging/ctb-display up` first (config 2)")
            return 1
        print("reading", dev)
        fd = os.open(dev, os.O_RDONLY)
        print("touch the bar; Ctrl-C to stop. off=offset of a float32 that stays within [0.5, 1.0]")
        try:
            while True:
                buf = os.read(fd, 256)
                fl = {i: round(struct.unpack_from("<f", buf, i)[0], 3) for i in range(0, len(buf) - 3)
                      if 0.4 <= struct.unpack_from("<f", buf, i)[0] <= 1.05}
                print(buf.hex(" "), "| float32 candidates:", fl)
        except KeyboardInterrupt:
            return 0
    print(probe.__doc__)
    return 2
