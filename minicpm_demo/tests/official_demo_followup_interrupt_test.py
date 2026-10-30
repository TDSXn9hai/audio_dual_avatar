#!/usr/bin/env python3
"""Test follow-up and force-listen barge-in on the official demo WebSocket API."""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import ssl
import time
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf
import websockets
from decord import VideoReader, cpu
from PIL import Image


IN_RATE = 16000
OUT_RATE = 24000
CHUNK_SAMPLES = 16000
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_audio_chunks(path: Path) -> list[str]:
    samples, _ = librosa.load(str(path), sr=IN_RATE, mono=True)
    chunks = []
    for start in range(0, len(samples), CHUNK_SAMPLES):
        chunk = samples[start : start + CHUNK_SAMPLES].astype(np.float32, copy=False)
        if len(chunk) < CHUNK_SAMPLES:
            chunk = np.pad(chunk, (0, CHUNK_SAMPLES - len(chunk)))
        chunks.append(base64.b64encode(chunk.astype("<f4").tobytes()).decode("ascii"))
    return chunks


def load_video_frames(path: Path) -> list[str]:
    reader = VideoReader(str(path), ctx=cpu(0))
    fps = reader.get_avg_fps()
    frame_count = max(1, int(np.ceil(len(reader) / fps)))
    indices = [min(int(i * fps), len(reader) - 1) for i in range(frame_count)]
    frames = reader.get_batch(indices).asnumpy()
    encoded = []
    for frame in frames:
        image = Image.fromarray(frame.astype(np.uint8)).convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=88)
        encoded.append(base64.b64encode(buffer.getvalue()).decode("ascii"))
    return encoded


def decode_audio(value: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(value), dtype="<f4").astype(np.float32, copy=True)


class TurnState:
    def __init__(self, name: str):
        self.name = name
        self.started = time.perf_counter()
        self.first_text_s = None
        self.first_audio_s = None
        self.first_audio_duration_s = None
        self.text_parts: list[str] = []
        self.audio_chunks: list[np.ndarray] = []
        self.events: list[dict] = []
        self.listen_count = 0
        self.speech_started = False
        self.first_audio_event = asyncio.Event()
        self.done = asyncio.Event()
        self.force_listen_sent_at = None
        self.forced_listen_at = None
        self.forced_listen_event = asyncio.Event()


class DemoSession:
    def __init__(self, ws, stream_start: float):
        self.ws = ws
        self.stream_start = stream_start
        self.frames = []
        self.frame_index = 0
        self.turn: TurnState | None = None
        self.stop_receiver = asyncio.Event()
        self.receiver_task = None

    def set_frames(self, frames: list[str]):
        self.frames = frames

    def begin_turn(self, name: str) -> TurnState:
        self.turn = TurnState(name)
        return self.turn

    async def _send(self, audio_b64: str, *, force_listen: bool = False):
        payload = {
            "type": "input.append",
            "input": {
                "audio": audio_b64,
                "max_slice_nums": 1,
            },
        }
        if self.frames:
            payload["input"]["video_frames"] = [
                self.frames[self.frame_index % len(self.frames)]
            ]
            self.frame_index += 1
        if force_listen:
            payload["input"]["force_listen"] = True
        await self.ws.send(json.dumps(payload))

    async def send_audio(self, audio_path: Path, *, force_first: bool = False):
        chunks = load_audio_chunks(audio_path)
        anchor = time.perf_counter()
        for index, chunk in enumerate(chunks):
            delay = anchor + index - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            await self._send(chunk, force_listen=force_first and index == 0)
        return time.perf_counter() - anchor

    async def keepalive(self, until: asyncio.Event):
        silence = base64.b64encode(
            np.zeros(CHUNK_SAMPLES, dtype="<f4").tobytes()
        ).decode("ascii")
        next_tick = time.perf_counter()
        while not until.is_set():
            await self._send(silence)
            next_tick += 1.0
            await asyncio.sleep(max(0.0, next_tick - time.perf_counter()))

    async def receiver(self):
        while not self.stop_receiver.is_set():
            try:
                message = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=30))
            except asyncio.TimeoutError:
                break
            turn = self.turn
            if turn is None:
                continue
            now = time.perf_counter()
            relative_turn = now - turn.started
            record = {
                "t_s": round(relative_turn, 4),
                "type": message.get("type"),
                "kind": message.get("kind"),
                "text": message.get("text"),
                "audio_b64_chars": len(message.get("audio", "") or ""),
                "end_of_turn": bool(message.get("end_of_turn")),
                "metrics": message.get("metrics"),
            }
            turn.events.append(record)

            if message.get("type") != "response.output.delta":
                if message.get("type") in ("session.closed", "error"):
                    break
                continue

            kind = message.get("kind")
            if kind == "listen":
                turn.listen_count += 1
                if turn.force_listen_sent_at is not None and not turn.forced_listen_event.is_set():
                    turn.forced_listen_at = round(now - turn.force_listen_sent_at, 4)
                    turn.forced_listen_event.set()
                if turn.speech_started:
                    turn.done.set()
            elif kind == "text":
                text = message.get("text") or ""
                if text:
                    turn.speech_started = True
                    turn.text_parts.append(text)
                    if turn.first_text_s is None:
                        turn.first_text_s = round(relative_turn, 4)
            elif kind == "audio":
                audio = message.get("audio")
                if audio:
                    samples = decode_audio(audio)
                    turn.speech_started = True
                    turn.audio_chunks.append(samples)
                    if turn.first_audio_s is None:
                        turn.first_audio_s = round(relative_turn, 4)
                        turn.first_audio_duration_s = round(len(samples) / OUT_RATE, 4)
                        turn.first_audio_event.set()

    def start_receiver(self):
        self.receiver_task = asyncio.create_task(self.receiver())

    async def close(self):
        self.stop_receiver.set()
        if self.receiver_task:
            self.receiver_task.cancel()
            try:
                await self.receiver_task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await self.ws.send(json.dumps({"type": "session.close", "reason": "test_done"}))
            await asyncio.sleep(0.2)
        except Exception:
            pass


