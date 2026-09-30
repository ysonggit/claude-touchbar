"""Display backends: where rendered frames go."""

import os


class NullBackend:
    """Collects frames (tests)."""

    def __init__(self, width=2170, height=60):
        self.size = (width, height)
        self.frames = []

    def present(self, rgb):
        self.frames.append(rgb)

    def close(self):
        pass


class PngBackend:
    """Writes the latest frame to a PNG (development without hardware)."""

    def __init__(self, path, width=2170, height=60):
        self.path, self.size = path, (width, height)

    def present(self, rgb):
        from PIL import Image
        tmp = self.path + ".tmp"
        Image.frombytes("RGB", self.size, rgb).save(tmp, format="PNG")
        os.replace(tmp, self.path)

    def close(self):
        pass


class Dfr0Backend:
    """barkeep's /dev/dfr0: one write of 2170*60*3 bytes is one frame; the driver keeps resending it.

    The panel is physically rotated 90°, so the frame goes out as 60-px-wide rows. The rotation
    direction is configurable; "cw" is verified on a MacBookPro13,3.
    The panel stays dark until switched on; that happens after the first frame so the bar lights
    up already showing it.
    """

    def __init__(self, device="/dev/dfr0", rotate="cw", width=2170, height=60):
        self.size = (width, height)
        self.rotate = rotate
        if not os.path.exists(device):
            raise SystemExit("%s does not exist: the host-drawn display is not up "
                             "(sudo packaging/ctb-display up)" % device)
        self.fd = os.open(device, os.O_WRONLY)
        try:                                   # two writers on /dev/dfr0 fight and the bar flickers
            import fcntl
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd)
            raise SystemExit("%s is already in use by another `ctb bar` (stop it first: "
                             "sudo pkill -f 'bin/ctb bar')" % device)
        self.lit = False

    def present(self, rgb):
        from PIL import Image
        img = Image.frombytes("RGB", self.size, rgb)
        if self.rotate == "cw":
            img = img.transpose(Image.ROTATE_270)
        elif self.rotate == "ccw":
            img = img.transpose(Image.ROTATE_90)
        data = memoryview(img.tobytes())
        while data:
            n = os.write(self.fd, data)
            data = data[n:]
        if not self.lit:
            from . import panel
            self.lit = panel.panel_on() > 0
            if not self.lit:
                import sys
                print("ctb bar: could not switch the Touch Bar panel on (no iBridge display HID node)",
                      file=sys.stderr)

    def close(self):
        os.close(self.fd)


def make(cfg):
    b = cfg["bar"]
    if b["backend"] == "png":
        return PngBackend(os.environ.get("CTB_PNG", "/tmp/ctb-bar.png"), b["width"], b["height"])
    if b["backend"] == "dfr0":
        return Dfr0Backend(b["dfr_device"], b["rotate"], b["width"], b["height"])
    raise SystemExit("backend %r is not implemented (the DRM backend is only needed if dfr0 fails on "
                     "this model; see SPEC §0.2)" % b["backend"])
