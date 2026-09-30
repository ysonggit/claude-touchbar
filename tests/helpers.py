import asyncio
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ctb import config  # noqa: E402
from ctb.bridge import Bridge  # noqa: E402


class FakeNotifier:
    def __init__(self):
        self.started, self.stopped = [], []

    def start(self, prompt, label):
        self.started.append((prompt.id, label))
        return object()

    def stop(self, prompt):
        self.stopped.append(prompt.id)


class FakeBar:
    """Stands in for ctb-bar: connects to the render socket, keeps the latest layout."""

    def __init__(self, path):
        self.path, self.layout, self.layouts = path, {}, []

    async def connect(self):
        self.r, self.w = await asyncio.open_unix_connection(self.path)
        self.w.write(b'{"type":"hello","width":2170,"height":60,"backend":"fake"}\n')
        self._task = asyncio.ensure_future(self._read())
        await self.settle()

    async def _read(self):
        while True:
            line = await self.r.readline()
            if not line:
                return
            self.layout = json.loads(line)
            self.layouts.append(self.layout)

    async def settle(self, t=0.05):
        await asyncio.sleep(t)

    async def tap(self, button, held_ms=0, seq=None):
        self.w.write((json.dumps({"type": "tap", "seq": self.layout["seq"] if seq is None else seq,
                                  "button": button, "held_ms": held_ms}) + "\n").encode())
        await self.settle()

    async def close(self):
        self._task.cancel()
        self.w.close()


class Env:
    """Bridge + sockets in a temp runtime dir."""

    def __init__(self, **prompt_overrides):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["CTB_RUNTIME_DIR"] = self.tmp.name
        cfg = config.load("/nonexistent")
        cfg["prompts"].update(prompt_overrides)
        self.notifier = FakeNotifier()
        self.bridge = Bridge(cfg, notifier=self.notifier)

    async def __aenter__(self):
        await self.bridge.start()
        return self

    async def __aexit__(self, *a):
        await self.bridge.stop()
        self.tmp.cleanup()

    @property
    def render_path(self):
        return config.render_sock()

    async def bar(self):
        b = FakeBar(self.render_path)
        await b.connect()
        return b

    async def run_hook(self, event):
        env = dict(os.environ, CTB_RUNTIME_DIR=self.tmp.name)
        p = await asyncio.create_subprocess_exec(
            os.path.join(ROOT, "bin", "ctb-hook"), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, env=env)
        p.stdin.write(json.dumps(event).encode())
        p.stdin.close()
        return p

    def event(self, name, **kw):
        d = {"hook_event_name": name, "session_id": "s1", "cwd": "/home/u/ARX"}
        d.update(kw)
        return d

    def send(self, name, **kw):
        self.bridge.handle_event(self.event(name, **kw))
