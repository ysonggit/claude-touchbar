"""Touch Bar renderer: layout model (from the bridge) → RGB24 frame + hit regions.

Everything is drawn with cairo so it does not depend on emoji/symbol fonts.
"""

import datetime
import math
import time

import cairo

W_DEFAULT, H_DEFAULT = 2170, 60

GREY = (0.62, 0.62, 0.66)
BLUE = (0.36, 0.62, 1.0)
AMBER = (1.0, 0.76, 0.22)
GREEN = (0.36, 0.85, 0.46)
RED = (1.0, 0.36, 0.36)
WHITE = (0.96, 0.96, 0.98)
KEY_BG = (0.17, 0.17, 0.19)
CLAUDE_ORANGE = (0.843, 0.467, 0.341)          # #D77757, Claude Code's own accent
# Claude Code's welcome-screen mascot, decoded from its block art
#   ▐▛███▜▌  /  ▝▜█████▛▘  /  ▘▘ ▝▝     ('#' = lit pixel)
MASCOT = [
    "..############..",
    "..##.######.##..",
    "################",
    "..############..",
    "...#.#....#.#...",
]
STATE_COLOR = {"idle": GREY, "working": BLUE, "input": AMBER, "done": GREEN, "error": RED}

FN_KEYS = ["F%d" % i for i in range(1, 13)]
MEDIA_ROW = ["bright_dn", "bright_up", "kbd_dn", "kbd_up", "prev", "playpause", "next",
             "mute", "vol_dn", "vol_up"]
STRIP = {"brightness": ["bright_dn", "bright_up"], "volume": ["vol_dn", "vol_up"],
         "mute": ["mute"], "playpause": ["playpause"]}

# linux/input-event-codes.h
KEYCODES = {"ESC": 1, "F1": 59, "F2": 60, "F3": 61, "F4": 62, "F5": 63, "F6": 64, "F7": 65,
            "F8": 66, "F9": 67, "F10": 68, "F11": 87, "F12": 88,
            "mute": 113, "vol_dn": 114, "vol_up": 115, "playpause": 164, "prev": 165, "next": 163,
            "bright_dn": 224, "bright_up": 225, "kbd_dn": 229, "kbd_up": 230}


class Region:
    def __init__(self, x0, x1, id, kind, hold_ms=0, key=None):
        self.x0, self.x1, self.id, self.kind, self.hold_ms, self.key = x0, x1, id, kind, hold_ms, key


class BarState:
    def __init__(self, layout=None, fn_held=False, holding=None, brightness=1.0, now=None):
        self.layout = layout          # None → bridge offline
        self.fn_held = fn_held
        self.holding = holding        # (region id, progress 0..1) or None
        self.brightness = brightness  # 0 = off
        self.now = now if now is not None else time.time()


