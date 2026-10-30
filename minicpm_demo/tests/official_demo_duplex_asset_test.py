#!/usr/bin/env python3
"""Official demo full-duplex test using local video frames and an existing speech question."""

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


SAMPLE_RATE_IN = 16000
SAMPLE_RATE_OUT = 24000
CHUNK_SAMPLES = 16000
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_frames(video_path: Path, fps: int = 1) -> list[str]:
    reader = VideoReader(str(video_path), ctx=cpu(0))
    video_fps = reader.get_avg_fps()
    duration = len(reader) / video_fps
    indices = [min(int(i * video_fps), len(reader) - 1) for i in range(int(np.ceil(duration)))]
    frames = reader.get_batch(indices).asnumpy()
    encoded = []
    for frame in frames:
        image = Image.fromarray(frame.astype(np.uint8)).convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=88)
        encoded.append(base64.b64encode(buffer.getvalue()).decode("ascii"))
    return encoded


def load_audio_chunks(audio_path: Path, chunk_ms: int = 1000) -> list[str]:
    samples, _ = librosa.load(str(audio_path), sr=SAMPLE_RATE_IN, mono=True)
    chunk_samples = int(SAMPLE_RATE_IN * chunk_ms / 1000)
    chunks = []
    for start in range(0, len(samples), chunk_samples):
        chunk = samples[start : start + chunk_samples].astype(np.float32, copy=False)
        if len(chunk) < chunk_samples:
            chunk = np.pad(chunk, (0, chunk_samples - len(chunk)))
        chunks.append(base64.b64encode(chunk.astype("<f4").tobytes()).decode("ascii"))
    return chunks


def decode_audio_b64(payload: str) -> np.ndarray:
    raw = base64.b64decode(payload)
    return np.frombuffer(raw, dtype="<f4").astype(np.float32, copy=True)


async def connect_and_init(
    ws,
    system_prompt: str,
    force_listen_count: int | None = None,
) -> tuple[str, float]:
    started = time.perf_counter()
    init_sent = False
    while True:
        message = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
        message_type = message.get("type")
        if message_type in ("queue_done", "session.queue_done") and not init_sent:
            config = {"length_penalty": 1.1}
            if force_listen_count is not None:
                config["force_listen_count"] = force_listen_count
            await ws.send(
                json.dumps(
                    {
                        "type": "session.init",
                        "payload": {
                            "system_prompt": system_prompt,
                            "config": config,
                            "max_slice_nums": 1,
                            "use_tts": True,
                        },
                    },
                    ensure_ascii=False,
                )
            )
            init_sent = True
            continue
        if message.get("type") == "session.created":
            return str(message.get("session_id")), time.perf_counter() - started
        if message.get("type") == "error":
            raise RuntimeError(f"session init failed: {message}")


