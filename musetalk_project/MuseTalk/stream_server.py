#!/usr/bin/env python3
"""Compatibility entrypoint for the modular MuseTalk streaming server.

The implementation lives in :mod:`muse_stream`. This module keeps the
documented ``python stream_server.py`` and ``uvicorn stream_server:app`` entry
points working for existing deployments.
"""

from __future__ import annotations

from muse_stream.api.app import create_app
from muse_stream.domain import LiveSegment, Runtime
from muse_stream.main import run
from muse_stream.protocol import (
    FRAME_GENERATED,
    FRAME_HEADER,
    FRAME_IDLE,
    FRAME_SILENT,
    IDLE_SEGMENT_ID,
)
from muse_stream.state import AppState


__all__ = [
    "FRAME_GENERATED",
    "FRAME_HEADER",
    "FRAME_IDLE",
    "FRAME_SILENT",
    "IDLE_SEGMENT_ID",
    "LiveSegment",
    "Runtime",
    "STATE",
    "app",
    "main",
]

STATE = AppState()
app = create_app(STATE)


def main() -> None:
    run(STATE, app)


if __name__ == "__main__":
    main()
