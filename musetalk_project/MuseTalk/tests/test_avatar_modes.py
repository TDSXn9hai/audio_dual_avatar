import asyncio
import random
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np

from muse_stream.avatar_modes import (
    AvatarBaseBundle,
    AvatarBaseController,
)
from muse_stream.config import parse_avatar_base_configs
from muse_stream.audio.minicpm import MiniCPMBridgeSession
from muse_stream.audio.segments import SegmentManager
from muse_stream.inference import workers
from muse_stream.state import AppState
from scripts.realtime_inference import build_loop


class FirstChoice:
    """Deterministic stand-in for random.Random: always picks the first candidate."""

    def choice(self, values):
        return values[0]


def make_controller(rng=None):
    return AvatarBaseController(
        [
            AvatarBaseBundle(
                base="listen",
                variant_id="listen-1",
                cycle_length=4,
            ),
            AvatarBaseBundle(
                base="listen",
                variant_id="listen-2",
                cycle_length=4,
            ),
            AvatarBaseBundle(
                base="speak",
                variant_id="speak-1",
                cycle_length=4,
            ),
            AvatarBaseBundle(
                base="speak",
                variant_id="speak-2",
                cycle_length=4,
            ),
        ],
        initial_base="listen",
        rng=rng or FirstChoice(),
    )


def test_repeated_model_mode_does_not_create_revisions():
    controller = make_controller()

    controller.observe_model_mode("listen")
    first = controller.snapshot_for_segment()
    controller.observe_model_mode("listen")
    second = controller.snapshot_for_segment()

    assert second.revision == first.revision
    assert second.variant_id == first.variant_id


def test_listen_base_can_snapshot_a_speak_segment_before_boundary():
    controller = make_controller()

    controller.observe_model_mode("speak")
    snapshot = controller.snapshot_for_segment()

    assert snapshot.base == "listen"
    assert snapshot.variant_id == "listen-1"


def test_pending_base_applies_only_after_current_cycle_boundary():
    controller = make_controller()

    controller.observe_model_mode("speak")
    before_boundary = controller.next_idle_frame(3)
    at_boundary = controller.next_frame(4, speech=True)

    assert before_boundary.base == "listen"
    assert before_boundary.revision == 1
    assert at_boundary.base == "speak"
    assert at_boundary.variant_id == "speak-1"
    assert at_boundary.revision == 2


def test_segment_start_at_boundary_applies_pending_base():
    controller = make_controller()

    controller.observe_model_mode("speak")
    snapshot = controller.next_frame(4, speech=True)

    assert snapshot.base == "speak"
    assert snapshot.variant_id == "speak-1"
    assert snapshot.revision == 2


def test_latest_target_wins_before_boundary():
    controller = make_controller()

    controller.observe_model_mode("speak")
    controller.observe_model_mode("listen")
    snapshot = controller.next_idle_frame(4)

    assert snapshot.base == "listen"
    assert snapshot.variant_id == "listen-2"


def test_same_state_draws_a_different_clip_at_each_boundary():
    controller = make_controller(rng=random.Random(7))
    played = [controller.next_idle_frame(0).variant_id]
    for frame in range(1, 4 * 50):
        snapshot = controller.next_idle_frame(frame)
        if snapshot.source_frame(frame) == 0:
            played.append(snapshot.variant_id)
    assert len(played) == 50
    assert all(a != b for a, b in zip(played, played[1:]))


def test_random_draw_covers_the_whole_pool_without_repeating():
    bundles = [AvatarBaseBundle("listen", f"listen-{i}", 2) for i in range(5)]
    controller = AvatarBaseController(bundles, rng=random.Random(3))
    played = []
    for frame in range(0, 2 * 200, 2):
        played.append(controller.next_idle_frame(frame).variant_id)
    assert set(played) == {item.variant_id for item in bundles}
    assert all(a != b for a, b in zip(played, played[1:]))


def test_single_clip_pool_keeps_looping_without_new_revisions():
    controller = AvatarBaseController(
        [AvatarBaseBundle("listen", "listen-1", 4)], rng=random.Random(0)
    )
    assert controller.next_idle_frame(8).revision == 1
    assert controller.next_idle_frame(8).source_frame(8) == 0


def test_switching_back_uses_next_variant_of_target_base():
    controller = make_controller()

    controller.observe_model_mode("speak")
    first_speak = controller.next_frame(4, speech=True)
    controller.observe_model_mode("listen")
    first_listen = controller.next_idle_frame(8)
    controller.observe_model_mode("speak")
    second_speak = controller.next_frame(12, speech=True)

    assert first_speak.variant_id == "speak-1"
    assert first_listen.variant_id == "listen-2"
    assert second_speak.variant_id == "speak-2"


