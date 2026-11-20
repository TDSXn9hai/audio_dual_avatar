"""Thread workers connecting inference, encoding and network queues."""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import queue as std_queue
import threading
import time
import traceback

import cv2
import numpy as np

from ..audio.segments import SegmentManager
from ..avatar_modes import AvatarBaseSnapshot
from ..domain import LiveSegment
from ..log import log
from ..protocol import (
    FRAME_GENERATED,
    FRAME_IDLE,
    FRAME_SILENT,
    IDLE_SEGMENT_ID,
    frame_pts_us,
    pack_h264_frame,
    pack_frame,
)
from ..state import AppState
from .h264 import EncodedH264Frame, H264Encoder
from .pipeline import InferencePipeline

# Raw-queue marker for "repeat the previous composite frame". It never goes
# on the wire: the encode worker sends it as FRAME_SILENT.
FRAME_HOLD = -1


def direct_bbox_restoration() -> bool:
    return os.environ.get("MUSETALK_GPEN_OUTPUT_MODE", "crop").strip().lower() == "bbox"


def stream_width_limit() -> int:
    width = int(os.environ.get("AVATAR_STREAM_MAX_WIDTH", "0"))
    return width - width % 2 if width > 0 else 0


def resize_for_stream(frame: np.ndarray, stream_width: int) -> np.ndarray:
    if stream_width > 0 and frame.shape[1] > stream_width:
        height = round(frame.shape[0] * stream_width / frame.shape[1])
        height -= height % 2
        frame = cv2.resize(
            frame,
            (stream_width, height),
            interpolation=cv2.INTER_AREA,
        )
    return frame


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


def wait_for_flush_control(
    state: AppState,
    flush_seq: int,
    stop: threading.Event,
) -> bool:
    """Wait until the viewer has received the matching flush control."""
    while not stop.is_set():
        with state.pipeline.lock:
            sent_seq = state.pipeline.flush_sent_seq
        if sent_seq >= flush_seq:
            return True
        time.sleep(0.01)
    return False


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
    controller = state.model.avatar_base_controller
    if controller is not None:
        controller.rewind(0)
    pending_extra: dict[int, tuple[str, np.ndarray, AvatarBaseSnapshot | None]] = {}
    # Last segment that finished generating while an answer is still open.
    # Underruns inside an answer repeat its last frame instead of cutting to
    # the source video; between answers and during interrupts use idle frames.
    last_media: LiveSegment | None = None
    last_snapshot: AvatarBaseSnapshot | None = None
    hold_run = 0

    def emit_idle_frame() -> bool:
        nonlocal next_frame
        snapshot = (
            controller.next_idle_frame(next_frame)
            if controller is not None
            else None
        )
        source_avatar = (
            snapshot.bundle.avatar
            if snapshot is not None
            else avatar
        )
        with state.pipeline.lock:
            source_epoch = state.pipeline.flush_seq
            source_revision = state.model.avatar_revision
        cycle = source_avatar.frame_list_cycle
        source_index = (
            snapshot.source_frame(next_frame)
            if snapshot is not None
            else next_frame % len(cycle)
        )
        frame = cycle[source_index]
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
                source_epoch,
                source_revision,
                snapshot,
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
            with state.pipeline.lock:
                source_epoch = state.pipeline.flush_seq
                source_revision = state.model.avatar_revision
            held_snapshot = (
                controller.hold_frame(next_frame)
                if controller is not None
                else last_snapshot
            )
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
                    source_epoch,
                    source_revision,
                    held_snapshot,
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
                last_snapshot = None
                hold_run = 0
                pending_extra.clear()
                if controller is not None:
                    controller.rewind(reset_frame)
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
                segment_epoch = state.pipeline.flush_seq
                segment_revision = state.model.avatar_revision
            try:
                frame_bases = None
                if controller is not None:
                    mask = segment.speech_mask
                    frame_bases = [
                        controller.next_frame(
                            next_frame + offset,
                            speech=mask is None or (offset < len(mask) and bool(mask[offset])),
                        )
                        for offset in range(segment.total_frames)
                    ]
                    # Overlap is speculative. It must not advance the controller.
                    frame_bases.extend([frame_bases[-1]] * segment.extra_frames)
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
                for frame_kind, frame_index, frame in pipeline.iter_segment(segment, frame_bases):
                    if segment.id in state.pipeline.canceled_ids or (
                        state.interrupt.active and segment.priority != 0
                    ):
                        canceled = True
                        state.pipeline.canceled_ids.add(segment.id)
                        pending_extra.clear()
                        log(f"cancelled generation of segment {segment.id}")
                        break
                    snapshot = (
                        frame_bases[frame_index - segment.global_start_frame]
                        if frame_bases is not None
                        else segment.base_snapshot
                    )
                    core_end = (
                        segment.global_start_frame
                        + segment.total_frames
                    )
                    if frame_index >= core_end:
                        pending_extra[frame_index] = (
                            frame_kind,
                            frame,
                            snapshot,
                        )
                        continue
                    emit_frame = frame
                    if (
                        frame_index
                        < segment.global_start_frame + overlap
                        and frame_index in pending_extra
                    ):
                        previous_kind, previous_frame, previous_snapshot = (
                            pending_extra.pop(frame_index)
                        )
                        if (
                            previous_kind == "generated"
                            and frame_kind == "generated"
                            and previous_snapshot is snapshot
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
                            segment_epoch,
                            segment_revision,
                            snapshot,
                        ),
                        stop,
                    ):
                        return
                    if frame_kind == "generated":
                        state.pipeline.generated_frames += 1
                    else:
                        state.pipeline.silent_frames += 1
                    next_frame = max(next_frame, frame_index + 1)
                    last_snapshot = snapshot
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
        log(traceback.format_exc())
        put_blocking(
            raw_queue,
            loop,
            ("error", traceback.format_exc()),
            stop,
        )


