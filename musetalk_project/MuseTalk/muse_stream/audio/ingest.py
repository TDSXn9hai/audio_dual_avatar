"""Directory-backed audio ingestion and idle lifecycle handling."""

from __future__ import annotations

import time
import traceback
from pathlib import Path
from typing import Callable

from ..log import log
from ..state import AppState
from .segments import InterruptManager, SegmentManager


class DirectoryAudioIngestor:
    """Watch complete audio files and feed them into the segment pipeline."""

    def __init__(
        self,
        state: AppState,
        on_idle_exit: Callable[[str], None],
    ) -> None:
        self.state = state
        self.on_idle_exit = on_idle_exit
        self.segments = SegmentManager(state)
        self.interrupts = InterruptManager(state)

    def scan(self) -> None:
        state = self.state
        args = state.args
        assert args is not None
        audio_dir = Path(args.audio_dir)
        if not audio_dir.exists():
            audio_dir.mkdir(parents=True, exist_ok=True)
            return

        now = time.monotonic()
        candidates = []
        for path in audio_dir.iterdir():
            if not path.is_file() or path.suffix.lower() not in args.extensions:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            key = (str(path), stat.st_mtime_ns, stat.st_size)
            if key in state.ingest.seen_keys:
                continue
            candidates.append(
                (stat.st_mtime_ns, path.name, path, stat.st_size, key)
            )

        for _, _, path, size, key in sorted(candidates):
            pending = state.ingest.pending.get(str(path))
            if pending is None or pending[0] != size:
                state.ingest.last_audio_seen = now
                if state.interrupt.active:
                    # Give the writer/converter a moment to finish before the
                    # resume deadline can fire; success cancels the interrupt.
                    state.interrupt.deadline = max(
                        state.interrupt.deadline,
                        now + args.file_stable_seconds + 1.0,
                    )
                state.ingest.pending[str(path)] = (size, now)
                continue
            if now - pending[1] < args.file_stable_seconds:
                continue

            if self.segments.register_file(path):
                state.ingest.seen_keys.add(key)
                state.ingest.pending.pop(str(path), None)
            else:
                state.ingest.pending[str(path)] = (size, now)

    def run(self) -> None:
        state = self.state
        args = state.args
        assert args is not None
        initial_scan_done = False

        while not state.service.exit_requested:
            try:
                if not initial_scan_done and not args.process_existing:
                    for path in Path(args.audio_dir).iterdir():
                        if (
                            path.is_file()
                            and path.suffix.lower() in args.extensions
                        ):
                            stat = path.stat()
                            state.ingest.seen_keys.add(
                                (str(path), stat.st_mtime_ns, stat.st_size)
                            )
                    log(
                        "process_existing=false: skipped files already in audio dir"
                    )
                initial_scan_done = True
                self.scan()
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
