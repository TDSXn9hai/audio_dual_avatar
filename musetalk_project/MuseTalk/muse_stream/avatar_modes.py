"""Per-session base-video mode scheduling."""

from __future__ import annotations

from dataclasses import dataclass, replace
import random
import threading
from typing import Iterable


@dataclass(frozen=True)
class AvatarBaseBundle:
    """One preprocessed source-video bundle."""

    base: str
    variant_id: str
    cycle_length: int
    avatar: object | None = None

    def __post_init__(self) -> None:
        if self.base not in {"listen", "speak"}:
            raise ValueError(f"unsupported avatar base: {self.base}")
        if not self.variant_id:
            raise ValueError("avatar variant_id is required")
        if self.cycle_length < 1:
            raise ValueError("avatar cycle_length must be positive")


@dataclass(frozen=True)
class AvatarBaseSnapshot:
    """Immutable resource identity captured by a media segment or frame."""

    base: str
    variant_id: str
    revision: int
    cycle_length: int
    bundle: AvatarBaseBundle
    start_frame: int = 0

    def source_frame(self, frame_index: int) -> int:
        return (frame_index - self.start_frame) % self.cycle_length


class AvatarBaseController:
    """Apply base changes only at complete source-cycle boundaries.

    At every boundary the next clip is drawn at random from the target base's
    pool, so a state does not keep looping one short clip. The clip that just
    played (or last played in that base) is excluded when the pool allows it.
    """

    # Bounded so a long session does not grow the switch history forever;
    # rewinds only ever reach back a few seconds of unplayed frames.
    _MAX_HISTORY = 512

    def __init__(
        self,
        bundles: Iterable[AvatarBaseBundle],
        *,
        initial_base: str = "listen",
        rng: random.Random | None = None,
    ) -> None:
        values = tuple(bundles)
        if not values:
            raise ValueError("at least one avatar base bundle is required")
        self._bundles = values
        self._by_base: dict[str, tuple[AvatarBaseBundle, ...]] = {
            "listen": tuple(item for item in values if item.base == "listen"),
            "speak": tuple(item for item in values if item.base == "speak"),
        }
        if not self._by_base.get(initial_base):
            raise ValueError(f"no avatar bundle configured for {initial_base}")
        self._lock = threading.RLock()
        self._rng = rng or random.Random()
        self._active = self._by_base[initial_base][0]
        self._model_mode = initial_base
        self._revision = 1
        self._history = [(0, self._snapshot(0))]

    @property
    def active_base(self) -> str:
        with self._lock:
            return self._active.base

    @property
    def pending_base(self) -> str | None:
        with self._lock:
            return self._model_mode if self._model_mode != self._active.base else None

    @property
    def revision(self) -> int:
        with self._lock:
            return self._history[-1][1].revision

    def observe_model_mode(self, mode: str) -> None:
        """Record the latest MiniCPM mode without delaying audio."""
        if mode not in {"listen", "speak"}:
            raise ValueError(f"unsupported model mode: {mode}")
        with self._lock:
            self._model_mode = mode

    def snapshot_for_segment(self) -> AvatarBaseSnapshot:
        """Read the active source without advancing the media timeline."""
        with self._lock:
            return self._history[-1][1]

    def next_idle_frame(self, frame_index: int) -> AvatarBaseSnapshot:
        """Return the source bundle for an idle frame on the global timeline."""
        return self.next_frame(frame_index)

    def next_frame(self, frame_index: int, *, speech: bool = False) -> AvatarBaseSnapshot:
        """Select the base for a committed frame, in output order."""
        if frame_index < 0:
            raise ValueError("frame_index must be non-negative")
        with self._lock:
            current = self._history[-1][1]
            if not (
                frame_index > current.start_frame
                and current.source_frame(frame_index) == 0
            ):
                return current
            target = self._model_mode
            if (
                target != current.base
                and self._by_base[target]
                and (target != "speak" or speech)
            ):
                self._activate(target, frame_index)
            elif len(self._by_base[current.base]) > 1:
                # Staying in the same state: move to another clip of the pool.
                self._activate(current.base, frame_index)
            return self._history[-1][1]

    def hold_frame(self, frame_index: int) -> AvatarBaseSnapshot:
        """Repeat the previous source frame without advancing the clip."""
        with self._lock:
            current = self._history[-1][1]
            held = replace(current, start_frame=current.start_frame + 1)
            self._history.append((frame_index, held))
            self._trim_history()
            return held

    def rewind(self, next_frame: int) -> None:
        """Discard switches and holds in frames removed by a playback reset."""
        with self._lock:
            while len(self._history) > 1 and self._history[-1][0] >= next_frame:
                self._history.pop()
            self._active = self._history[-1][1].bundle

    def _snapshot(self, start_frame: int) -> AvatarBaseSnapshot:
        return AvatarBaseSnapshot(
            base=self._active.base,
            variant_id=self._active.variant_id,
            revision=self._revision,
            cycle_length=self._active.cycle_length,
            bundle=self._active,
            start_frame=start_frame,
        )

    def _last_played(self, base: str) -> AvatarBaseBundle | None:
        for _, snapshot in reversed(self._history):
            if snapshot.base == base:
                return snapshot.bundle
        return None

    def _activate(self, base: str, frame_index: int) -> None:
        variants = self._by_base[base]
        last = self._last_played(base)
        candidates = [item for item in variants if item is not last] or list(variants)
        self._active = self._rng.choice(candidates)
        self._revision += 1
        self._history.append((frame_index, self._snapshot(frame_index)))
        self._trim_history()

    def _trim_history(self) -> None:
        if len(self._history) > self._MAX_HISTORY:
            del self._history[: len(self._history) - self._MAX_HISTORY]