def live_prepare_worker(
    state: AppState,
    raw_queue: asyncio.Queue,
    restored_queue: asyncio.Queue,
    loop: asyncio.AbstractEventLoop,
    stop: threading.Event,
) -> None:
    """Prepare aligned GPEN inputs without running GPEN."""
    pipeline = InferencePipeline(state)
    count = 0
    try:
        while not stop.is_set():
            item = get_blocking(raw_queue, loop, stop)
            if item is None:
                return
            kind = item[0]
            if kind in ("segment", "segment_end", "error"):
                if kind == "segment_end" and count:
                    log(
                        f"alignment stage: frames={count} "
                        f"raw_queue={raw_queue.qsize()} "
                        f"restored_queue={restored_queue.qsize()}"
                    )
                if not put_blocking(restored_queue, loop, item, stop):
                    return
                if kind == "error":
                    return
                continue
            if kind != "raw":
                continue
            _, frame_kind, segment_id, pts_us, frame_index, frame, *extra = item
            with state.pipeline.lock:
                epoch = int(extra[0]) if extra else state.pipeline.flush_seq
                revision = int(extra[1]) if len(extra) > 1 else state.model.avatar_revision
                snapshot = extra[2] if len(extra) > 2 else None
                stale = (
                    segment_id in state.pipeline.canceled_ids
                    or epoch != state.pipeline.flush_seq
                    or revision != state.model.avatar_revision
                )
            if stale:
                continue
            if frame_kind == FRAME_GENERATED:
                pipeline._dump_generated_raw(frame_index, frame)
                if snapshot is None:
                    frame, original, inverse = pipeline.prepare_restoration(
                        frame_index,
                        frame,
                    )
                else:
                    frame, original, inverse = pipeline.prepare_restoration(
                        frame_index,
                        frame,
                        snapshot,
                    )
                count += 1
            else:
                original, inverse = None, None
            with state.pipeline.lock:
                stale = (
                    segment_id in state.pipeline.canceled_ids
                    or epoch != state.pipeline.flush_seq
                    or revision != state.model.avatar_revision
                )
            if stale:
                continue
            prepared_item = (
                (
                    "raw",
                    frame_kind,
                    segment_id,
                    pts_us,
                    frame_index,
                    frame,
                    epoch,
                    revision,
                    snapshot,
                    original,
                    inverse,
                )
                if snapshot is not None
                else (
                    "raw",
                    frame_kind,
                    segment_id,
                    pts_us,
                    frame_index,
                    frame,
                    epoch,
                    revision,
                    original,
                    inverse,
                )
            )
            if not put_blocking(restored_queue, loop, prepared_item, stop):
                return
    except Exception:
        log(traceback.format_exc())
        put_blocking(
            restored_queue, loop, ("error", traceback.format_exc()), stop,
        )


