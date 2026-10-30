"""Thread workers connecting inference, encoding and network queues."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
import traceback

import cv2
import numpy as np

from ..audio.segments import SegmentManager
from ..domain import LiveSegment
from ..log import log
from ..protocol import (
    FRAME_GENERATED,
    FRAME_IDLE,
    FRAME_SILENT,
    IDLE_SEGMENT_ID,
    frame_pts_us,
    pack_frame,
)
from ..state import AppState
from .pipeline import InferencePipeline

# Raw-queue marker for "repeat the previous composite frame". It never goes
# on the wire: the encode worker sends it as FRAME_SILENT.
FRAME_HOLD = -1


def blend_generated_frames(
    previous,
    current,
    alpha: float,
):
    """Blend two same-timestamp generated faces for a segment seam."""
    if previous.shape != current.shape:
        return current
    blended = (
        previous.astype(np.float32) * (1.0 - alpha)
        + current.astype(np.float32) * alpha
    )
    return np.clip(blended, 0, 255).astype(np.uint8)


def put_blocking(
    queue: asyncio.Queue,
    loop: asyncio.AbstractEventLoop,
    item: tuple,
    stop: threading.Event,
) -> bool:
    future = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
    while True:
        try:
            future.result(timeout=0.5)
            return True
        except concurrent.futures.TimeoutError:
            if stop.is_set():
                future.cancel()
                return False


def get_blocking(
    queue: asyncio.Queue,
    loop: asyncio.AbstractEventLoop,
    stop: threading.Event,
):
    future = asyncio.run_coroutine_threadsafe(queue.get(), loop)
    while True:
        try:
            return future.result(timeout=0.5)
        except concurrent.futures.TimeoutError:
            if stop.is_set():
                future.cancel()
                return None


def live_infer_worker(
    state: AppState,
    raw_queue: asyncio.Queue,
    loop: asyncio.AbstractEventLoop,
    stop: threading.Event,
) -> None:
    args = state.args
    assert args is not None
    avatar = state.model.avatar
    assert avatar is not None
    segments = SegmentManager(state)
    pipeline = InferencePipeline(state)
    next_frame = 0
    pending_extra: dict[int, tuple[str, np.ndarray]] = {}
    # Last segment that finished generating while an answer is still open.
    # Underruns inside an answer repeat its last frame instead of cutting to
    # the source video; between answers and during interrupts use idle frames.
    last_media: LiveSegment | None = None
    hold_run = 0

    def emit_idle_frame() -> bool:
        nonlocal next_frame
        frame = avatar.frame_list_cycle[
            next_frame % len(avatar.frame_list_cycle)
        ]
        if not put_blocking(
            raw_queue,
            loop,
            (
                "raw",
                FRAME_IDLE,
                IDLE_SEGMENT_ID,
                frame_pts_us(next_frame, state.model.fps),
                next_frame,
                frame,
            ),
            stop,
        ):
            return False
        state.pipeline.idle_frames += 1
        next_frame += 1
        return True

    def emit_gap_frame() -> bool:
        nonlocal next_frame, last_media, hold_run
        if (
            last_media is not None
            and not last_media.is_final
            and not state.interrupt.active
            and last_media.id not in state.pipeline.canceled_ids
            and hold_run < args.hold_max_frames
        ):
            if not put_blocking(
                raw_queue,
                loop,
                (
                    "raw",
                    FRAME_HOLD,
                    last_media.id,
                    frame_pts_us(next_frame, state.model.fps),
                    next_frame,
                    None,
                ),
                stop,
            ):
                return False
            if hold_run == 0:
                log(
                    f"underrun inside answer: holding last frame of "
                    f"segment {last_media.id} from frame {next_frame}"
                )
            state.pipeline.hold_frames += 1
            hold_run += 1
            next_frame += 1
            return True
        if last_media is not None:
            log(
                f"answer gap ends hold after {hold_run} frames: "
                f"segment={last_media.id} final={last_media.is_final} "
                f"interrupt={state.interrupt.active}"
            )
        # Once the source video is visible, never go back to a stale face.
        last_media = None
        hold_run = 0
        return emit_idle_frame()

    try:
        while not stop.is_set() and not state.service.exit_requested:
            with state.pipeline.lock:
                reset_frame = state.interrupt.reset_frame
                if reset_frame >= 0:
                    state.interrupt.reset_frame = -1
            if reset_frame >= 0:
                last_media = None
                hold_run = 0
            if reset_frame >= 0 and reset_frame != next_frame:
                next_frame = reset_frame

            segment = segments.next_segment()
            if segment is None:
                if (
                    next_frame
                    <= state.pipeline.ack_frame + args.idle_prebuffer_frames
                ):
                    if not emit_gap_frame():
                        return
                else:
                    time.sleep(0.02)
                continue

            if segment.id in state.pipeline.canceled_ids:
                continue
            if state.interrupt.active and segment.priority != 0:
                pending_extra.clear()
                segments.enqueue(segment)
                time.sleep(0.1)
                continue

            desired_start = next_frame
            # Optionally buffer only before an answer starts; padding every
            # continuation window would insert hold frames mid-answer.
            continuing_answer = (
                args.prebuffer_answer_start_only
                and last_media is not None
                and not last_media.is_final
            )
            if state.pipeline.ack_frame >= 0 and not continuing_answer:
                desired_start = max(
                    desired_start,
                    state.pipeline.ack_frame
                    + max(args.prebuffer, args.idle_prebuffer_frames),
                )
            while next_frame < desired_start:
                if not emit_gap_frame():
                    return
            hold_run = 0

            segment.global_start_frame = next_frame
            overlap = min(segment.extra_frames, segment.total_frames)
            for frame_index in list(pending_extra):
                if (
                    frame_index < segment.global_start_frame
                    or frame_index >= segment.global_start_frame + overlap
                ):
                    pending_extra.pop(frame_index, None)
            with state.pipeline.lock:
                state.pipeline.latest_media_segment_id = segment.id
                state.pipeline.latest_media_end_frame = (
                    next_frame + segment.total_frames - 1
                )
            completed = False
            canceled = False
            with state.pipeline.lock:
                state.pipeline.generating += 1
            try:
                if not put_blocking(
                    raw_queue, loop, ("segment", segment), stop
                ):
                    return
                log(
                    f"generating segment {segment.id}: "
                    f"{segment.total_frames} frames "
                    f"start_frame={segment.global_start_frame} "
                    f"start_pts="
                    f"{frame_pts_us(segment.global_start_frame, state.model.fps)}us"
                )
                for frame_kind, frame_index, frame in pipeline.iter_segment(
                    segment
                ):
                    if segment.id in state.pipeline.canceled_ids or (
                        state.interrupt.active and segment.priority != 0
                    ):
                        canceled = True
                        state.pipeline.canceled_ids.add(segment.id)
                        pending_extra.clear()
                        log(f"cancelled generation of segment {segment.id}")
                        break
                    core_end = (
                        segment.global_start_frame
                        + segment.total_frames
                    )
                    if frame_index >= core_end:
                        pending_extra[frame_index] = (
                            frame_kind,
                            frame,
                        )
                        continue
                    emit_frame = frame
                    if (
                        frame_index
                        < segment.global_start_frame + overlap
                        and frame_index in pending_extra
                    ):
                        previous_kind, previous_frame = (
                            pending_extra.pop(frame_index)
                        )
                        if (
                            previous_kind == "generated"
                            and frame_kind == "generated"
                        ):
                            alpha = (
                                frame_index
                                - segment.global_start_frame
                                + 1
                            ) / max(1, overlap)
                            emit_frame = blend_generated_frames(
                                previous_frame,
                                frame,
                                alpha,
                            )
                    if not put_blocking(
                        raw_queue,
                        loop,
                        (
                            "raw",
                            (
                                FRAME_GENERATED
                                if frame_kind == "generated"
                                else FRAME_SILENT
                            ),
                            segment.id,
                            frame_pts_us(frame_index, state.model.fps),
                            frame_index,
                            emit_frame,
                        ),
                        stop,
                    ):
                        return
                    if frame_kind == "generated":
                        state.pipeline.generated_frames += 1
                    else:
                        state.pipeline.silent_frames += 1
                    next_frame = max(next_frame, frame_index + 1)
                if not canceled:
                    if not put_blocking(
                        raw_queue, loop, ("segment_end", segment.id), stop
                    ):
                        return
                    completed = True
                    last_media = segment
                    hold_run = 0
                else:
                    last_media = None
                    hold_run = 0
            finally:
                with state.pipeline.lock:
                    state.pipeline.generating -= 1
                if not completed and not canceled:
                    segments.enqueue(segment)
    except Exception:
        put_blocking(
            raw_queue,
            loop,
            ("error", traceback.format_exc()),
            stop,
        )


def live_encode_worker(
    state: AppState,
    raw_queue: asyncio.Queue,
    network_queue: asyncio.Queue,
    loop: asyncio.AbstractEventLoop,
    stop: threading.Event,
) -> None:
    args = state.args
    assert args is not None
    pipeline = InferencePipeline(state)
    count = 0
    started = time.monotonic()
    last_encoded: bytes | None = None
    try:
        while not stop.is_set():
            item = get_blocking(raw_queue, loop, stop)
            if item is None:
                return
            kind = item[0]
            if kind in ("segment", "segment_end", "error"):
                put_blocking(network_queue, loop, item, stop)
                if kind == "error":
                    return
                if kind == "segment":
                    log(f"encoding segment {item[1].id}")
                continue

            if kind != "raw":
                continue

            _, frame_kind, segment_id, pts_us, frame_index, frame = item
            if frame_kind == FRAME_HOLD and last_encoded is not None:
                # Repeat the whole composite: re-blending the old face onto a
                # newer background frame would misalign it.
                frame_kind = FRAME_SILENT
                jpeg = last_encoded
            else:
                if frame_kind == FRAME_HOLD:
                    frame_kind = FRAME_SILENT
                    cycle = state.model.avatar.frame_list_cycle
                    frame = cycle[frame_index % len(cycle)]
                if frame_kind == FRAME_GENERATED:
                    combined = pipeline.blend_frame(frame_index, frame)
                else:
                    combined = frame
                ok, encoded = cv2.imencode(
                    ".jpg",
                    combined,
                    [cv2.IMWRITE_JPEG_QUALITY, args.jpeg_quality],
                )
                if not ok:
                    continue
                jpeg = encoded.tobytes()
                last_encoded = jpeg
            packet = pack_frame(
                frame_kind,
                segment_id,
                pts_us,
                frame_index,
                jpeg,
            )
            if not put_blocking(
                network_queue,
                loop,
                (
                    "frame",
                    frame_kind,
                    segment_id,
                    pts_us,
                    frame_index,
                    packet,
                ),
                stop,
            ):
                return
            count += 1
            if count % 500 == 0:
                log(
                    f"encoded {count} live frames "
                    f"at={time.monotonic() - started:.3f}s "
                    f"network_queue={network_queue.qsize()}"
                )
    except Exception:
        put_blocking(
            network_queue,
            loop,
            ("error", traceback.format_exc()),
            stop,
        )
