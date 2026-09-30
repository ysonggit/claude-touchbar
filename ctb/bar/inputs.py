"""Touch, Fn-key and virtual-keyboard inputs for the bar.

Touch sources yield ("down", x_px) / ("move", x_px) / ("up", None) into an asyncio.Queue.
"""

import asyncio
import ctypes
import fcntl
import glob
import os
import struct
import time

from .render import KEYCODES

TOUCH_RELEASE_GAP_S = 0.12  # barkeep infers release from a ~120 ms gap in reports
KEY_FN = 464


class QueueTouch:
    """Feed touches programmatically (tests, simulator)."""

    def __init__(self):
        self.queue = asyncio.Queue()

    def put(self, kind, x=None):
        self.queue.put_nowait((kind, x))

    def start(self, loop):
        pass


def find_digitizer():
    """hidraw node on USB interface 2 of the iBridge (05ac:8600), i.e. the Touch Bar digitizer in config 2."""
    for hr in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        p = os.path.realpath(os.path.join(hr, "device"))
        while p != "/" and not os.path.exists(os.path.join(p, "bInterfaceNumber")):
            p = os.path.dirname(p)
        try:
            with open(os.path.join(p, "bInterfaceNumber")) as f:
                if int(f.read(), 16) != 2:
                    continue
            usb = os.path.dirname(p)
            with open(os.path.join(usb, "idVendor")) as f, open(os.path.join(usb, "idProduct")) as g:
                if (f.read().strip(), g.read().strip()) == ("05ac", "8600"):
                    return "/dev/" + os.path.basename(hr)
        except (OSError, ValueError):
            continue
    return None


class HidrawTouch:
    """Reads the iBridge digitizer through a hidraw node.

    Verified on a MacBookPro13,3: 52-byte reports with a little-endian float32 X in [0.5, 1.0] at
    byte 0 (as barkeep documents). The offset inside the report and the hidraw node are configurable;
    use `ctb probe touch /dev/hidrawN` to find them (milestone M0), then set bar.touch_device,
    bar.touch_x_offset, bar.touch_x_min/max (and bar.touch_flip if left/right are mirrored).
    """

    def __init__(self, path, width, offset=1, xmin=0.5, xmax=1.0, flip=False):
        self.path, self.width, self.offset = path, width, offset
        self.xmin, self.xmax, self.flip = xmin, xmax, flip
        self.queue = asyncio.Queue()
        self._down = False
        self._release = None

    def start(self, loop):
        self.loop = loop
        self.fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
        loop.add_reader(self.fd, self._on_readable)

    def _px(self, x):
        f = (x - self.xmin) / (self.xmax - self.xmin)
        f = 1 - f if self.flip else f
        return max(0, min(self.width - 1, int(f * self.width)))

    def _on_readable(self):
        try:
            buf = os.read(self.fd, 64)
        except BlockingIOError:
            return
        if len(buf) < self.offset + 4:
            return
        x = struct.unpack_from("<f", buf, self.offset)[0]
        if not (self.xmin <= x <= self.xmax):
            return
        self.queue.put_nowait(("move" if self._down else "down", self._px(x)))
        self._down = True
        if self._release:
            self._release.cancel()
        self._release = self.loop.call_later(TOUCH_RELEASE_GAP_S, self._up)

    def _up(self):
        self._down = False
        self.queue.put_nowait(("up", None))


class FnWatcher:
    """Watches KEY_FN on the internal keyboard (raw evdev, no python-evdev needed). UNVERIFIED on
    hardware: requires that the applespi keyboard reports KEY_FN (SPEC M0)."""

    EVFMT = "llHHi"

    def __init__(self, on_change, path=None):
        self.on_change = on_change
        self.path = path or self._find()

    @staticmethod
    def _find():
        try:
            text = open("/proc/bus/input/devices").read()
        except OSError:
            return None
        for block in text.split("\n\n"):
            if "applespi" in block.lower() or "Apple Internal Keyboard" in block:
                for tok in block.split():
                    if tok.startswith("event"):
                        return "/dev/input/" + tok
        return None

    def start(self, loop):
        if not self.path:
            return False
        self.fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
        self.size = struct.calcsize(self.EVFMT)
        loop.add_reader(self.fd, self._read)
        return True

    def _read(self):
        try:
            data = os.read(self.fd, self.size * 32)
        except BlockingIOError:
            return
        for i in range(0, len(data) - self.size + 1, self.size):
            _s, _u, typ, code, val = struct.unpack_from(self.EVFMT, data, i)
            if typ == 1 and code == KEY_FN and val in (0, 1):
                self.on_change(bool(val))


class FakeKeys:
    def __init__(self):
        self.pressed = []

    def tap(self, name):
        self.pressed.append(name)

    def close(self):
        pass


class UinputKeys:
    """Virtual keyboard through /dev/uinput (needs root or a udev rule)."""

    UI_SET_EVBIT, UI_SET_KEYBIT = 0x40045564, 0x40045565
    UI_DEV_CREATE, UI_DEV_DESTROY = 0x5501, 0x5502

    def __init__(self):
        self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        fcntl.ioctl(self.fd, self.UI_SET_EVBIT, 1)   # EV_KEY
        fcntl.ioctl(self.fd, self.UI_SET_EVBIT, 0)   # EV_SYN
        for code in KEYCODES.values():
            fcntl.ioctl(self.fd, self.UI_SET_KEYBIT, code)
        name = b"ctb virtual keyboard".ljust(80, b"\0")
        dev = name + struct.pack("<HHHH", 0x03, 0x1209, 0xC7B0, 1) + struct.pack("<I", 0) \
            + b"\0" * (4 * 64 * 4)
        os.write(self.fd, dev)
        fcntl.ioctl(self.fd, self.UI_DEV_CREATE)
        time.sleep(0.1)

    def _emit(self, typ, code, val):
        t = time.time()
        os.write(self.fd, struct.pack("llHHi", int(t), int((t % 1) * 1e6), typ, code, val))

    def tap(self, name):
        code = KEYCODES.get(name)
        if code is None:
            return
        for v in (1, 0):
            self._emit(1, code, v)
            self._emit(0, 0, 0)

    def close(self):
        try:
            fcntl.ioctl(self.fd, self.UI_DEV_DESTROY)
        finally:
            os.close(self.fd)