def test_avatar_base_config_parses_two_variants_per_base():
    configs = parse_avatar_base_configs(
        {
            "id": "legacy",
            "video_path": "legacy.mp4",
            "bases": {
                "listen": [
                    {"id": "listen-1", "cache_id": "cache-listen-1"},
                    {"id": "listen-2", "cache_id": "cache-listen-2"},
                ],
                "speak": [
                    {"id": "speak-1", "cache_id": "cache-speak-1"},
                    {"id": "speak-2", "cache_id": "cache-speak-2"},
                ],
            },
        }
    )

    assert [item.variant_id for item in configs["listen"]] == [
        "listen-1",
        "listen-2",
    ]
    assert [item.cache_id for item in configs["speak"]] == [
        "cache-speak-1",
        "cache-speak-2",
    ]


def test_legacy_avatar_config_maps_to_one_listen_variant():
    configs = parse_avatar_base_configs(
        {"id": "legacy", "video_path": "legacy.mp4"}
    )

    assert [item.variant_id for item in configs["listen"]] == ["legacy"]
    assert configs["speak"] == ()


def test_forward_loop_does_not_append_reverse_frames():
    source = ["first", "middle", "last"]

    assert build_loop(source, "forward") == source
    assert build_loop(source, "pingpong") == [
        "first",
        "middle",
        "last",
        "last",
        "middle",
        "first",
    ]


def test_listen_delta_updates_base_without_ending_idle_session():
    controller = make_controller()
    session = object.__new__(MiniCPMBridgeSession)
    session.state = SimpleNamespace(
        model=SimpleNamespace(avatar_base_controller=controller)
    )
    session.turn_open = False

    asyncio.run(
        session._handle_message(
            {
                "type": "response.output.delta",
                "kind": "listen",
            }
        )
    )

    assert controller.active_base == "listen"
    assert controller.pending_base is None


def test_speak_audio_updates_target_without_delaying_audio_delivery():
    controller = make_controller()
    session = object.__new__(MiniCPMBridgeSession)
    session.state = SimpleNamespace(
        model=SimpleNamespace(avatar_base_controller=controller)
    )
    session._on_audio = AsyncMock()

    asyncio.run(
        session._handle_message(
            {
                "type": "response.output.delta",
                "kind": "audio",
                "response_id": "response-1",
                "audio": "encoded-audio",
            }
        )
    )

    session._on_audio.assert_awaited_once_with(
        "response-1",
        "encoded-audio",
    )
    assert controller.active_base == "listen"
    assert controller.pending_base == "speak"

    segment_snapshot = controller.snapshot_for_segment()
    assert segment_snapshot.base == "listen"
    assert segment_snapshot.variant_id == "listen-1"


def test_listen_transition_finishes_audio_without_barge_in_flush():
    controller = make_controller()
    session = object.__new__(MiniCPMBridgeSession)
    session.state = SimpleNamespace(
        model=SimpleNamespace(avatar_base_controller=controller)
    )
    session.turn_open = True
    session._end_turn = AsyncMock()
    session._barge_in = AsyncMock()

    asyncio.run(
        session._handle_message(
            {
                "type": "response.output.delta",
                "kind": "listen",
            }
        )
    )

    session._end_turn.assert_awaited_once_with("listen")
    session._barge_in.assert_not_called()
    assert controller.active_base == "listen"
    assert controller.pending_base is None


def _bridge_with_queued_reply():
    state = AppState()
    state.pipeline.ack_frame = 40
    for segment_id in (5, 6, 7):
        state.pipeline.segments[segment_id] = SimpleNamespace(id=segment_id)
    # 5 is generating; 6 and 7 are queued behind it.
    state.pipeline.normal_queue.put(state.pipeline.segments[6])
    state.pipeline.normal_queue.put(state.pipeline.segments[7])
    session = object.__new__(MiniCPMBridgeSession)
    session.state = state
    session.segments = SegmentManager(state)
    session.buffer = bytearray(b"\x00" * 64)
    session.context_left_bytes = 0
    session.response_id = "resp-old"
    session.last_response_id = "resp-old"
    session.turn_open = True
    session.resampler = object()
    session.flushes = 0
    session.session_flushed = False
    return session, state


def test_session_reset_drops_unplayed_reply_instead_of_finishing_it():
    session, state = _bridge_with_queued_reply()
    session._end_turn = AsyncMock()

    asyncio.run(session._handle_message({"type": "bridge.reset"}))

    session._end_turn.assert_not_called()
    assert state.pipeline.normal_queue.qsize() == 0
    assert {5, 6, 7} <= state.pipeline.canceled_ids
    assert state.interrupt.reset_frame == 41
    assert not session.turn_open
    assert session.flushed_response_id == "resp-old"


