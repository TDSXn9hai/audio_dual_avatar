from __future__ import annotations

import asyncio
import struct
import threading
import time
from argparse import Namespace
from types import SimpleNamespace

import numpy as np
import pytest

from muse_stream.domain import LiveSegment
from muse_stream.inference import workers
from muse_stream.protocol import FRAME_HEADER, FRAME_SILENT
from muse_stream.state import AppState


class EventLoopThread:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()
        self.loop.close()

    def submit(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop)

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)


async def _new_queue(maxsize: int) -> asyncio.Queue:
    return asyncio.Queue(maxsize=maxsize)


def test_parallel_encoding_preserves_frame_and_control_order(monkeypatch) -> None:
    monkeypatch.setenv("AVATAR_ENCODE_THREADS", "4")
    monkeypatch.setenv("AVATAR_STREAM_FORMAT", "webp")
    monkeypatch.setenv("AVATAR_STREAM_MAX_WIDTH", "0")

    active = 0
    max_active = 0
    active_lock = threading.Lock()

    def fake_imencode(extension, frame, params):
        nonlocal active, max_active
        with active_lock:
            active += 1
            max_active = max(max_active, active)
        try:
            # Different completion times force the thread pool to finish out
            # of submission order.
            time.sleep(0.02 - int(frame[0, 0, 0]) * 0.001)
            marker = struct.pack(">I", int(frame[0, 0, 0]))
            return True, np.frombuffer(marker, dtype=np.uint8)
        finally:
            with active_lock:
                active -= 1

    monkeypatch.setattr(workers.cv2, "imencode", fake_imencode)

    loop_thread = EventLoopThread()
    raw_queue = loop_thread.submit(_new_queue(64)).result(timeout=2)
    network_queue = loop_thread.submit(_new_queue(64)).result(timeout=2)
    stop = threading.Event()
    state = AppState(
        args=Namespace(jpeg_quality=88),
    )
    segment = LiveSegment(
        id=7,
        source_path=None,
        wav_path=None,
        duration=0.5,
        total_frames=12,
        global_start_frame=0,
        speech_frames=12,
    )
    encoder_thread = threading.Thread(
        target=workers.live_encode_worker,
        args=(
            state,
            raw_queue,
            network_queue,
            loop_thread.loop,
            stop,
        ),
        daemon=True,
    )

    try:
        encoder_thread.start()
        loop_thread.submit(raw_queue.put(("segment", segment))).result(timeout=2)
        frame_indices = list(range(100, 112))
        for index in frame_indices:
            frame = np.full((8, 8, 3), index - 100, dtype=np.uint8)
            loop_thread.submit(
                raw_queue.put(
                    (
                        "raw",
                        FRAME_SILENT,
                        segment.id,
                        index * 1000,
                        index,
                        frame,
                    )
                )
            ).result(timeout=2)
        loop_thread.submit(
            raw_queue.put(("segment_end", segment.id))
        ).result(timeout=2)

        received = []
        while True:
            item = loop_thread.submit(network_queue.get()).result(timeout=5)
            received.append(item)
            if item[0] == "segment_end":
                break
            if item[0] == "error":
                pytest.fail(item[1])

        assert received[0][0] == "segment"
        assert received[-1] == ("segment_end", segment.id)
        frames = received[1:-1]
        assert len(frames) == len(frame_indices)
        assert [item[4] for item in frames] == frame_indices
        assert [
            FRAME_HEADER.unpack_from(item[5])[4]
            for item in frames
        ] == frame_indices
        assert max_active >= 2
    finally:
        stop.set()
        encoder_thread.join(timeout=5)
        loop_thread.close()

    assert not encoder_thread.is_alive()