def live_gpen_worker(
    state: AppState,
    prepared_queue: asyncio.Queue,
    restored_queue: asyncio.Queue,
    loop: asyncio.AbstractEventLoop,
    stop: threading.Event,
) -> None:
    """Run GPEN only on already aligned faces."""
    pipeline = InferencePipeline(state)
    try:
        while not stop.is_set():
            item = get_blocking(prepared_queue, loop, stop)
            if item is None:
                return
            if item[0] in ("segment", "segment_end", "error"):
                if not put_blocking(restored_queue, loop, item, stop):
                    return
                if item[0] == "error":
                    return
                continue
            if item[0] != "raw":
                continue
            if len(item) >= 11:
                (
                    _,
                    kind,
                    segment_id,
                    pts_us,
                    frame_index,
                    frame,
                    epoch,
                    revision,
                    snapshot,
                    original,
                    inverse,
                ) = item
            else:
                (
                    _,
                    kind,
                    segment_id,
                    pts_us,
                    frame_index,
                    frame,
                    epoch,
                    revision,
                    original,
                    inverse,
                ) = item
                snapshot = None
            with state.pipeline.lock:
                if (segment_id in state.pipeline.canceled_ids
                    or epoch != state.pipeline.flush_seq
                    or revision != state.model.avatar_revision):
                    continue
            if kind == FRAME_GENERATED:
                frame = pipeline.infer_restoration(frame)
            with state.pipeline.lock:
                if (segment_id in state.pipeline.canceled_ids
                    or epoch != state.pipeline.flush_seq
                    or revision != state.model.avatar_revision):
                    continue
            if not put_blocking(
                restored_queue, loop,
                ("raw", kind, segment_id, pts_us, frame_index, frame,
                 epoch, revision, snapshot, original, inverse), stop):
                return
    except Exception:
        log(traceback.format_exc())
        put_blocking(restored_queue, loop, ("error", traceback.format_exc()), stop)


def live_restoration_worker(
    state, raw_queue, restored_queue, loop, stop
) -> None:
    """Backward-compatible single-stage worker used by older integrations."""
    pipeline = InferencePipeline(state)
    try:
        while not stop.is_set():
            item = get_blocking(raw_queue, loop, stop)
            if item is None:
                return
            if item[0] in ("segment", "segment_end", "error"):
                if not put_blocking(restored_queue, loop, item, stop):
                    return
                if item[0] == "error":
                    return
                continue
            if item[0] != "raw":
                continue
            _, kind, segment_id, pts_us, index, frame, *extra = item
            epoch = int(extra[0]) if extra else state.pipeline.flush_seq
            revision = int(extra[1]) if len(extra) > 1 else state.model.avatar_revision
            snapshot = extra[2] if len(extra) >= 3 else None
            with state.pipeline.lock:
                if (segment_id in state.pipeline.canceled_ids
                    or epoch != state.pipeline.flush_seq
                    or revision != state.model.avatar_revision):
                    continue
            if kind == FRAME_GENERATED:
                if snapshot is None:
                    frame = pipeline.restore_frame(index, frame)
                else:
                    frame = pipeline.restore_frame(index, frame, snapshot)
            with state.pipeline.lock:
                if (segment_id in state.pipeline.canceled_ids
                    or epoch != state.pipeline.flush_seq
                    or revision != state.model.avatar_revision):
                    continue
            restored_item = (
                (
                    "raw",
                    kind,
                    segment_id,
                    pts_us,
                    index,
                    frame,
                    epoch,
                    revision,
                    snapshot,
                )
                if snapshot is not None
                else (
                    "raw",
                    kind,
                    segment_id,
                    pts_us,
                    index,
                    frame,
                    epoch,
                    revision,
                )
            )
            if not put_blocking(restored_queue, loop, restored_item, stop):
                return
    except Exception:
        put_blocking(restored_queue, loop, ("error", traceback.format_exc()), stop)


