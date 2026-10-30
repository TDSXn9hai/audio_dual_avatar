"""WebSocket routes for streaming audio input."""

from __future__ import annotations

from fastapi import APIRouter, WebSocket

from ..audio.minicpm import MiniCPMBridgeSession
from ..audio.stream import StreamAudioSession


router = APIRouter()


@router.websocket("/ws/audio")
async def audio_input(websocket: WebSocket) -> None:
    session = StreamAudioSession(
        websocket.app.state.stream_state,
        websocket,
    )
    await session.run()


@router.websocket("/ws/minicpm")
async def minicpm_input(websocket: WebSocket) -> None:
    session = MiniCPMBridgeSession(
        websocket.app.state.stream_state,
        websocket,
    )
    await session.run()
