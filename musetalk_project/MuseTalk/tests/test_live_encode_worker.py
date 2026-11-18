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
from muse_stream.api.websocket import LiveStreamSession
from muse_stream.inference import workers
from muse_stream.protocol import (
    FRAME_HEADER,
    FRAME_GENERATED,
    FRAME_SILENT,
    H264_FRAME_HEADER,
    H264_FRAME_MAGIC,
    VIDEO_FLAG_KEYFRAME,
)
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
        async def drain():
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        self.submit(drain()).result(timeout=2)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)


async def _new_queue(maxsize: int) -> asyncio.Queue:
    return asyncio.Queue(maxsize=maxsize)


@pytest.mark.parametrize("change", ["queued_epoch", "queued_avatar", "inflight_avatar"])
@pytest.mark.parametrize("frames_restored", [False, True])
def test_fusion_rejects_stale_restoration_work(monkeypatch, change, frames_restored):
    state = AppState(args=Namespace())
    state.pipeline.flush_seq = 1
    state.model.avatar_revision = 1
    calls = []

    def blend(self, index, frame):
        calls.append(index)
        if change == "inflight_avatar":
            state.model.avatar_revision += 1
        return frame

    monkeypatch.setattr(workers.InferencePipeline, "composite_frame" if frames_restored else "blend_frame", blend)
    loop_thread = EventLoopThread()
    raw = loop_thread.submit(_new_queue(8)).result(timeout=2)
    fused = loop_thread.submit(_new_queue(8)).result(timeout=2)
    stop = threading.Event()
    thread = threading.Thread(
        target=workers.live_fusion_worker,
        args=(state, raw, fused, loop_thread.loop, stop),
        kwargs={"frames_restored": frames_restored}, daemon=True)
    try:
        thread.start()
        epoch = 0 if change == "queued_epoch" else 1
        revision = 0 if change == "queued_avatar" else 1
        loop_thread.submit(raw.put((
            "raw", FRAME_GENERATED, 99, 123456, 3,
            np.zeros((8, 8, 3), np.uint8), epoch, revision,
        ))).result(timeout=2)
        loop_thread.submit(raw.put(("segment_end", 99))).result(timeout=2)
        item = loop_thread.submit(fused.get()).result(timeout=3)
        assert item == ("segment_end", 99)
        assert calls == ([3] if change == "inflight_avatar" else [])
    finally:
        stop.set()
        thread.join(timeout=3)
        loop_thread.close()
    assert not thread.is_alive()


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
        assert all(item[6] == state.pipeline.flush_seq for item in frames)
        assert max_active >= 2
    finally:
        stop.set()
        encoder_thread.join(timeout=5)
        loop_thread.close()

    assert not encoder_thread.is_alive()