def live_fusion_worker(
    state: AppState,
    raw_queue: asyncio.Queue,
    fused_queue: asyncio.Queue,
    loop: asyncio.AbstractEventLoop,
    stop: threading.Event,
    frames_restored: bool = False,
) -> None:
    """Fuse generated faces into source frames ahead of the H.264 encoder."""
    pipeline = InferencePipeline(state)
    stream_width = stream_width_limit()
    count = 0
    generated_count = 0
    total_ms = 0.0
    direct_bbox = direct_bbox_restoration()
    try:
        while not stop.is_set():
            item = get_blocking(raw_queue, loop, stop)
            if item is None:
                return

            kind = item[0]
            if kind in ("segment", "segment_end", "error"):
                if kind == "segment_end" and generated_count:
                    log(
                        f"composite stage: frames={generated_count} "
                        f"mean={total_ms / generated_count:.2f}ms "
                        f"input_queue={raw_queue.qsize()} "
                        f"fused_queue={fused_queue.qsize()}"
                    )
                if not put_blocking(fused_queue, loop, item, stop):
                    return
                if kind == "error":
                    return
                continue
            if kind != "raw":
                continue

            _, frame_kind, segment_id, pts_us, frame_index, frame, *extra = item
            original = inverse = None
            snapshot = (
                extra[2]
                if (len(extra) >= 5 if frames_restored else len(extra) >= 3)
                else None
            )
            if frames_restored and len(extra) >= 5:
                source_flush_seq, source_revision = extra[:2]
                original, inverse = extra[3:5]
            else:
                source_flush_seq = int(extra[0]) if extra else state.pipeline.flush_seq
                source_revision = int(extra[1]) if len(extra) > 1 else state.model.avatar_revision
            with state.pipeline.lock:
                source_flush_seq = int(extra[0]) if extra else state.pipeline.flush_seq
                source_revision = int(extra[1]) if len(extra) > 1 else state.model.avatar_revision
                canceled = (
                    segment_id in state.pipeline.canceled_ids
                    or source_flush_seq != state.pipeline.flush_seq
                    or source_revision != state.model.avatar_revision
                )
            if canceled:
                continue

            if frame_kind == FRAME_HOLD:
                prepared = None
            else:
                if frame_kind == FRAME_GENERATED:
                    started = time.perf_counter()
                    if frames_restored:
                        if original is not None:
                            if direct_bbox:
                                if snapshot is None:
                                    restored_crop = (
                                        pipeline.finalize_restoration_to_bbox(
                                            frame_index,
                                            frame,
                                            original,
                                            inverse,
                                        )
                                    )
                                    prepared = pipeline.composite_frame(
                                        frame_index,
                                        restored_crop,
                                        already_bbox=True,
                                    )
                                else:
                                    restored_crop = (
                                        pipeline.finalize_restoration_to_bbox(
                                            frame_index,
                                            frame,
                                            original,
                                            inverse,
                                            snapshot,
                                        )
                                    )
                                    prepared = pipeline.composite_frame(
                                        frame_index,
                                        restored_crop,
                                        already_bbox=True,
                                        snapshot=snapshot,
                                    )
                            else:
                                restored_crop = pipeline.finalize_restoration(
                                    frame,
                                    original,
                                    inverse,
                                )
                                if snapshot is None:
                                    prepared = pipeline.composite_frame(
                                        frame_index,
                                        restored_crop,
                                    )
                                else:
                                    prepared = pipeline.composite_frame(
                                        frame_index,
                                        restored_crop,
                                        snapshot=snapshot,
                                    )
                        else:
                            if snapshot is None:
                                prepared = pipeline.composite_frame(
                                    frame_index,
                                    frame,
                                )
                            else:
                                prepared = pipeline.composite_frame(
                                    frame_index,
                                    frame,
                                    snapshot=snapshot,
                                )
                    else:
                        if snapshot is None:
                            prepared = pipeline.blend_frame(
                                frame_index,
                                frame,
                            )
                        else:
                            prepared = pipeline.blend_frame(
                                frame_index,
                                frame,
                                snapshot,
                            )
                    total_ms += (time.perf_counter() - started) * 1000
                    generated_count += 1
                else:
                    prepared = frame
                prepared = resize_for_stream(prepared, stream_width)

            with state.pipeline.lock:
                canceled = (
                    segment_id in state.pipeline.canceled_ids
                    or source_flush_seq != state.pipeline.flush_seq
                    or source_revision != state.model.avatar_revision
                )
            if canceled:
                continue

            fused_item = (
                (
                    "raw",
                    frame_kind,
                    segment_id,
                    pts_us,
                    frame_index,
                    prepared,
                    source_flush_seq,
                    source_revision,
                    snapshot,
                )
                if snapshot is not None
                else (
                    "raw",
                    frame_kind,
                    segment_id,
                    pts_us,
                    frame_index,
                    prepared,
                    source_flush_seq,
                    source_revision,
                )
            )
            if not put_blocking(fused_queue, loop, fused_item, stop):
                return
            count += 1
            if count % 500 == 0:
                log(
                    f"fused {count} live frames "
                    f"fused_queue={fused_queue.qsize()}"
                )
    except Exception:
        log(traceback.format_exc())
        put_blocking(
            fused_queue,
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
    stream_format: str | None = None,
    input_queue: asyncio.Queue | None = None,
    frames_prepared: bool = False,
) -> None:
    args = state.args
    assert args is not None
    source_queue = input_queue if input_queue is not None else raw_queue
    pipeline = InferencePipeline(state)
    count = 0
    started = time.monotonic()
    last_encoded: bytes | None = None
    last_composite: np.ndarray | None = None
    stream_width = stream_width_limit()
    selected_format = (
        stream_format
        or os.environ.get("AVATAR_STREAM_FORMAT", "h264")
    ).lower()
    use_h264 = selected_format in ("h264", "h264_annexb")
    use_webp = not use_h264 and selected_format != "jpeg"
    extension = ".webp" if use_webp else ".jpg"
    quality_key = cv2.IMWRITE_WEBP_QUALITY if use_webp else cv2.IMWRITE_JPEG_QUALITY
    encode_threads_raw = os.environ.get("AVATAR_ENCODE_THREADS", "4") or "4"
    try:
        encode_threads = int(encode_threads_raw)
    except ValueError:
        log(
            f"invalid AVATAR_ENCODE_THREADS={encode_threads_raw!r}; "
            "falling back to 4"
        )
        encode_threads = 4
    encode_threads = max(1, min(32, encode_threads))
    # Keep several frames in flight without allowing a full-resolution
    # backlog to grow without bound.
    ordered_queue: std_queue.Queue = std_queue.Queue(
        maxsize=max(8, encode_threads * 4),
    )
    sender_gone = threading.Event()
    executor: concurrent.futures.ThreadPoolExecutor | None = None
    h264_encoder: H264Encoder | None = None

    def encode_prepared(frame: np.ndarray) -> bytes | None:
        ok, encoded = cv2.imencode(
            extension,
            frame,
            [quality_key, args.jpeg_quality],
        )
        if not ok and use_webp:
            ok, encoded = cv2.imencode(
                ".jpg",
                frame,
                [cv2.IMWRITE_JPEG_QUALITY, args.jpeg_quality],
            )
        return encoded.tobytes() if ok else None

    def put_ordered(
        future: concurrent.futures.Future | None,
        metadata: tuple,
    ) -> bool:
        while not stop.is_set() and not sender_gone.is_set():
            try:
                ordered_queue.put((future, metadata), timeout=0.1)
                return True
            except std_queue.Full:
                continue
        return False

    def ordered_sender() -> None:
        nonlocal count, last_encoded
        try:
            while True:
                try:
                    item = ordered_queue.get(timeout=0.1)
                except std_queue.Empty:
                    if stop.is_set() or sender_gone.is_set():
                        return
                    continue
                if item is None:
                    return
                future, metadata = item
                item_type = metadata[0]

                if item_type == "control":
                    control = metadata[1]
                    if not put_blocking(network_queue, loop, control, stop):
                        return
                    if control[0] == "error":
                        stop.set()
                        return
                    continue

                _, frame_kind, segment_id, pts_us, frame_index = metadata[:5]
                frame_flush_seq = metadata[-1]
                with state.pipeline.lock:
                    current_flush_seq = state.pipeline.flush_seq
                if frame_flush_seq != current_flush_seq:
                    continue
                encoded: EncodedH264Frame | bytes | None = None
                if item_type == "hold":
                    encoded_bytes = last_encoded
                    if encoded_bytes is None:
                        # This is only possible when a hold is emitted before
                        # the first encoded frame. Encode the fallback once.
                        encoded_bytes = encode_prepared(metadata[5])
                else:
                    assert future is not None
                    try:
                        encoded = future.result()
                    except Exception:
                        log(traceback.format_exc())
                        continue
                    if isinstance(encoded, EncodedH264Frame):
                        encoded_bytes = encoded.data
                    else:
                        encoded_bytes = encoded
                if encoded_bytes is None:
                    continue
                with state.pipeline.lock:
                    canceled = segment_id in state.pipeline.canceled_ids
                if canceled:
                    continue
                if isinstance(encoded, EncodedH264Frame):
                    packet = pack_h264_frame(
                        frame_kind,
                        segment_id,
                        pts_us,
                        frame_index,
                        encoded_bytes,
                        keyframe=encoded.keyframe,
                    )
                    last_encoded = None
                else:
                    packet = pack_frame(
                        frame_kind,
                        segment_id,
                        pts_us,
                        frame_index,
                        encoded_bytes,
                    )
                    last_encoded = encoded_bytes
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
                        frame_flush_seq,
                    ),
                    stop,
                ):
                    return
                count += 1
                if count % 500 == 0:
                    log(
                        f"encoded {count} live frames "
                        f"at={time.monotonic() - started:.3f}s "
                        f"network_queue={network_queue.qsize()} "
                        f"encode_threads={encode_threads}"
                    )
        except Exception:
            sender_gone.set()
            put_blocking(
                network_queue,
                loop,
                ("error", traceback.format_exc()),
                stop,
            )

    def h264_error(message: str) -> None:
        put_blocking(
            network_queue,
            loop,
            ("error", message),
            stop,
        )
        stop.set()

    output_width = int(state.model.width)
    output_height = int(state.model.height)
    if stream_width > 0 and output_width > stream_width:
        output_height = round(output_height * stream_width / output_width)
        output_width = stream_width
    output_width -= output_width % 2
    output_height -= output_height % 2
    encoder_flush_seq = state.pipeline.flush_seq

    def create_h264_encoder() -> H264Encoder:
        return H264Encoder(
            width=output_width,
            height=output_height,
            fps=args.fps,
            on_error=h264_error,
        )

    try:
        if use_h264:
            h264_encoder = create_h264_encoder()
            log(
                f"live encoder started: format=h264 "
                f"size={output_width}x{output_height} fps={args.fps}"
            )
        else:
            executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=encode_threads,
                thread_name_prefix="avatar-image",
            )
            log(
                f"live encoder started: format={extension} "
                f"threads={encode_threads} quality={args.jpeg_quality}"
            )
    except Exception:
        put_blocking(
            network_queue,
            loop,
            ("error", traceback.format_exc()),
            stop,
        )
        return
    sender = threading.Thread(
        target=ordered_sender,
        name="avatar-frame-sender",
        daemon=True,
    )
    sender.start()
    try:
        while not stop.is_set():
            item = get_blocking(source_queue, loop, stop)
            if item is None:
                return
            with state.pipeline.lock:
                current_flush_seq = state.pipeline.flush_seq
            if use_h264 and current_flush_seq != encoder_flush_seq:
                if not wait_for_flush_control(
                    state,
                    current_flush_seq,
                    stop,
                ):
                    return
                with state.pipeline.lock:
                    current_flush_seq = state.pipeline.flush_seq
                assert h264_encoder is not None
                h264_encoder.close()
                h264_encoder = create_h264_encoder()
                encoder_flush_seq = current_flush_seq
                log(
                    "H.264 encoder restarted for flush "
                    f"seq={encoder_flush_seq}"
                )
            kind = item[0]
            if kind == "raw":
                segment_id = item[2]
                with state.pipeline.lock:
                    canceled = segment_id in state.pipeline.canceled_ids
                if canceled:
                    # A frame dequeued before flush may still be in flight
                    # here. Feeding it to the new encoder would consume the
                    # post-flush IDR for a segment the viewer must discard.
                    continue
            if kind in ("segment", "segment_end", "error"):
                if not put_ordered(None, ("control", item)):
                    return
                if kind == "error":
                    return
                if kind == "segment":
                    log(f"encoding segment {item[1].id}")
                continue

            if kind != "raw":
                continue

            (
                _,
                frame_kind,
                segment_id,
                pts_us,
                frame_index,
                frame,
                *extra,
            ) = item
            source_flush_seq = (
                int(extra[0]) if extra else encoder_flush_seq
            )
            source_revision = (
                int(extra[1]) if len(extra) > 1 else state.model.avatar_revision
            )
            snapshot = extra[2] if len(extra) > 2 else None
            if use_h264 and source_flush_seq != encoder_flush_seq:
                continue
            if source_revision != state.model.avatar_revision:
                continue
            if frame_kind == FRAME_HOLD:
                frame_kind = FRAME_SILENT
                if use_h264:
                    fallback = last_composite
                    if fallback is None:
                        source_avatar = (
                            snapshot.bundle.avatar
                            if snapshot is not None
                            else state.model.avatar
                        )
                        cycle = source_avatar.frame_list_cycle
                        fallback = resize_for_stream(
                            cycle[snapshot.source_frame(frame_index) if snapshot is not None
                                  else frame_index % len(cycle)],
                            stream_width,
                        )
                    assert h264_encoder is not None
                    future = h264_encoder.submit(fallback)
                else:
                    # Repeat the whole image composite: re-blending the old
                    # face onto a newer background frame would misalign it.
                    source_avatar = (
                        snapshot.bundle.avatar
                        if snapshot is not None
                        else state.model.avatar
                    )
                    cycle = source_avatar.frame_list_cycle
                    fallback = resize_for_stream(
                        cycle[snapshot.source_frame(frame_index) if snapshot is not None
                              else frame_index % len(cycle)],
                        stream_width,
                    )
                    future = None
                metadata = (
                    "hold" if not use_h264 else "frame",
                    frame_kind,
                    segment_id,
                    pts_us,
                    frame_index,
                    fallback,
                    encoder_flush_seq,
                )
                if not put_ordered(future, metadata):
                    return
            else:
                if frame_kind == FRAME_GENERATED and not frames_prepared:
                    combined = pipeline.blend_frame(
                        frame_index,
                        frame,
                        snapshot,
                    )
                else:
                    combined = frame
                with state.pipeline.lock:
                    stale = (
                        segment_id in state.pipeline.canceled_ids
                        or source_flush_seq != state.pipeline.flush_seq
                        or source_revision != state.model.avatar_revision
                    )
                if stale:
                    continue
                if not frames_prepared:
                    combined = resize_for_stream(combined, stream_width)
                last_composite = combined
                if use_h264:
                    assert h264_encoder is not None
                    future = h264_encoder.submit(combined)
                else:
                    assert executor is not None
                    future = executor.submit(encode_prepared, combined)
                if not put_ordered(
                    future,
                    (
                        "frame",
                        frame_kind,
                        segment_id,
                        pts_us,
                        frame_index,
                        encoder_flush_seq,
                    ),
                ):
                    return
    except Exception:
        sender_gone.set()
        log(traceback.format_exc())
        put_blocking(
            network_queue,
            loop,
            ("error", traceback.format_exc()),
            stop,
        )
    finally:
        # Stop the producer side of the bounded queue and wait for all accepted
        # encode jobs to retire before the caller tears down queues/threads.
        if h264_encoder is not None:
            h264_encoder.close()
        try:
            while not ordered_queue.full():
                ordered_queue.put(None, timeout=0.1)
                break
        except (std_queue.Full, ValueError):
            pass
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=False)
        sender.join(timeout=5)