async def initialize(ws, system_prompt: str) -> str:
    while True:
        message = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
        if message.get("type") in ("queue_done", "session.queue_done"):
            await ws.send(
                json.dumps(
                    {
                        "type": "session.init",
                        "payload": {
                            "system_prompt": system_prompt,
                            "config": {"length_penalty": 1.1},
                            "max_slice_nums": 1,
                            "use_tts": True,
                        },
                    },
                    ensure_ascii=False,
                )
            )
        elif message.get("type") == "session.created":
            return str(message.get("session_id"))
        elif message.get("type") == "error":
            raise RuntimeError(str(message))


def summarize_turn(turn: TurnState, output_path: Path | None = None) -> dict:
    result = {
        "name": turn.name,
        "first_text_s": turn.first_text_s,
        "first_audio_s": turn.first_audio_s,
        "first_audio_duration_s": turn.first_audio_duration_s,
        "speech_started": turn.speech_started,
        "listen_count": turn.listen_count,
        "forced_listen_sent": turn.force_listen_sent_at is not None,
        "forced_listen_latency_s": turn.forced_listen_at,
        "generated_text": "".join(turn.text_parts).strip(),
        "events": turn.events,
    }
    if turn.audio_chunks:
        audio = np.concatenate(turn.audio_chunks)
        result["audio_duration_s"] = round(len(audio) / OUT_RATE, 3)
        result["audio_peak"] = round(float(np.max(np.abs(audio))), 4)
        result["audio_rms_dbfs"] = round(
            20 * np.log10(max(float(np.sqrt(np.mean(audio * audio))), 1e-12)), 2
        )
        result["clipping_ratio"] = round(
            float(np.mean(np.abs(audio) >= 0.999)), 6
        )
        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            sf.write(output_path, audio, OUT_RATE)
            result["output_audio"] = str(output_path.resolve())
    return result