def test_session_end_flushes_once_across_reset_and_disconnect():
    session, state = _bridge_with_queued_reply()

    asyncio.run(session._handle_message({"type": "bridge.reset"}))
    seq = state.pipeline.flush_seq
    session._end_session("disconnect")

    assert session.flushes == 1
    assert state.pipeline.flush_seq == seq


def test_speak_waits_for_a_speech_frame_at_a_forward_loop_boundary():
    controller = make_controller()
    controller.observe_model_mode("speak")
    assert controller.next_idle_frame(4).base == "listen"
    assert controller.next_frame(5, speech=True).base == "listen"
    speaking = controller.next_frame(8, speech=True)
    assert speaking.variant_id == "speak-1"
    assert speaking.source_frame(8) == 0


def test_new_source_starts_at_zero_and_flush_discards_unplayed_switch():
    controller = make_controller()
    controller.observe_model_mode("speak")
    assert controller.next_frame(4, speech=True).variant_id == "speak-1"
    controller.observe_model_mode("listen")
    listening = controller.next_idle_frame(8)
    assert listening.variant_id == "listen-2"
    assert listening.source_frame(8) == 0
    controller.rewind(8)
    assert controller.snapshot_for_segment().variant_id == "speak-1"
    assert controller.next_idle_frame(8).variant_id == "listen-2"


def test_hold_keeps_clip_phase_and_rewind_discards_unplayed_hold():
    controller = make_controller()
    assert controller.next_idle_frame(3).source_frame(3) == 3
    controller.observe_model_mode("speak")
    held = controller.hold_frame(4)
    assert held.source_frame(4) == 3
    assert controller.next_frame(5, speech=True).source_frame(5) == 0
    controller.rewind(4)
    assert controller.next_frame(4, speech=True).base == "speak"


def test_stream_registration_preserves_explicit_base_snapshot(tmp_path):
    state = AppState()
    state.args = SimpleNamespace(
        silence_threshold_dbfs=-40, silence_merge_gap_frames=0, enable_vad=False
    )
    state.ingest.output_audio_dir = tmp_path
    controller = make_controller()
    snapshot = controller.snapshot_for_segment()
    segment = SegmentManager(state).register_samples(
        np.full(4000, 0.2, dtype=np.float32), 16000, "stream.wav",
        base_snapshot=snapshot,
    )
    assert segment is not None
    assert segment.base_snapshot == snapshot


def test_live_worker_emits_speech_without_waiting_and_switches_inside_window(monkeypatch, tmp_path):
    state = AppState()
    state.args = SimpleNamespace(
        idle_prebuffer_frames=2, prebuffer=3, prebuffer_answer_start_only=True,
        hold_max_frames=0, silence_threshold_dbfs=-40,
        silence_merge_gap_frames=0, enable_vad=False,
    )
    state.ingest.output_audio_dir = tmp_path
    listen = SimpleNamespace(frame_list_cycle=["L0", "L1", "L2", "L3"])
    speak = SimpleNamespace(frame_list_cycle=["S0", "S1", "S2"])
    state.model.avatar = listen
    controller = AvatarBaseController([
        AvatarBaseBundle("listen", "listen-1", 4, listen),
        AvatarBaseBundle("speak", "speak-1", 3, speak),
    ])
    state.model.avatar_base_controller = controller
    state.pipeline.ack_frame = 2
    stop = threading.Event()
    rows = []
    segments = SegmentManager(state)

    def put(_queue, _loop, item, _stop):
        if item[0] == "raw":
            _, kind, sid, pts, index, _, _, _, snapshot = item
            rows.append((kind, sid, pts, index, snapshot))
            if index == 2:
                controller.observe_model_mode("speak")
                segments.register_samples(
                    np.full(4000, 0.2, dtype=np.float32), 16000, "stream.wav",
                )
        elif item[0] == "segment_end" or item[0] == "error":
            stop.set()
        return True

    def infer(segment, _frame_bases):
        for i in range(segment.total_frames):
            yield "generated", segment.global_start_frame + i, None

    monkeypatch.setattr(workers, "put_blocking", put)
    monkeypatch.setattr(workers.InferencePipeline, "iter_segment", lambda self, *a, **kw: infer(*a, **kw))
    workers.live_infer_worker(state, None, None, stop)

    assert [(kind, sid, pts, i) for kind, sid, pts, i, _ in rows[4:9]] == [
        (workers.FRAME_IDLE, workers.IDLE_SEGMENT_ID, 166667, 4),
        (workers.FRAME_GENERATED, 0, 208333, 5),
        (workers.FRAME_GENERATED, 0, 250000, 6),
        (workers.FRAME_GENERATED, 0, 291667, 7),
        (workers.FRAME_GENERATED, 0, 333333, 8),
    ]
    assert [snapshot.base for *_, snapshot in rows] == ["listen"] * 8 + ["speak"] * 3
    assert rows[8][-1].source_frame(8) == 0