async def run(args):
    audio_chunks = load_audio_chunks(args.audio)
    video_frames = load_frames(args.video)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    audio_chunks_out: list[np.ndarray] = []
    text_parts: list[str] = []
    events: list[dict] = []
    listen_count = 0
    speak_text_count = 0
    speak_audio_count = 0
    first_text_s = None
    first_audio_s = None
    first_audio_chunk_s = None
    end_of_turn_seen = asyncio.Event()
    all_user_sent = asyncio.Event()
    stop_receiving = asyncio.Event()
    stream_start = None

    ssl_context = ssl._create_unverified_context() if args.insecure else None
    async with websockets.connect(
        args.url,
        ssl=ssl_context,
        max_size=128 * 1024 * 1024,
        open_timeout=30,
    ) as ws:
        session_id, init_seconds = await connect_and_init(
            ws,
            args.system_prompt,
            force_listen_count=args.force_listen_count,
        )
        stream_start = time.perf_counter()
        user_audio_end_s = len(audio_chunks)

        async def receiver():
            nonlocal listen_count, speak_text_count, speak_audio_count
            nonlocal first_text_s, first_audio_s, first_audio_chunk_s
            while not stop_receiving.is_set():
                try:
                    message = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                except asyncio.TimeoutError:
                    break
                now_s = time.perf_counter() - stream_start
                event_type = message.get("type")
                kind = message.get("kind")
                record = {
                    "t_s": round(now_s, 4),
                    "type": event_type,
                    "kind": kind,
                    "text": message.get("text"),
                    "end_of_turn": bool(message.get("end_of_turn")),
                    "audio_bytes": len(message.get("audio", "") or ""),
                    "metrics": message.get("metrics"),
                }
                events.append(record)

                if event_type == "response.output.delta" and kind == "listen":
                    listen_count += 1
                    if speak_audio_count > 0:
                        end_of_turn_seen.set()
                elif event_type == "response.output.delta" and kind == "text":
                    speak_text_count += 1
                    text = message.get("text") or ""
                    if text:
                        text_parts.append(text)
                        if first_text_s is None:
                            first_text_s = now_s
                elif event_type == "response.output.delta" and kind == "audio":
                    speak_audio_count += 1
                    audio_value = message.get("audio")
                    if audio_value:
                        samples = decode_audio_b64(audio_value)
                        audio_chunks_out.append(samples)
                        if first_audio_s is None:
                            first_audio_s = now_s
                            first_audio_chunk_s = len(samples) / SAMPLE_RATE_OUT

                if message.get("end_of_turn") and all_user_sent.is_set():
                    end_of_turn_seen.set()
                if event_type in ("session.closed", "error"):
                    end_of_turn_seen.set()
                    break

        receive_task = asyncio.create_task(receiver())

        try:
            for index in range(args.max_chunks):
                deadline = stream_start + index
                delay = deadline - time.perf_counter()
                if delay > 0:
                    await asyncio.sleep(delay)

                if index < len(audio_chunks):
                    audio_b64 = audio_chunks[index]
                else:
                    silence = np.zeros(CHUNK_SAMPLES, dtype=np.float32)
                    audio_b64 = base64.b64encode(silence.astype("<f4").tobytes()).decode("ascii")

                frame_b64 = video_frames[index % len(video_frames)] if video_frames else None
                payload = {
                    "type": "input.append",
                    "input": {
                        "audio": audio_b64,
                        "max_slice_nums": 1,
                    },
                }
                if frame_b64:
                    payload["input"]["video_frames"] = [frame_b64]
                await ws.send(json.dumps(payload))
                if index + 1 >= len(audio_chunks):
                    all_user_sent.set()
                if end_of_turn_seen.is_set():
                    break

            all_user_sent.set()
            try:
                await asyncio.wait_for(end_of_turn_seen.wait(), timeout=args.completion_timeout)
            except asyncio.TimeoutError:
                pass
        finally:
            stop_receiving.set()
            receive_task.cancel()
            try:
                await receive_task
            except (asyncio.CancelledError, Exception):
                pass
            try:
                await ws.send(json.dumps({"type": "session.close", "reason": "asset_test_done"}))
                await asyncio.sleep(0.2)
            except Exception:
                pass

    if not audio_chunks_out:
        raise RuntimeError("No audio chunks received")
    output_audio = np.concatenate(audio_chunks_out)
    output_wav = output_dir / "response.wav"
    sf.write(output_wav, output_audio, SAMPLE_RATE_OUT)

    frame_count = len(output_audio)
    peak = float(np.max(np.abs(output_audio))) if frame_count else 0.0
    rms = float(np.sqrt(np.mean(np.square(output_audio)))) if frame_count else 0.0
    result = {
        "session_id": session_id,
        "init_seconds": round(init_seconds, 4),
        "input_audio": str(Path(args.audio).resolve()),
        "input_audio_chunks": len(audio_chunks),
        "input_video": str(Path(args.video).resolve()),
        "input_video_frames": len(video_frames),
        "user_audio_end_s": user_audio_end_s,
        "events_received": len(events),
        "listen_events": listen_count,
        "text_delta_events": speak_text_count,
        "audio_delta_events": speak_audio_count,
        "first_text_s": round(first_text_s, 4) if first_text_s is not None else None,
        "first_audio_s": round(first_audio_s, 4) if first_audio_s is not None else None,
        "first_audio_after_user_end_s": (
            round(max(0.0, first_audio_s - user_audio_end_s), 4)
            if first_audio_s is not None
            else None
        ),
        "first_audio_before_user_end_s": (
            round(max(0.0, user_audio_end_s - first_audio_s), 4)
            if first_audio_s is not None
            else None
        ),
        "first_audio_chunk_duration_s": (
            round(first_audio_chunk_s, 4) if first_audio_chunk_s is not None else None
        ),
        "generated_text": "".join(text_parts).strip(),
        "output_audio": str(output_wav.resolve()),
        "output_audio_duration_s": round(len(output_audio) / SAMPLE_RATE_OUT, 3),
        "output_audio_peak": round(peak, 4),
        "output_audio_rms_dbfs": round(20 * np.log10(max(rms, 1e-12)), 2),
        "output_audio_clipping_ratio": round(float(np.mean(np.abs(output_audio) >= 0.999)), 6),
        "events": events,
    }
    (output_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="wss://127.0.0.1:8006/v1/realtime?mode=video")
    parser.add_argument(
        "--audio",
        default=str(PROJECT_ROOT / "sample_data" / "video_question.wav"),
    )
    parser.add_argument(
        "--video",
        default=str(PROJECT_ROOT / "sample_data" / "Skiing.mp4"),
    )
    parser.add_argument(
        "--output-dir",
        default=str(PROJECT_ROOT / "tests" / "results" / "official_demo_duplex"),
    )
    parser.add_argument(
        "--system-prompt",
        default="Streaming Omni Conversation. 请使用中文自然、简洁地回答用户关于视频的问题。",
    )
    parser.add_argument("--max-chunks", type=int, default=15)
    parser.add_argument("--completion-timeout", type=float, default=20.0)
    parser.add_argument(
        "--force-listen-count",
        type=int,
        default=None,
        help="Override startup forced-listen steps; 0 allows immediate proactive speaking.",
    )
    parser.add_argument("--insecure", action="store_true", default=True)
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