class Renderer:
    def __init__(self, width=W_DEFAULT, height=H_DEFAULT, font="Sans",
                 default_layer="claude", control_strip=None, style="pulse"):
        self.w, self.h, self.font = width, height, font
        self.style = style          # "pulse" (claude-pulse-like meters) | "classic"
        self.default_layer = default_layer
        self.strip = control_strip if control_strip is not None else ["brightness", "volume", "mute", "playpause"]
        self.regions = []

    # ------------------------------------------------------------ primitives

    def _text_w(self, c, s, size, bold=False):
        c.select_font_face(self.font, cairo.FONT_SLANT_NORMAL,
                           cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL)
        c.set_font_size(size)
        return c.text_extents(s).x_advance

    def _fit(self, c, s, size, maxw, bold=False):
        if self._text_w(c, s, size, bold) <= maxw:
            return s
        while s and self._text_w(c, s + "…", size, bold) > maxw:
            s = s[:-1]
        return s + "…" if s else ""

    def _text(self, c, s, x, w, color, size=22, align="left", bold=False, alpha=1.0):
        s = self._fit(c, s, size, w, bold)
        tw = self._text_w(c, s, size, bold)
        tx = x if align == "left" else x + (w - tw) / 2 if align == "center" else x + w - tw
        c.set_source_rgba(*color, alpha)
        c.move_to(tx, self.h / 2 + size * 0.36)
        c.show_text(s)
        return tw

    def _rrect(self, c, x, y, w, h, r=9):
        c.new_sub_path()
        c.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        c.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        c.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        c.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        c.close_path()

    def _button(self, c, x, w, label, bg=KEY_BG, fg=WHITE, progress=0.0, size=22, bold=False, icon=None):
        y, h = 7, self.h - 14
        self._rrect(c, x, y, w, h)
        c.set_source_rgb(*bg)
        c.fill()
        if progress > 0:
            c.save()
            self._rrect(c, x, y, w, h)
            c.clip()
            c.set_source_rgba(1, 1, 1, 0.38)
            c.rectangle(x, y, w * min(progress, 1.0), h)
            c.fill()
            c.restore()
        if icon:
            self._icon(c, icon, x + w / 2, self.h / 2, fg)
        else:
            self._text(c, label, x + 6, w - 12, fg, size, "center", bold)

    def _bar(self, c, x, y, w, h, pct, thr, alpha=1.0):
        pct = max(0.0, min(100.0, pct))
        col = RED if pct >= thr["crit"] else AMBER if pct >= thr["warn"] else GREEN
        self._rrect(c, x, y, w, h, 3)
        c.set_source_rgba(1, 1, 1, 0.14 * alpha)
        c.fill()
        if pct > 0:
            self._rrect(c, x, y, max(6, w * pct / 100), h, 3)
            c.set_source_rgba(*col, alpha)
            c.fill()

    @staticmethod
    def _level_color(v, thr):
        """Green, easing to amber at `warn`, then to red at `crit` (claude-pulse's ramp)."""
        lerp = lambda a, b, t: tuple(a[i] + (b[i] - a[i]) * max(0.0, min(1.0, t)) for i in range(3))
        warn, crit = thr["warn"], thr["crit"]
        if v < warn:
            return lerp(GREEN, AMBER, (v / warn) ** 3 if warn else 1)
        return lerp(AMBER, RED, (v - warn) / (crit - warn) if crit > warn else 1)

    def _meter(self, c, x, w, pct, thr, alpha=1.0, cells=10):
        """Segmented ━━━─── meter; each lit cell is coloured by its own position on the ramp."""
        pct = max(0.0, min(100.0, pct))
        gap, cy = 4, self.h / 2
        cw = (w - gap * (cells - 1)) / cells
        lit = pct / 100 * cells
        for i in range(cells):
            cx = x + i * (cw + gap)
            c.rectangle(cx, cy - 1, cw, 2)
            c.set_source_rgba(1, 1, 1, 0.2 * alpha)
            c.fill()
            frac = max(0.0, min(1.0, lit - i))
            if frac > 0:
                c.rectangle(cx, cy - 4, cw * frac, 8)
                c.set_source_rgba(*self._level_color((i + 0.5) / cells * 100, thr), alpha)
                c.fill()

    def _icon(self, c, name, cx, cy, col):
        c.set_source_rgb(*col)
        c.set_line_width(2.4)
        c.set_line_cap(cairo.LINE_CAP_ROUND)
        if name in ("bright_dn", "bright_up"):
            r = 4 if name == "bright_dn" else 6
            c.new_sub_path()
            c.arc(cx, cy, r, 0, 2 * math.pi)
            c.fill() if name == "bright_dn" else c.stroke()
            for i in range(8):
                a = i * math.pi / 4
                c.move_to(cx + math.cos(a) * (r + 4), cy + math.sin(a) * (r + 4))
                c.line_to(cx + math.cos(a) * (r + (7 if name == "bright_up" else 5)),
                          cy + math.sin(a) * (r + (7 if name == "bright_up" else 5)))
            c.stroke()
        elif name in ("kbd_dn", "kbd_up"):
            self._rrect(c, cx - 14, cy - 7, 28, 14, 3)
            c.stroke()
            for i in range(4):
                c.rectangle(cx - 10 + i * 6, cy - 3, 3, 2)
            c.fill()
            c.move_to(cx - 4, cy + 13)
            c.line_to(cx + 4, cy + 13)
            c.stroke()
        elif name in ("vol_dn", "vol_up", "mute"):
            c.move_to(cx - 12, cy - 4)
            c.line_to(cx - 6, cy - 4)
            c.line_to(cx + 1, cy - 10)
            c.line_to(cx + 1, cy + 10)
            c.line_to(cx - 6, cy + 4)
            c.line_to(cx - 12, cy + 4)
            c.close_path()
            c.fill()
            if name == "mute":
                c.move_to(cx + 6, cy - 6)
                c.line_to(cx + 16, cy + 6)
                c.move_to(cx + 16, cy - 6)
                c.line_to(cx + 6, cy + 6)
                c.stroke()
            else:
                for i in range(1 if name == "vol_dn" else 2):
                    c.new_sub_path()
                    c.arc(cx + 2, cy, 7 + i * 5, -0.8, 0.8)
                    c.stroke()
        elif name == "playpause":
            c.move_to(cx - 10, cy - 9)
            c.line_to(cx - 10, cy + 9)
            c.line_to(cx + 1, cy)
            c.close_path()
            c.fill()
            c.rectangle(cx + 5, cy - 9, 3.5, 18)
            c.rectangle(cx + 11, cy - 9, 3.5, 18)
            c.fill()
        elif name in ("prev", "next"):
            d = -1 if name == "prev" else 1
            for off in (-7, 5):
                c.move_to(cx + d * (off + 7), cy - 9)
                c.line_to(cx + d * (off + 7), cy + 9)
                c.line_to(cx + d * off, cy)
                c.close_path()
                c.fill()

    def _spinner(self, c, cx, cy, col, now):
        a = (now * 6.0) % (2 * math.pi)
        c.set_source_rgb(*col)
        c.set_line_width(3)
        c.set_line_cap(cairo.LINE_CAP_ROUND)
        c.new_sub_path()
        c.arc(cx, cy, 9, a, a + 4.2)
        c.stroke()

    # ------------------------------------------------------------ regions

    def _add(self, x0, x1, id, kind, hold_ms=0, key=None):
        self.regions.append(Region(x0, x1, id, kind, hold_ms, key))

    def _key(self, c, x, w, name, st, label=None):
        prog = st.holding[1] if st.holding and st.holding[0] == "key:" + name else 0
        icon = name if name in ("bright_dn", "bright_up", "kbd_dn", "kbd_up", "vol_dn", "vol_up",
                                "mute", "playpause", "prev", "next") else None
        self._button(c, x, w, label or ("esc" if name == "ESC" else name), icon=icon, size=20)
        self._add(x, x + w, "key:" + name, "key", key=name)

    def hit(self, x):
        for r in self.regions:
            if r.x0 - 4 <= x < r.x1 + 4:
                return r
        return None

    # ------------------------------------------------------------ rows

    def _fkeys_row(self, c, st, x0, x1):
        gap = 8
        n = len(FN_KEYS)
        w = (x1 - x0 - gap * (n - 1)) / n
        for i, k in enumerate(FN_KEYS):
            self._key(c, x0 + i * (w + gap), w, k, st, label=k)

    def _media_row(self, c, st, x0, x1):
        gap = 8
        w = min(120, (x1 - x0 - gap * (len(MEDIA_ROW) - 1)) / len(MEDIA_ROW))
        total = w * len(MEDIA_ROW) + gap * (len(MEDIA_ROW) - 1)
        x = x0 + (x1 - x0 - total) / 2
        for k in MEDIA_ROW:
            self._key(c, x, w, k, st)
            x += w + gap

    def _strip(self, c, st, x1):
        """Right-hand control strip; returns the x where it starts."""
        keys = [k for name in self.strip for k in STRIP.get(name, [])]
        w, gap = 70, 8
        x = x1 - (w * len(keys) + gap * len(keys))
        x0 = x
        for k in keys:
            self._key(c, x, w, k, st)
            x += w + gap
        return x0 - 6

    def _mascot(self, c, x, pw=4, ph=8):
        """Draws MASCOT with its left edge at x, vertically centred. Returns its width.

        Block-art pixels are half a terminal cell each way, so they are about twice as tall as wide.
        """
        y0 = round((self.h - len(MASCOT) * ph) / 2)
        c.set_source_rgb(*CLAUDE_ORANGE)
        for r, row in enumerate(MASCOT):
            for col, ch in enumerate(row):
                if ch == "#":
                    c.rectangle(x + col * pw, y0 + r * ph, pw, ph)
        c.fill()
        return len(MASCOT[0]) * pw

    def _chip(self, c, x, lay, st, icon_txt=None):
        chip = lay["chip"]
        # The mascot stands for the session; no project label (the +N badge still counts the others).
        label = icon_txt or ""
        mw = len(MASCOT[0]) * 4 + 10
        w = max(110, min(320, mw + (self._text_w(c, label, 22, True) + 8 if label else 0) + 30
                         + (46 if chip["waiting"] else 0)))
        col = STATE_COLOR.get(lay["state"], GREY)
        self._rrect(c, x, 7, w, self.h - 14)
        c.set_source_rgb(0.14, 0.14, 0.17)
        c.fill()
        self._rrect(c, x, 7, 5, self.h - 14, 2)
        c.set_source_rgb(*col)
        c.fill()
        tw = self._mascot(c, x + 16) + 8
        if label:
            tw += self._text(c, label, x + 16 + tw, w - 24 - tw - (46 if chip["waiting"] else 0),
                             WHITE, 22, bold=True)
        if chip["waiting"]:
            pulse = 0.65 + 0.35 * math.sin(st.now * 5)
            bx = x + 16 + tw
            self._rrect(c, bx, 17, 38, self.h - 34, 8)
            c.set_source_rgba(*AMBER, pulse)
            c.fill()
            self._text(c, "+%d" % chip["waiting"], bx, 38, (0, 0, 0), 18, "center", True)
        self._add(x, x + w, "chip", "button")
        return x + w + 8

    # ------------------------------------------------------------ claude region

    @staticmethod
    def _fmt_timer(start, now):
        s = max(0, int(now - start))
        return "%d:%02d" % (s // 60, s % 60)

    @staticmethod
    def _fmt_reset(v, now, weekly):
        """statusLine resets_at (epoch s/ms or ISO 8601) → "2h 53m" (session) / "R:Fri 3pm" (weekly)."""
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            t = v / 1000 if v > 1e12 else float(v)
        elif isinstance(v, str):
            try:
                t = datetime.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return ""
        else:
            return ""
        if t <= now:
            return ""
        if not weekly:
            mins = int(t - now) // 60
            return "%dh %02dm" % divmod(mins, 60) if mins >= 60 else "%dm" % mins
        lt = time.localtime(t + 1800)          # nearest hour, like claude-pulse's "R:Fri 3pm"
        hour = "%d%s" % (lt.tm_hour % 12 or 12, "am" if lt.tm_hour < 12 else "pm")
        return "R:%s %s" % (time.strftime("%a", lt), hour)

    def _pulse_segments(self, m, lay, now, thr, alpha):
        segs = []

        def meter_seg(key, label, reset_key=None, weekly=False, mw=130):
            if key not in m:
                return
            reset = self._fmt_reset(m.get(reset_key), now, weekly) if reset_key else ""
            lw = 76 if len(label) > 3 else 42
            w = lw + mw + 64 + (104 if reset else 0)

            def draw(c, x):
                self._text(c, label, x, lw, GREY, 18, alpha=alpha)
                self._meter(c, x + lw, mw, m[key], thr, alpha)
                self._text(c, "%d%%" % round(m[key]), x + lw + mw + 8, 56,
                           self._level_color(m[key], thr), 19, bold=True, alpha=alpha)
                if reset:
                    self._text(c, reset, x + lw + mw + 64, 104, GREY, 17, alpha=alpha)
            segs.append((key, w, draw))

        meter_seg("five_h", "Session", "five_h_reset")
        meter_seg("seven_d", "Weekly", "seven_d_reset", weekly=True)
        meter_seg("ctx", "Context", mw=100)
        if "cost" in m:
            segs.append(("cost", 90, lambda c, x: self._text(
                c, "$%.2f" % m["cost"], x, 86, GREEN, 19, alpha=alpha)))
        if m.get("plan"):
            segs.append(("plan", 90, lambda c, x: self._text(c, m["plan"], x, 86, WHITE, 18, alpha=alpha)))
        info = "·".join(str(v) for v in (m.get("model"), m.get("effort"), lay.get("branch")) if v)
        if info:
            segs.append(("model", 240, lambda c, x: self._text(c, info, x, 236, GREY, 18, alpha=alpha)))
        return segs

    def _metric_segments(self, lay, now):
        """[(id, width, draw(c, x))] in display order, before fitting."""
        m = lay.get("metrics") or {}
        thr = lay.get("thresholds", {"warn": 70, "crit": 90})
        stale = lay.get("metrics_at") is not None and now - lay["metrics_at"] > 60
        alpha = 0.45 if stale else 1.0
        if self.style == "pulse":
            return self._pulse_segments(m, lay, now, thr, alpha)
        segs = []

        def pct_seg(key, label, w):
            if key in m:
                def draw(c, x, key=key, label=label, w=w):
                    self._text(c, label, x, 44, GREY, 19, alpha=alpha)
                    self._bar(c, x + 46, self.h / 2 - 5, w - 120, 10, m[key], thr, alpha)
                    self._text(c, "%d%%" % round(m[key]), x + w - 66, 62, WHITE, 19, "right", alpha=alpha)
                segs.append((key, w, draw))
        pct_seg("ctx", "ctx", 210)
        pct_seg("five_h", "5h", 190)
        if "seven_d" in m:
            segs.append(("seven_d", 120, lambda c, x: self._text(
                c, "7d %d%%" % round(m["seven_d"]), x, 110, WHITE, 19, alpha=alpha)))
        if "cost" in m:
            segs.append(("cost", 100, lambda c, x: self._text(
                c, "$%.2f" % m["cost"], x, 92, WHITE, 19, alpha=alpha)))
        info = "·".join(str(v) for v in (m.get("model"), m.get("effort"), lay.get("branch")) if v)
        if info:
            segs.append(("model", 260, lambda c, x: self._text(c, info, x, 250, GREY, 19, alpha=alpha)))
        return segs

    def _activity(self, c, x, w, lay, st):
        col = STATE_COLOR.get(lay["state"], GREY)
        if lay["state"] == "working":
            self._spinner(c, x + 12, self.h / 2, col, st.now)
            x, w = x + 34, w - 34
        right = ""
        if lay.get("timer_start"):
            right = self._fmt_timer(lay["timer_start"], st.now)
        if lay.get("subagents"):
            right += "  sub×%d" % lay["subagents"]
        rw = self._text_w(c, right, 20) + 12 if right else 0
        text = lay.get("activity") or ("Idle" if lay["state"] == "idle" else "")
        if lay.get("compacting"):
            text = "Compacting · " + text
        self._text(c, text, x, w - rw, col, 22)
        if right:
            self._text(c, right, x, w, GREY, 20, "right")

    def _claude_region(self, c, st, x0, x1):
        lay = st.layout
        mode = lay["mode"]
        if mode in ("permission", "plan", "question"):
            return self._prompt_region(c, st, x0, x1)
        if mode == "terminal":
            x = self._chip(c, x0, lay, st)
            self._text(c, "Waiting in terminal", x + 8, x1 - x, AMBER, 22, bold=True)
            return
        x = self._chip(c, x0, lay, st)
        avail = x1 - x
        segs, used = [], 0
        for seg in self._metric_segments(lay, st.now):  # priority order == display order
            need = seg[1] + 12
            if used + need <= avail - 420:
                segs.append(seg)
                used += need
        self._activity(c, x + 8, avail - used - 28, lay, st)
        sx = x1 - used + 6
        for i, (_id, w, draw) in enumerate(segs):
            if self.style == "pulse" and i:        # claude-pulse's │ separators
                c.rectangle(sx - 7, 16, 1.5, self.h - 32)
                c.set_source_rgba(1, 1, 1, 0.22)
                c.fill()
            draw(c, sx)
            sx += w + 12

    def _prompt_region(self, c, st, x0, x1):
        lay = st.layout
        p = lay["prompt"]
        mode = lay["mode"]
        icon = {"permission": "!", "plan": "P", "question": "?"}[mode]
        x = self._chip(c, x0, lay, st, icon)
        buttons = [b for b in lay["buttons"] if b["id"] != "chip"]
        bw = {"deny": 170, "allow": 210, "always": 170, "reject": 170, "approve": 230, "terminal": 160}
        bx = x1 - sum(bw.get(b["id"], 160) + 8 for b in buttons)
        region_right = bx - 8
        if mode == "question":
            opts = p.get("options", [])
            ow = [min(180, self._text_w(c, o, 20) + 36) for o in opts]
            ox = region_right - sum(w + 8 for w in ow)
            for o, w in zip(opts, ow):   # read-only (answering happens in the terminal in v1)
                self._rrect(c, ox, 11, w, self.h - 22, 8)
                c.set_source_rgba(1, 1, 1, 0.10)
                c.fill()
                self._text(c, o, ox + 6, w - 12, GREY, 20, "center")
                ox += w + 8
            text = (p.get("pager") + " " if p.get("pager") else "") + p["summary"]
            self._text(c, text, x + 8, ox - x - 16, AMBER, 22, bold=True)
        else:
            text = ("Plan ready: " if mode == "plan" else "") + p["summary"]
            col = RED if p.get("dangerous") else AMBER
            self._text(c, text, x + 8, region_right - x - 8, col, 22, bold=True)
        for b in buttons:
            w = bw.get(b["id"], 160)
            prog = st.holding[1] if st.holding and st.holding[0] == b["id"] else 0.0
            bg = {"danger": (0.42, 0.14, 0.14), "primary": (0.13, 0.42, 0.22)}.get(b.get("style"), KEY_BG)
            self._button(c, bx, w, b["label"], bg=bg, progress=prog, size=23, bold=True)
            self._add(bx, bx + w, b["id"], "button", b["hold_ms"])
            bx += w + 8

    # ------------------------------------------------------------ frame

    def render(self, st):
        """→ (RGB24 bytes, regions)"""
        self.regions = []
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, self.w, self.h)
        c = cairo.Context(surf)
        c.set_source_rgb(0, 0, 0)
        c.paint()
        if st.brightness > 0:
            self._draw(c, st)
            if st.brightness < 1:
                c.set_source_rgba(0, 0, 0, 1 - st.brightness)
                c.paint()
        return self._to_rgb(surf), self.regions

    def _draw(self, c, st):
        M = 8
        c.set_operator(cairo.OPERATOR_OVER)
        lay = st.layout
        if st.fn_held or (lay and lay["mode"] == "none" and self.default_layer == "fkeys"):
            self._key(c, M, 90, "ESC", st)
            self._fkeys_row(c, st, M + 90 + M, self.w - M)
            return
        if lay and lay["mode"] == "none":
            self._key(c, M, 90, "ESC", st)
            self._media_row(c, st, M + 90 + M, self.w - M)
            return
        self._key(c, M, 90, "ESC", st)
        x = M + 90 + M
        prompt = lay is not None and lay["mode"] in ("permission", "plan", "question")
        x1 = self.w - M if prompt else self._strip(c, st, self.w - M)
        if lay is None:
            self._text(c, "Claude: offline", x + 8, x1 - x, GREY, 22, alpha=0.6)
            return
        self._claude_region(c, st, x, x1)

    @staticmethod
    def _to_rgb(surf):
        from PIL import Image
        surf.flush()
        img = Image.frombuffer("RGBA", (surf.get_width(), surf.get_height()),
                               bytes(surf.get_data()), "raw", "BGRA", surf.get_stride(), 1)
        return img.convert("RGB").tobytes()
