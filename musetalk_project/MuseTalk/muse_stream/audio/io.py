"""Audio file conversion, trimming and sample loading."""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

from ..log import log


def read_audio_samples(path: Path) -> tuple[np.ndarray, int]:
    import soundfile as sf

    samples, sample_rate = sf.read(str(path), dtype="float32", always_2d=False)
    if samples.ndim > 1:
        samples = samples.mean(axis=1)
    return np.asarray(samples, dtype=np.float32), int(sample_rate)


def convert_audio(src: Path, dst: Path) -> bool:
    cmd = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-i",
        str(src),
        "-ar",
        "16000",
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        str(dst),
    ]
    try:
        result = subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120
        )
    except Exception as exc:
        log(f"audio conversion failed for {src}: {exc}")
        return False
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        log(f"audio conversion failed for {src}: {detail[:300]}")
        return False
    return dst.exists()


def trim_audio(src: Path, dst: Path, offset: float, duration: float) -> bool:
    cmd = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-ss",
        f"{offset:.3f}",
        "-i",
        str(src),
        "-t",
        f"{duration:.3f}",
        "-ar",
        "16000",
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        str(dst),
    ]
    try:
        result = subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120
        )
    except Exception as exc:
        log(f"audio trim failed for {src}: {exc}")
        return False
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        log(f"audio trim failed for {src}: {detail[:300]}")
        return False
    return dst.exists()
