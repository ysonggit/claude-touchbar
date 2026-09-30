"""Turns the T1 Touch Bar panel on in USB config 2.

Port of scripts/dispon.py from barkeep (https://github.com/mgd34msu/barkeep),
Copyright (C) 2026 Mike Davis, GPL-2.0.

barkeep-dfr streams frames but leaves the panel dark; the panel is switched on with a HID feature
report on the iBridge's usage-page 0xff12 / usage 0x21 collection (interface 6 in config 2).
"""

import fcntl
import glob
import os

USAGE_PAGE, USAGE = 0xFF12, 0x21


def display_report_id(descriptor):
    """Report ID that carries the display usage in a raw HID report descriptor, or None."""
    i, page, rid = 0, None, None
    while i < len(descriptor):
        b = descriptor[i]
        size = b & 3
        size = 4 if size == 3 else size
        kind, tag = (b >> 2) & 3, (b >> 4) & 0xF
        value = int.from_bytes(descriptor[i + 1:i + 1 + size], "little")
        if kind == 1 and tag == 0:      # global: Usage Page
            page = value
        elif kind == 1 and tag == 8:    # global: Report ID
            rid = value
        elif kind == 2 and tag == 0 and page == USAGE_PAGE and value == USAGE:   # local: Usage
            return rid
        i += 1 + size
    return None


def _hidiocsfeature(length):
    return (3 << 30) | (length << 16) | (ord("H") << 8) | 0x06


def panel_on(sysfs="/sys/bus/hid/devices"):
    """Send the panel-on report to every matching hidraw node. Returns how many accepted it."""
    ok = 0
    for dev in sorted(glob.glob(os.path.join(sysfs, "*"))):
        try:
            with open(os.path.join(dev, "report_descriptor"), "rb") as f:
                rid = display_report_id(f.read())
        except OSError:
            continue
        raws = glob.glob(os.path.join(dev, "hidraw", "hidraw*"))
        if rid is None or not raws:
            continue
        buf = bytearray(11)
        buf[0], buf[1], buf[2] = rid, 1, 1
        try:
            fd = os.open("/dev/" + os.path.basename(raws[0]), os.O_RDWR)
            try:
                fcntl.ioctl(fd, _hidiocsfeature(len(buf)), bytes(buf))
                ok += 1
            finally:
                os.close(fd)
        except OSError:
            pass
    return ok
