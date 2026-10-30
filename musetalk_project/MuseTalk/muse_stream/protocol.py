"""Binary frame protocol and shared timeline helpers."""

from __future__ import annotations

import struct


FRAME_MAGIC = b"MTF1"
FRAME_HEADER = struct.Struct(">4sB3xIqI")
FRAME_IDLE = 0
FRAME_GENERATED = 1
FRAME_SILENT = 2
IDLE_SEGMENT_ID = 0xFFFFFFFF


def frame_pts_us(frame_index: int, fps: int) -> int:
    """Return the global PTS for a video frame on the shared timeline."""
    return int(round(frame_index * 1_000_000 / fps))


def pack_frame(
    frame_kind: int,
    segment_id: int,
    pts_us: int,
    frame_index: int,
    jpeg: bytes,
) -> bytes:
    header = FRAME_HEADER.pack(
        FRAME_MAGIC,
        frame_kind,
        segment_id,
        pts_us,
        frame_index,
    )
    return header + jpeg
