"""End-to-end check against a REAL Claude Code (one small model call, temp settings only).

Runs the bridge with a scripted fake bar that holds 'Allow' on the first permission prompt, points
`claude -p` at our hooks through --settings, and verifies the command ran and the states flowed.
usage: tools/e2e_claude.py [allow|deny]
"""
import asyncio, json, os, subprocess, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from ctb import config
from ctb.bridge import Bridge
from ctb.install import EVENTS, TOOL_EVENTS

mode = sys.argv[1] if len(sys.argv) > 1 else "allow"


async def main():
    tmp = tempfile.mkdtemp(prefix="ctb_e2e_")
    os.environ["CTB_RUNTIME_DIR"] = tmp
    target = os.path.join(tmp, "made_by_claude.txt")
    hooks = {}
    for ev in EVENTS:
        e = {"hooks": [{"type": "command", "command": os.path.join(ROOT, "bin", "ctb-hook"),
                        "timeout": 120 if ev == "PermissionRequest" else 5}]}
        hooks[ev] = [({"matcher": "*", **e} if ev in TOOL_EVENTS else e)]
    settings = os.path.join(tmp, "settings.json")
    json.dump({"hooks": hooks}, open(settings, "w"))

    cfg = config.load("/nonexistent")
    cfg["fallback"]["notifications"] = False
    bridge = Bridge(cfg)
    await bridge.start()
    log = []

    async def fake_bar():
        r, w = await asyncio.open_unix_connection(config.render_sock())
        w.write(b'{"type":"hello"}\n')
        done = set()
        while True:
            line = await r.readline()
            if not line:
                return
            lay = json.loads(line)
            log.append((lay["mode"], lay.get("state"), lay.get("activity"), (lay.get("prompt") or {}).get("summary")))
            p = lay.get("prompt")
            if p and p["id"] not in done:
                done.add(p["id"])
                await asyncio.sleep(0.6)   # let the 300 ms guard pass, like a human would
                btn = "allow" if mode == "allow" else "deny"
                hold = next(b["hold_ms"] for b in lay["buttons"] if b["id"] == btn)
                w.write((json.dumps({"type": "tap", "seq": lay["seq"], "button": btn, "held_ms": hold}) + "\n").encode())

    bar = asyncio.ensure_future(fake_bar())
    await asyncio.sleep(0.2)
    cmd = ["claude", "-p", "Run exactly this shell command with the Bash tool and then say done: touch %s" % target,
           "--settings", settings, "--model", "claude-haiku-4-5-20251001", "--max-turns", "4"]
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=tmp, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 150)
    except asyncio.TimeoutError:
        proc.kill(); out = b"(timeout)"
    bar.cancel()
    await bridge.stop()
    print("claude output:", out.decode().strip()[:400])
    print("file created :", os.path.exists(target), "(expected %s)" % (mode == "allow"))
    print("state flow   :")
    last = None
    for row in log:
        if row != last:
            print("   ", row)
        last = row

asyncio.run(main())