async def run_followup(args, frames):
    async with websockets.connect(args.url, ssl=ssl._create_unverified_context(), max_size=128 * 1024 * 1024) as ws:
        sid = await initialize(ws, args.system_prompt)
        session = DemoSession(ws, time.perf_counter())
        session.set_frames(frames)
        session.start_receiver()

        turn1 = session.begin_turn("followup_turn_1")
        audio_task = asyncio.create_task(session.send_audio(args.question1))
        await audio_task
        keepalive = asyncio.create_task(session.keepalive(turn1.done))
        await asyncio.wait_for(turn1.done.wait(), timeout=args.turn_timeout)
        keepalive.cancel()
        await asyncio.gather(keepalive, return_exceptions=True)

        turn2 = session.begin_turn("followup_turn_2")
        audio_task = asyncio.create_task(session.send_audio(args.question2))
        await audio_task
        keepalive = asyncio.create_task(session.keepalive(turn2.done))
        await asyncio.wait_for(turn2.done.wait(), timeout=args.turn_timeout)
        keepalive.cancel()
        await asyncio.gather(keepalive, return_exceptions=True)

        await session.close()
        return {
            "session_id": sid,
            "turns": [
                summarize_turn(turn1, args.output_dir / "followup_turn_1.wav"),
                summarize_turn(turn2, args.output_dir / "followup_turn_2.wav"),
            ],
        }


async def run_interrupt(args, frames):
    async with websockets.connect(args.url, ssl=ssl._create_unverified_context(), max_size=128 * 1024 * 1024) as ws:
        sid = await initialize(ws, args.system_prompt)
        session = DemoSession(ws, time.perf_counter())
        session.set_frames(frames)
        session.start_receiver()

        first = session.begin_turn("interrupted_turn")
        q1_chunks = load_audio_chunks(args.question1)
        anchor = time.perf_counter()
        for index, chunk in enumerate(q1_chunks):
            delay = anchor + index - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            await session._send(chunk)

        keepalive = asyncio.create_task(session.keepalive(first.first_audio_event))
        await asyncio.wait_for(first.first_audio_event.wait(), timeout=args.turn_timeout)
        keepalive.cancel()
        await asyncio.gather(keepalive, return_exceptions=True)

        q3_chunks = load_audio_chunks(args.question3)
        first.force_listen_sent_at = time.perf_counter()
        await session._send(q3_chunks[0], force_listen=True)
        await asyncio.wait_for(first.forced_listen_event.wait(), timeout=10)
        interrupted_result = summarize_turn(
            first, args.output_dir / "interrupted_before_force_listen.wav"
        )

        response = session.begin_turn("interrupt_response")
        interrupt_anchor = time.perf_counter()
        for index, chunk in enumerate(q3_chunks[1:], start=1):
            delay = interrupt_anchor + (index - 1) - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            await session._send(chunk)
        keepalive = asyncio.create_task(session.keepalive(response.done))
        await asyncio.wait_for(response.done.wait(), timeout=args.turn_timeout)
        keepalive.cancel()
        await asyncio.gather(keepalive, return_exceptions=True)

        await session.close()
        return {
            "session_id": sid,
            "interrupted_turn": interrupted_result,
            "interrupt_response": summarize_turn(
                response, args.output_dir / "interrupt_response.wav"
            ),
        }


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="wss://127.0.0.1:8006/v1/realtime?mode=video")
    parser.add_argument(
        "--question1",
        default=str(PROJECT_ROOT / "sample_data" / "video_question.wav"),
    )
    parser.add_argument(
        "--question2",
        default=str(PROJECT_ROOT / "sample_data" / "video_followup_1.wav"),
    )
    parser.add_argument(
        "--question3",
        default=str(PROJECT_ROOT / "sample_data" / "video_followup_2.wav"),
    )
    parser.add_argument(
        "--video",
        default=str(PROJECT_ROOT / "sample_data" / "Skiing.mp4"),
    )
    parser.add_argument(
        "--output-dir",
        default=str(PROJECT_ROOT / "tests" / "results" / "official_demo_followup_interrupt"),
    )
    parser.add_argument(
        "--system-prompt",
        default="Streaming Omni Conversation. 请使用中文自然、简洁地回答用户关于视频的问题。",
    )
    parser.add_argument("--turn-timeout", type=float, default=25.0)
    parser.add_argument("--mode", choices=["all", "followup", "interrupt"], default="all")
    return parser.parse_args()


async def main():
    args = parse_args()
    args.output_dir = Path(args.output_dir)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    frames = load_video_frames(Path(args.video))
    result = {}
    if args.mode in ("all", "followup"):
        result["followup"] = await run_followup(args, frames)
        await asyncio.sleep(2)
    if args.mode in ("all", "interrupt"):
        result["interrupt"] = await run_interrupt(args, frames)
    output_name = "result.json" if args.mode == "all" else f"result_{args.mode}.json"
    (args.output_dir / output_name).write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