def test_h264_encoding_preserves_packets_and_keyframe_flags(monkeypatch) -> None:
    monkeypatch.setenv("AVATAR_STREAM_FORMAT", "h264")
    monkeypatch.setenv("AVATAR_H264_GOP", "6")

    loop_thread = EventLoopThread()
    raw_queue = loop_thread.submit(_new_queue(64)).result(timeout=2)
    network_queue = loop_thread.submit(_new_queue(64)).result(timeout=2)
    stop = threading.Event()
    state = AppState(
        args=Namespace(jpeg_quality=88, fps=24),
    )
    state.model.width = 128
    state.model.height = 128
    segment = LiveSegment(
        id=9,
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
        kwargs={"stream_format": "h264"},
        daemon=True,
    )

    try:
        encoder_thread.start()
        loop_thread.submit(raw_queue.put(("segment", segment))).result(timeout=2)
        frame_indices = list(range(12))
        for index in frame_indices:
            frame = np.full((128, 128, 3), index * 15, dtype=np.uint8)
            loop_thread.submit(
                raw_queue.put(
                    (
                        "raw",
                        FRAME_SILENT,
                        segment.id,
                        index * 41667,
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
            item = loop_thread.submit(network_queue.get()).result(timeout=10)
            received.append(item)
            if item[0] == "segment_end":
                break
            if item[0] == "error":
                pytest.fail(item[1])

        packets = received[1:-1]
        assert len(packets) == len(frame_indices)
        assert [item[4] for item in packets] == frame_indices
        decoded_headers = [
            H264_FRAME_HEADER.unpack_from(item[5])
            for item in packets
        ]
        assert all(header[0] == H264_FRAME_MAGIC for header in decoded_headers)
        assert decoded_headers[0][3] & VIDEO_FLAG_KEYFRAME
        assert decoded_headers[6][3] & VIDEO_FLAG_KEYFRAME
        assert all(item[6] == state.pipeline.flush_seq for item in packets)
    finally:
        stop.set()
        encoder_thread.join(timeout=10)
        loop_thread.close()

    assert not encoder_thread.is_alive()


def test_h264_async_fusion_uses_separate_thread_and_preserves_order(
    monkeypatch,
) -> None:
    monkeypatch.setenv("AVATAR_STREAM_FORMAT", "h264")
    monkeypatch.setenv("AVATAR_H264_GOP", "6")
    monkeypatch.setenv("AVATAR_STREAM_MAX_WIDTH", "0")

    blend_threads: list[int] = []

    def fake_blend(self, frame_index, frame):
        blend_threads.append(threading.get_ident())
        time.sleep(0.005)
        marker = int(frame[0, 0, 0])
        return np.full((128, 128, 3), marker, dtype=np.uint8)

    monkeypatch.setattr(
        workers.InferencePipeline,
        "blend_frame",
        fake_blend,
    )

    loop_thread = EventLoopThread()
    raw_queue = loop_thread.submit(_new_queue(64)).result(timeout=2)
    fused_queue = loop_thread.submit(_new_queue(32)).result(timeout=2)
    network_queue = loop_thread.submit(_new_queue(64)).result(timeout=2)
    stop = threading.Event()
    state = AppState(
        args=Namespace(jpeg_quality=88, fps=24),
    )
    state.model.width = 128
    state.model.height = 128
    segment = LiveSegment(
        id=10,
        source_path=None,
        wav_path=None,
        duration=0.5,
        total_frames=12,
        global_start_frame=0,
        speech_frames=12,
    )
    fusion_thread = threading.Thread(
        target=workers.live_fusion_worker,
        args=(
            state,
            raw_queue,
            fused_queue,
            loop_thread.loop,
            stop,
        ),
        daemon=True,
    )
    encoder_thread = threading.Thread(
        target=workers.live_encode_worker,
        args=(
            state,
            raw_queue,
            network_queue,
            loop_thread.loop,
            stop,
            "h264",
        ),
        kwargs={
            "input_queue": fused_queue,
            "frames_prepared": True,
        },
        daemon=True,
    )

    try:
        encoder_thread.start()
        fusion_thread.start()
        loop_thread.submit(raw_queue.put(("segment", segment))).result(timeout=2)
        frame_indices = list(range(12))
        for index in frame_indices:
            frame = np.full((8, 8, 3), index * 15, dtype=np.uint8)
            loop_thread.submit(
                raw_queue.put(
                    (
                        "raw",
                        FRAME_GENERATED,
                        segment.id,
                        index * 41667,
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
            item = loop_thread.submit(network_queue.get()).result(timeout=10)
            received.append(item)
            if item[0] == "segment_end":
                break
            if item[0] == "error":
                pytest.fail(item[1])

        frames = received[1:-1]
        assert received[0][0] == "segment"
        assert [item[4] for item in frames] == frame_indices
        assert len(blend_threads) == len(frame_indices)
        assert set(blend_threads) == {fusion_thread.ident}
        assert encoder_thread.ident not in blend_threads
    finally:
        stop.set()
        fusion_thread.join(timeout=5)
        encoder_thread.join(timeout=10)
        loop_thread.close()

    assert not fusion_thread.is_alive()
    assert not encoder_thread.is_alive()


def test_async_h264_fusion_drops_canceled_epoch_before_new_idr(
    monkeypatch,
) -> None:
    monkeypatch.setenv("AVATAR_STREAM_FORMAT", "h264")
    monkeypatch.setenv("AVATAR_H264_GOP", "6")
    monkeypatch.setenv("AVATAR_STREAM_MAX_WIDTH", "0")

    blend_started = threading.Event()
    release_blend = threading.Event()

    def delayed_blend(self, frame_index, frame):
        blend_started.set()
        assert release_blend.wait(timeout=2)
        marker = int(frame[0, 0, 0])
        return np.full((128, 128, 3), marker, dtype=np.uint8)

    monkeypatch.setattr(
        workers.InferencePipeline,
        "blend_frame",
        delayed_blend,
    )

    loop_thread = EventLoopThread()
    raw_queue = loop_thread.submit(_new_queue(64)).result(timeout=2)
    fused_queue = loop_thread.submit(_new_queue(32)).result(timeout=2)
    network_queue = loop_thread.submit(_new_queue(64)).result(timeout=2)
    stop = threading.Event()
    state = AppState(
        args=Namespace(jpeg_quality=88, fps=24),
    )
    state.model.width = 128
    state.model.height = 128
    old_segment = LiveSegment(
        id=30,
        source_path=None,
        wav_path=None,
        duration=0.5,
        total_frames=6,
        global_start_frame=0,
        speech_frames=6,
    )
    new_segment = LiveSegment(
        id=31,
        source_path=None,
        wav_path=None,
        duration=0.5,
        total_frames=6,
        global_start_frame=0,
        speech_frames=6,
    )
    fusion_thread = threading.Thread(
        target=workers.live_fusion_worker,
        args=(
            state,
            raw_queue,
            fused_queue,
            loop_thread.loop,
            stop,
        ),
        daemon=True,
    )
    encoder_thread = threading.Thread(
        target=workers.live_encode_worker,
        args=(
            state,
            raw_queue,
            network_queue,
            loop_thread.loop,
            stop,
            "h264",
        ),
        kwargs={
            "input_queue": fused_queue,
            "frames_prepared": True,
        },
        daemon=True,
    )

    def put(item) -> None:
        loop_thread.submit(raw_queue.put(item)).result(timeout=2)

    try:
        encoder_thread.start()
        fusion_thread.start()
        put(("segment", old_segment))
        put(
            (
                "raw",
                FRAME_GENERATED,
                old_segment.id,
                0,
                0,
                np.full((8, 8, 3), 20, dtype=np.uint8),
            )
        )
        assert blend_started.wait(timeout=2)

        with state.pipeline.lock:
            state.pipeline.flush_seq = 1
            state.pipeline.flush_sent_seq = 1
            state.pipeline.canceled_ids.add(old_segment.id)
        release_blend.set()

        put(("segment", new_segment))
        for index in range(6):
            put(
                (
                    "raw",
                    FRAME_GENERATED,
                    new_segment.id,
                    index * 41667,
                    index,
                    np.full((8, 8, 3), 80 + index, dtype=np.uint8),
                )
            )
        put(("segment_end", new_segment.id))

        received = []
        while True:
            item = loop_thread.submit(network_queue.get()).result(timeout=10)
            received.append(item)
            if item[0] == "segment_end":
                break
            if item[0] == "error":
                pytest.fail(item[1])

        frames = [item for item in received if item[0] == "frame"]
        assert frames
        assert all(item[2] == new_segment.id for item in frames)
        assert [item[4] for item in frames] == list(range(6))
        first_header = H264_FRAME_HEADER.unpack_from(frames[0][5])
        assert first_header[3] & VIDEO_FLAG_KEYFRAME
    finally:
        release_blend.set()
        stop.set()
        fusion_thread.join(timeout=5)
        encoder_thread.join(timeout=10)
        loop_thread.close()

    assert not fusion_thread.is_alive()
    assert not encoder_thread.is_alive()


def test_h264_flush_barrier_skips_canceled_frame_before_new_idr(
    monkeypatch,
) -> None:
    monkeypatch.setenv("AVATAR_STREAM_FORMAT", "h264")
    monkeypatch.setenv("AVATAR_H264_GOP", "6")

    loop_thread = EventLoopThread()
    raw_queue = loop_thread.submit(_new_queue(64)).result(timeout=2)
    network_queue = loop_thread.submit(_new_queue(64)).result(timeout=2)
    stop = threading.Event()
    state = AppState(
        args=Namespace(jpeg_quality=88, fps=24),
    )
    state.model.width = 128
    state.model.height = 128
    old_segment = LiveSegment(
        id=20,
        source_path=None,
        wav_path=None,
        duration=0.5,
        total_frames=12,
        global_start_frame=0,
        speech_frames=12,
    )
    new_segment = LiveSegment(
        id=21,
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
        kwargs={"stream_format": "h264"},
        daemon=True,
    )

    def put(item) -> None:
        loop_thread.submit(raw_queue.put(item)).result(timeout=2)

    def get(timeout: float = 10):
        return loop_thread.submit(network_queue.get()).result(timeout=timeout)

    try:
        encoder_thread.start()
        put(("segment", old_segment))
        put(
            (
                "raw",
                FRAME_SILENT,
                old_segment.id,
                0,
                0,
                np.zeros((128, 128, 3), dtype=np.uint8),
            )
        )
        assert get()[0] == "segment"
        initial_frame = get()
        assert H264_FRAME_HEADER.unpack_from(initial_frame[5])[3] & VIDEO_FLAG_KEYFRAME

        with state.pipeline.lock:
            state.pipeline.flush_seq = 1
            state.pipeline.canceled_ids.add(old_segment.id)

        # This frame was queued before the flush. The encoder must not consume
        # the new encoder's IDR while encoding it.
        put(
            (
                "raw",
                FRAME_SILENT,
                old_segment.id,
                1000,
                1,
                np.full((128, 128, 3), 20, dtype=np.uint8),
            )
        )
        put(("segment", new_segment))
        for index in range(2, 8):
            put(
                (
                    "raw",
                    FRAME_SILENT,
                    new_segment.id,
                    index * 41667,
                    index,
                    np.full((128, 128, 3), index * 20, dtype=np.uint8),
                )
            )
        put(("segment_end", new_segment.id))

        # The worker must not restart the H.264 encoder until the transport
        # has sent the matching flush control message.
        time.sleep(0.15)
        assert network_queue.qsize() == 0

        with state.pipeline.lock:
            state.pipeline.flush_sent_seq = 1

        received = []
        while True:
            item = get()
            received.append(item)
            if item[0] == "segment_end":
                break
            if item[0] == "error":
                pytest.fail(item[1])

        frames = [item for item in received if item[0] == "frame"]
        assert frames
        assert all(item[2] == new_segment.id for item in frames)
        assert all(item[6] == 1 for item in frames)
        first_header = H264_FRAME_HEADER.unpack_from(frames[0][5])
        assert first_header[3] & VIDEO_FLAG_KEYFRAME
    finally:
        stop.set()
        encoder_thread.join(timeout=10)
        loop_thread.close()

    assert not encoder_thread.is_alive()


def test_live_session_selects_codec_from_viewer_query() -> None:
    state = AppState()
    session = LiveStreamSession(
        state,
        SimpleNamespace(query_params={"video_codec": "webp"}),
    )
    assert session.stream_format == "webp"

    session = LiveStreamSession(
        state,
        SimpleNamespace(query_params={"video_codec": "h264"}),
    )
    assert session.stream_format == "h264"


def test_restoration_and_composite_overlap_and_preserve_fifo(monkeypatch):
    state = AppState(args=Namespace())
    entered_composite = threading.Event()
    next_restore = threading.Event()
    def restore(self, index, frame):
        if index == 2:
            assert entered_composite.wait(2)
            next_restore.set()
        return frame + 1
    def composite(self, index, frame):
        if index == 1:
            entered_composite.set()
            assert next_restore.wait(2), 'next restoration must overlap this composite'
        return frame + 2
    monkeypatch.setattr(workers.InferencePipeline, 'restore_frame', restore)
    monkeypatch.setattr(workers.InferencePipeline, 'composite_frame', composite)
    loop = EventLoopThread()
    raw = loop.submit(_new_queue(8)).result(2)
    restored = loop.submit(_new_queue(2)).result(2)
    fused = loop.submit(_new_queue(8)).result(2)
    stop = threading.Event()
    threads = [threading.Thread(target=workers.live_restoration_worker,args=(state,raw,restored,loop.loop,stop)),
               threading.Thread(target=workers.live_fusion_worker,args=(state,restored,fused,loop.loop,stop),kwargs={'frames_restored':True})]
    items=[('segment',123),('raw',FRAME_GENERATED,123,100,1,np.zeros((8,8,3),np.uint8),0,0),
           ('raw',FRAME_GENERATED,123,200,2,np.zeros((8,8,3),np.uint8),0,0),
           ('raw',FRAME_SILENT,123,300,3,np.full((8,8,3),10,np.uint8),0,0),
           ('raw',workers.FRAME_HOLD,123,400,4,None,0,0),('segment_end',123)]
    try:
        for t in threads:t.start()
        for item in items:loop.submit(raw.put(item)).result(2)
        received=[loop.submit(fused.get()).result(5) for _ in items]
        assert received[0] == items[0] and received[-1] == items[-1]
        for item, before in zip(received[1:-1],items[1:-1]):
            assert item[:5] == before[:5] and item[6:] == before[6:]
        assert np.all(received[1][5] == 3) and np.all(received[2][5] == 3)
        assert np.all(received[3][5] == 10) and received[4][5] is None
        assert next_restore.is_set()
    finally:
        stop.set()
        for t in threads:t.join(3)
        loop.close()
    assert not any(t.is_alive() for t in threads)


@pytest.mark.parametrize('change',['queued_epoch','queued_avatar','canceled','inflight_epoch','inflight_avatar'])
def test_restoration_rejects_stale_work(monkeypatch,change):
    state=AppState(args=Namespace())
    state.pipeline.flush_seq=1
    state.model.avatar_revision=1
    calls=[]
    def restore(self,index,frame):
        calls.append(index)
        if change=='inflight_epoch':state.pipeline.flush_seq+=1
        if change=='inflight_avatar':state.model.avatar_revision+=1
        return frame
    monkeypatch.setattr(workers.InferencePipeline,'restore_frame',restore)
    loop=EventLoopThread()
    raw=loop.submit(_new_queue(8)).result(2)
    restored=loop.submit(_new_queue(8)).result(2)
    stop=threading.Event()
    thread=threading.Thread(target=workers.live_restoration_worker,args=(state,raw,restored,loop.loop,stop))
    if change=='canceled':state.pipeline.canceled_ids.add(99)
    try:
        thread.start()
        epoch=0 if change=='queued_epoch' else 1
        revision=0 if change=='queued_avatar' else 1
        loop.submit(raw.put(('raw',FRAME_GENERATED,99,123,3,np.zeros((8,8,3),np.uint8),epoch,revision))).result(2)
        loop.submit(raw.put(('segment_end',99))).result(2)
        assert loop.submit(restored.get()).result(3)==('segment_end',99)
        assert calls==([3] if change.startswith('inflight') else [])
    finally:
        stop.set();thread.join(3);loop.close()
    assert not thread.is_alive()


def test_restoration_failure_is_forwarded_as_error(monkeypatch):
    def fail(self,index,frame):
        raise RuntimeError('restoration failed explicitly')
    monkeypatch.setattr(workers.InferencePipeline,'restore_frame',fail)
    state=AppState(args=Namespace())
    loop=EventLoopThread()
    raw=loop.submit(_new_queue(2)).result(2)
    restored=loop.submit(_new_queue(2)).result(2)
    stop=threading.Event()
    thread=threading.Thread(target=workers.live_restoration_worker,args=(state,raw,restored,loop.loop,stop))
    try:
        thread.start()
        loop.submit(raw.put(('raw',FRAME_GENERATED,1,0,0,np.zeros((8,8,3),np.uint8),0,0))).result(2)
        item=loop.submit(restored.get()).result(3)
        assert item[0]=='error' and 'restoration failed explicitly' in item[1]
        thread.join(2)
        assert not thread.is_alive()
    finally:
        stop.set();thread.join(3);loop.close()


def test_restoration_stops_when_output_queue_is_full(monkeypatch):
    state=AppState(args=Namespace())
    loop=EventLoopThread()
    raw=loop.submit(_new_queue(2)).result(2)
    restored=loop.submit(_new_queue(1)).result(2)
    stop=threading.Event()
    thread=threading.Thread(target=workers.live_restoration_worker,args=(state,raw,restored,loop.loop,stop))
    attempting_put = threading.Event()
    original_put = workers.put_blocking
    def observe_put(q, event_loop, item, stopped):
        if q is restored:
            attempting_put.set()
        return original_put(q, event_loop, item, stopped)
    monkeypatch.setattr(workers, 'put_blocking', observe_put)
    try:
        loop.submit(restored.put(('segment',0))).result(2)
        loop.submit(raw.put(('segment',1))).result(2)
        thread.start()
        assert attempting_put.wait(2)
        stop.set()
        thread.join(2)
        assert not thread.is_alive()
    finally:
        stop.set();thread.join(3);loop.close()


def test_flush_drains_restored_frames_along_with_other_queues():
    async def check():
        session=LiveStreamSession(AppState(args=Namespace()), SimpleNamespace(query_params={}))
        queues=[asyncio.Queue(maxsize=2) for _ in range(4)]
        session.raw_queue,session.restored_queue,session.fused_queue,session.network_queue=queues
        for q in queues:await q.put(('old-frame',))
        session._drop_queued_frames()
        assert all(q.empty() for q in queues)
    asyncio.run(check())


@pytest.mark.parametrize('change', ['queued_epoch', 'queued_avatar', 'inflight_epoch', 'inflight_avatar'])
def test_gpen_worker_drops_stale_results_and_preserves_control_order(monkeypatch, change):
    state = AppState()
    state.model.avatar_revision = 1
    state.pipeline.flush_seq = 1
    calls = []
    def infer(self, image):
        calls.append(1)
        if change == 'inflight_epoch':
            state.pipeline.flush_seq += 1
        if change == 'inflight_avatar':
            state.model.avatar_revision += 1
        return np.full_like(image, 80)
    monkeypatch.setattr(workers.InferencePipeline, 'infer_restoration', infer)
    loop_thread = EventLoopThread()
    prepared, restored = asyncio.Queue(), asyncio.Queue()
    stop = threading.Event()
    thread = threading.Thread(target=workers.live_gpen_worker,
                              args=(state, prepared, restored, loop_thread.loop, stop))
    thread.start()
    try:
        epoch = 0 if change == 'queued_epoch' else 1
        revision = 0 if change == 'queued_avatar' else 1
        loop_thread.submit(prepared.put((
            'raw', FRAME_GENERATED, 99, 123456, 3,
            np.zeros((512,512,3),np.uint8), epoch, revision,
            np.zeros((256,256,3),np.uint8), np.eye(2,3),
        ))).result(timeout=2)
        loop_thread.submit(prepared.put(('segment_end',99))).result(timeout=2)
        item = loop_thread.submit(restored.get()).result(timeout=3)
        assert item == ('segment_end',99)
        assert calls == ([1] if change.startswith('inflight') else [])
    finally:
        stop.set()
        thread.join(timeout=3)
        loop_thread.close()
    assert not thread.is_alive()
