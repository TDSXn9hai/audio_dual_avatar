"""Idle-exit monitor shared by input modes without directory scanning."""

from __future__ import annotations

import time
import traceback
from typing import Callable

from ..log import log
from ..state import AppState
from .segments import InterruptManager, SegmentManager


class IdleExitMonitor:
    """Maintain interrupt timers and stop an otherwise idle stream."""

    def __init__(
        self,
        state: AppState,
        on_idle_exit: Callable[[str], None],
    ) -> None:
        self.state = state
        self.on_idle_exit = on_idle_exit
        self.segments = SegmentManager(state)
        self.interrupts = InterruptManager(state)

    def run(self) -> None:
        state = self.state
        args = state.args
        assert args is not None
        while not state.service.exit_requested:
            try:
                self.interrupts.finish_if_due()
                now = time.monotonic()
                with state.pipeline.lock:
                    generating = state.pipeline.generating > 0
                    interrupt_active = state.interrupt.active
                queued = self.segments.queued_count() > 0

                if state.viewer.connected:
                    if state.viewer.playback_started:
                        reference = max(
                            state.ingest.last_audio_seen,
                            state.viewer.last_ack_time,
                        )
                    else:
                        reference = max(
                            state.ingest.last_audio_seen,
                            state.viewer.connect_time,
                        )
                else:
                    reference = max(
                        state.ingest.last_audio_seen,
                        state.service.ready_time,
                    )

                should_exit = (
                    now - reference >= args.idle_timeout_seconds
                    and not queued
                    and not generating
                )
                if interrupt_active:
                    should_exit = False
                if should_exit:
                    self.on_idle_exit(
                        f"no new audio and no playback activity for "
                        f"{args.idle_timeout_seconds:.0f}s"
                    )
                    return
            except Exception:
                log(traceback.format_exc())
            time.sleep(args.poll_interval_seconds)
