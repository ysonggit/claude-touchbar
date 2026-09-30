"""Render every bar mode to PNG (dev aid; no hardware needed).  usage: tools/preview.py OUT.png"""
import os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from PIL import Image
from ctb.bar.render import Renderer, BarState

now = time.time()
thr = {"warn": 70, "crit": 90}
base = {"type": "layout", "seq": 1, "sessions": 2, "chip": {"label": "ARX", "waiting": 0}, "state": "working",
        "activity": "Bash: cargo test --release", "timer_start": now - 102, "subagents": 2, "compacting": False,
        "metrics": {"ctx": 62, "five_h": 41, "seven_d": 78, "cost": 1.24, "model": "Opus", "effort": "high",
                    "five_h_reset": now + 2 * 3600 + 53 * 60, "seven_d_reset": now + 3 * 86400},
        "metrics_at": now, "branch": "main", "thresholds": thr, "attention": False, "mode": "status",
        "buttons": [{"id": "chip", "hold_ms": 0}]}
def L(**kw): d = dict(base); d.update(kw); return d
perm_btn = [{"id": "deny", "label": "Deny", "style": "danger", "hold_ms": 0},
            {"id": "allow", "label": "Allow", "style": "primary", "hold_ms": 400},
            {"id": "always", "label": "Always", "style": "plain", "hold_ms": 400}]
rows = [
 ("status", BarState(L(), now=now)),
 ("status (classic)", BarState(L(), now=now)),
 ("status high usage", BarState(L(metrics={"ctx": 91, "five_h": 88, "seven_d": 96, "cost": 7.9, "model": "Opus",
    "five_h_reset": now + 24 * 60, "seven_d_reset": now + 86400}), now=now)),
 ("status+waiting", BarState(L(chip={"label": "ARX", "waiting": 2}, state="input", activity="Needs input"), now=now)),
 ("status stale/partial", BarState(L(metrics={"ctx": 93, "five_h": 75}, metrics_at=now - 120, state="done", activity="Done", timer_start=None, subagents=0), now=now)),
 ("idle", BarState(L(state="idle", activity="", timer_start=None, subagents=0, metrics={}), now=now)),
 ("permission", BarState(L(mode="permission", state="input", attention=True, buttons=perm_btn + [{"id": "chip", "hold_ms": 0}],
    prompt={"id": "p1", "tool": "Bash", "summary": "Bash: cargo build --release && cargo test", "dangerous": False, "index": 1, "total": 1}), holding=("allow", 0.55), now=now)),
 ("permission dangerous", BarState(L(mode="permission", state="input", chip={"label": "ARX", "waiting": 1}, buttons=perm_btn[:2] + [{"id": "chip", "hold_ms": 0}],
    prompt={"id": "p1", "tool": "Bash", "summary": "Bash: rm -rf build/ && cargo build", "dangerous": True, "index": 1, "total": 2}), now=now)),
 ("plan", BarState(L(mode="plan", state="input", buttons=[{"id": "reject", "label": "Reject", "style": "danger", "hold_ms": 0}, {"id": "approve", "label": "Approve", "style": "primary", "hold_ms": 400}, {"id": "chip", "hold_ms": 0}],
    prompt={"id": "p2", "tool": "ExitPlanMode", "summary": "Write SPEC.md for Touch Bar", "dangerous": False, "index": 1, "total": 1}), now=now)),
 ("question", BarState(L(mode="question", state="input", buttons=[{"id": "terminal", "label": "Terminal", "style": "plain", "hold_ms": 0}, {"id": "chip", "hold_ms": 0}],
    prompt={"id": "p3", "tool": "AskUserQuestion", "summary": "Which OS drives the Touch Bar?", "dangerous": False, "index": 1, "total": 1, "options": ["macOS", "T2 Linux", "Both"], "pager": "1/3"}), now=now)),
 ("terminal", BarState(L(mode="terminal", state="input", activity="Waiting in terminal"), now=now)),
 ("offline", BarState(None, now=now)),
 ("no sessions (media)", BarState({"type": "layout", "mode": "none", "sessions": 0, "buttons": [], "attention": False}, now=now)),
 ("Fn held", BarState(L(), fn_held=True, now=now)),
 ("dimmed 30%", BarState(L(), brightness=0.3, now=now)),
]
r, classic = Renderer(), Renderer(style="classic")
out = Image.new("RGB", (r.w, (r.h + 6) * len(rows)), (40, 40, 40))
for i, (name, st) in enumerate(rows):
    buf, regions = (classic if name.endswith("(classic)") else r).render(st)
    out.paste(Image.frombytes("RGB", (r.w, r.h), buf), (0, i * (r.h + 6)))
    print("%-22s regions=%s" % (name, ",".join(x.id for x in regions if not x.id.startswith("key:")) or "-"))
out.save(sys.argv[1] if len(sys.argv) > 1 else "/tmp/preview.png")
