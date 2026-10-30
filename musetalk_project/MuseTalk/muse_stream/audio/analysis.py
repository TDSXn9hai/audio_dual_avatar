"""Speech analysis for complete audio segments."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from .io import read_audio_samples


def build_speech_mask(
    samples: np.ndarray,
    sample_rate: int,
    fps: int,
    threshold_dbfs: float,
) -> np.ndarray:
    """Map PCM sample windows to video frames using the audio sample clock.

    Frame ``k`` covers samples ``[k * sample_rate / fps,
    (k + 1) * sample_rate / fps)``. A frame is marked as speech when its
    window RMS is at or above the configured dBFS threshold.
    """
    total_frames = max(1, int(math.floor(len(samples) * fps / sample_rate)))
    mask = np.zeros(total_frames, dtype=bool)
    for frame_index in range(total_frames):
        start = int(round(frame_index * sample_rate / fps))
        end = int(round((frame_index + 1) * sample_rate / fps))
        window = samples[start:min(end, len(samples))]
        if window.size == 0:
            continue
        rms = float(np.sqrt(np.mean(np.square(window, dtype=np.float64))))
        dbfs = 20.0 * math.log10(max(rms, 1e-12))
        mask[frame_index] = dbfs >= threshold_dbfs
    return mask


def merge_short_speech_gaps(mask: np.ndarray, max_gap: int) -> np.ndarray:
    """Bridge very short dips so VAD does not fragment UNet batches."""
    if max_gap <= 0:
        return mask.copy()
    merged = mask.copy()
    indices = np.flatnonzero(merged)
    for left, right in zip(indices[:-1], indices[1:]):
        gap_frames = int(right - left - 1)
        if 0 < gap_frames <= max_gap:
            merged[left + 1 : right] = True
    return merged


def pause_near_boundary(
    speech_mask: np.ndarray,
    boundary_index: int,
    window_frames: int,
) -> bool:
    """Return whether a silence frame exists around a segment boundary.

    ``window_frames`` is the number of frames inspected on each side of the
    boundary. A value of 0 disables pause-boundary detection.
    """
    if window_frames <= 0:
        return False
    mask = np.asarray(speech_mask, dtype=bool)
    if mask.size == 0:
        return False
    left = max(0, int(boundary_index) - int(window_frames))
    right = min(mask.size, int(boundary_index) + int(window_frames))
    if left >= right:
        return False
    return bool(np.any(~mask[left:right]))


def analyze_audio_file(
    path: Path,
    fps: int,
    threshold_dbfs: float,
    merge_gap_frames: int = 3,
    enable_vad: bool = False,
) -> tuple[float, int, int, int, np.ndarray]:
    samples, sample_rate = read_audio_samples(path)
    return analyze_audio_samples(
        samples,
        sample_rate,
        fps,
        threshold_dbfs,
        merge_gap_frames,
        enable_vad,
    )


def analyze_audio_samples(
    samples: np.ndarray,
    sample_rate: int,
    fps: int,
    threshold_dbfs: float,
    merge_gap_frames: int = 3,
    enable_vad: bool = False,
) -> tuple[float, int, int, int, np.ndarray]:
    samples = np.asarray(samples, dtype=np.float32)
    sample_count = int(len(samples))
    duration = sample_count / float(sample_rate)
    total_frames = max(1, int(math.floor(sample_count * fps / sample_rate)))
    if enable_vad:
        speech_mask = merge_short_speech_gaps(
            build_speech_mask(
                samples,
                sample_rate,
                fps,
                threshold_dbfs,
            ),
            merge_gap_frames,
        )
    else:
        # Full-frame inference preserves the mouth-closing tail after speech.
        speech_mask = np.ones(total_frames, dtype=bool)
    return duration, total_frames, sample_count, sample_rate, speech_mask
