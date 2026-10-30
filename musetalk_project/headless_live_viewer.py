"""Headless replacement for web/app.js used to run simulate_pcm_interrupt_flow.py
without a browser.

It connects /ws/live, sends "ready" (进入直播), ACKs frames paced to wall clock
like the browser render loop, and sends "interrupt" (打断) whenever the
simulator prints an ACTION prompt asking to click it.
"""
import argparse
import asyncio
import json
import struct
import sys
import time

import websockets

FRAME_HEADER = struct.Struct(">4sB3xIqI")
KIND_NAME = {0: "idle", 2: "silent"}


class Viewer:
    def __init__(self, url: str) -> None:
        self.url = url
        self.ws = None
        self.fps = 24.0
        self.epoch = None
        self.segments = {}
        self.ignored = set()
        self.ignore_normal = False
        self.frames = {}  # frame_index -> (kind, segment_id)
        self.last_ack = -1
        self.last_media_ack = -1
        self.last_drawn = -1
        self.stats = {"frames": 0, "acks": 0, "interrupts": []}
        self.closed = asyncio.Event()

    def log(self, msg: str) -> None:
        print(f"[VIEWER] {msg}", flush=True)

    async def run(self) -> None:
        async with websockets.connect(self.url, max_size=None) as ws:
            self.ws = ws
            meta = json.loads(await ws.recv())
            if meta.get("type") != "meta":
                raise RuntimeError(f"unexpected first message: {meta}")
            self.fps = float(meta["fps"])
            await ws.send(json.dumps({"type": "ready"}))
            self.epoch = time.monotonic() + 0.12
            self.log(f"进入直播 fps={self.fps} {meta['width']}x{meta['height']}")
            render = asyncio.create_task(self.render_loop())
            try:
                async for message in ws:
                    if isinstance(message, bytes):
                        self.on_frame(message)
                    else:
                        self.on_json(json.loads(message))
            except websockets.ConnectionClosed:
                pass
            finally:
                render.cancel()
                self.closed.set()
                self.log(f"连接关闭 stats={self.stats}")

    def on_json(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "segment":
            seg_id = msg["id"]
            if self.ignore_normal and not (msg.get("priority") or msg.get("resume")):
                self.ignored.add(seg_id)
                return
            self.ignore_normal = False
            self.segments[seg_id] = msg
            self.log(
                f"segment id={seg_id} start={msg['start_frame']} frames={msg['total_frames']} "
                f"priority={msg.get('priority')} resume={msg.get('resume')}"
            )
        elif kind == "interrupt_ack":
            self.log(f"interrupt_ack {msg}")
            if msg.get("ok"):
                self.ignore_normal = True
                frame = int(msg.get("frame", self.last_drawn))
                self.frames = {k: v for k, v in self.frames.items() if k <= frame}
                self.last_ack = self.last_media_ack = frame
        elif kind == "flush":
            self.log(f"flush {msg}")
            self.stats.setdefault("flushes", []).append(msg.get("frame_index"))
            frame = max(self.last_drawn, int(msg.get("frame_index", -1)))
            self.frames = {k: v for k, v in self.frames.items() if k <= frame}
        elif kind in ("live_end", "error"):
            self.log(f"{kind} {msg}")
        elif kind != "segment_end":
            self.log(f"msg {msg}")

    def on_frame(self, data: bytes) -> None:
        magic, kind, seg_id, _pts, index = FRAME_HEADER.unpack_from(data)
        if magic != b"MTF1" or seg_id in self.ignored:
            return
        self.frames[index] = (kind, seg_id)
        self.stats["frames"] += 1

    async def render_loop(self) -> None:
        while True:
            await asyncio.sleep(0.5 / self.fps)
            target = int((time.monotonic() - self.epoch) * self.fps)
            ready = [i for i in self.frames if i <= target]
            if not ready:
                continue
            drawn = max(ready)
            kind, seg_id = self.frames[drawn]
            # Drop frames that are already in the past.
            for i in ready:
                if i < drawn:
                    del self.frames[i]
            if drawn == self.last_drawn:
                continue
            self.last_drawn = drawn
            seg = self.segments.get(seg_id)
            is_final = seg is not None and drawn == seg["start_frame"] + seg["total_frames"] - 1
            send = False
            if kind != 0 and (drawn - self.last_media_ack >= 6 or is_final):
                self.last_media_ack = drawn
                send = True
            elif drawn - self.last_ack >= 6:
                send = True
            if send:
                self.last_ack = drawn
                await self.ws.send(json.dumps({
                    "type": "ack", "frame_index": drawn,
                    "pts_us": round(drawn * 1e6 / self.fps),
                    "kind": KIND_NAME.get(kind, "speech"),
                }))
                self.stats["acks"] += 1

    def active_segment(self):
        for seg in self.segments.values():
            if seg["start_frame"] <= self.last_drawn < seg["start_frame"] + seg["total_frames"]:
                return seg
        return None

    async def interrupt(self, delay: float) -> None:
        await asyncio.sleep(delay)
        for _ in range(100):
            seg = self.active_segment()
            if seg is not None:
                frame = max(self.last_drawn, seg["start_frame"])
                self.log(f"打断 segment={seg['id']} frame={frame}")
                self.stats["interrupts"].append({"segment_id": seg["id"], "frame": frame})
                await self.ws.send(json.dumps(
                    {"type": "interrupt", "segment_id": seg["id"], "frame_index": frame}
                ))
                return
            await asyncio.sleep(0.1)
        self.log("没有正在播放的 segment，无法打断")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="127.0.0.1:8000")
    parser.add_argument("--first-delay", type=float, default=0.5)
    parser.add_argument("--second-delay", type=float, default=2.0)
    parser.add_argument("simulator_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    viewer = Viewer(f"ws://{args.base}/ws/live")
    # The simulator only needs the server to be ready; wait for that first.
    import urllib.request
    while True:
        try:
            with urllib.request.urlopen(f"http://{args.base}/api/status", timeout=3) as r:
                if json.load(r).get("ready"):
                    break
        except OSError:
            pass
        await asyncio.sleep(1)
    viewer_task = asyncio.create_task(viewer.run())

    sim = await asyncio.create_subprocess_exec(
        sys.executable, "-u", "simulate_pcm_interrupt_flow.py",
        "--base-url", f"http://{args.base}", *args.simulator_args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    clicks = 0
    async for raw in sim.stdout:
        line = raw.decode(errors="replace").rstrip()
        print(line, flush=True)
        if "[ACTION]" in line and "点击“打断”" in line:
            clicks += 1
            delay = args.first_delay if clicks == 1 else args.second_delay
            asyncio.create_task(viewer.interrupt(delay))
    code = await sim.wait()
    try:
        await asyncio.wait_for(viewer.closed.wait(), timeout=30)
    except asyncio.TimeoutError:
        viewer_task.cancel()
    print(f"[VIEWER] simulator exit={code} clicks={clicks}", flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
