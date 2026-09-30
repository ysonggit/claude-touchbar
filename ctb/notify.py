"""Desktop-notification fallback (R-36): an ALERT ONLY, never a decision.

When the Touch Bar is unavailable, a permission request raises a critical notification telling the
user to answer in the terminal. It deliberately has no Allow/Deny buttons: on the GNOME session this
was developed on, notification buttons were activated automatically (~2 s after appearing, and not
always the same button), so a notification can never be trusted to carry a human decision.

`python -m ctb.notify <session label> <tool summary>` shows the alert and exits when it is closed.
"""

import asyncio
import sys


class Notifier:
    """Bridge-side: spawns the alert helper per prompt and terminates it when the prompt is resolved."""

    def __init__(self):
        self._procs = {}

    def start(self, prompt, label):
        async def run():
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "ctb.notify", label, prompt.summary,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            except Exception:
                return
            self._procs[prompt.id] = proc
            await proc.wait()
            self._procs.pop(prompt.id, None)
        return asyncio.ensure_future(run())

    def stop(self, prompt):
        proc = self._procs.pop(prompt.id, None)
        if proc and proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass


def _show(label, summary):
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    loop = GLib.MainLoop()
    res = bus.call_sync(
        "org.freedesktop.Notifications", "/org/freedesktop/Notifications",
        "org.freedesktop.Notifications", "Notify",
        GLib.Variant("(susssasa{sv}i)", ("Claude Touch Bar", 0, "dialog-question",
                                         "Claude (%s) needs your permission" % label,
                                         summary + "\nAnswer in the terminal.", [],
                                         {"urgency": GLib.Variant("y", 2)}, 0)),
        GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, -1, None)
    nid = res.unpack()[0]
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, 15, lambda: (loop.quit(), False)[1])

    def closed(_c, _s, _p, _i, _sig, params):
        if params.unpack()[0] == nid:
            loop.quit()
    bus.signal_subscribe("org.freedesktop.Notifications", "org.freedesktop.Notifications",
                         "NotificationClosed", "/org/freedesktop/Notifications", None,
                         Gio.DBusSignalFlags.NONE, closed)
    try:
        loop.run()
    finally:
        try:
            bus.call_sync("org.freedesktop.Notifications", "/org/freedesktop/Notifications",
                          "org.freedesktop.Notifications", "CloseNotification",
                          GLib.Variant("(u)", (nid,)), None, Gio.DBusCallFlags.NONE, -1, None)
        except Exception:
            pass


if __name__ == "__main__":
    _show(sys.argv[1], sys.argv[2])
