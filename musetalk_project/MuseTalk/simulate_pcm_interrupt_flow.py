#!/usr/bin/env python3
"""Simulate upstream PCM answers and the manual interrupt acceptance flow.

The browser remains the only /ws/live viewer. This script only sends audio to
/ws/audio and polls /api/status. It expects the user to press the frontend
"打断" button when prompted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import librosa
import numpy as np
import websockets


@dataclass
class SendResult:
    path: str
    duration: float
    source_duration: float
    windows: int
    samples: int
    first_ack_delay: float
    first_segment_id: int = -1
    last_segment_id: int = -1
    send_delay_ms: float = 0.0
    interrupted: bool = False


@dataclass
class FlowReport:
    started_at: str
    base_url: str
    events: list[dict[str, Any]] = field(default_factory=list)
    sends: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    passed: bool = False

    def event(self, phase: str, message: str, **data: Any) -> None:
        item = {
            "time_s": round(time.perf_counter() - FLOW_STARTED, 3),
            "phase": phase,
            "message": message,
        }
        item.update(data)
        self.events.append(item)
        details = " ".join(f"{key}={value}" for key, value in data.items())
        suffix = f" {details}" if details else ""
        print(
            f"[{item['time_s']:8.3f}s] [{phase}] {message}{suffix}",
            flush=True,
        )


FLOW_STARTED = time.perf_counter()


class Simulator:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.base_url = args.base_url.rstrip("/") + "/"
        parts = urlsplit(self.base_url)
        ws_scheme = "wss" if parts.scheme == "https" else "ws"
        ws_path = parts.path.rstrip("/") + "/ws/audio"
        self.ws_url = urlunsplit(
            (ws_scheme, parts.netloc, ws_path, "", "")
        )
        self.status_url = self.base_url + "api/status"
        self.report = FlowReport(
            started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            base_url=self.base_url,
        )
        self.interrupt_base = 0
        self.resume_base = 0
        # 16 kHz mono PCM16: 32 bytes per millisecond.
        self.chunk_bytes = max(2, int(args.chunk_ms * 32))

    def log(self, phase: str, message: str, **data: Any) -> None:
        self.report.event(phase, message, **data)

    def fetch_status(self) -> dict[str, Any]:
        request = urllib.request.Request(
            self.status_url,
            headers={"Cache-Control": "no-store"},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.load(response)

    async def status(self) -> dict[str, Any]:
        return await asyncio.to_thread(self.fetch_status)

    async def wait_status(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        description: str,
        timeout: float,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        last_notice = 0.0
        while time.monotonic() < deadline:
            try:
                status = await self.status()
                if predicate(status):
                    return status
                now = time.monotonic()
                if now - last_notice >= 5.0:
                    print(
                        f"[WAIT] {description}; "
                        f"status={status.get('status')} "
                        f"ready={status.get('ready')}",
                        flush=True,
                    )
                    last_notice = now
            except Exception as exc:
                last_error = exc
                now = time.monotonic()
                if now - last_notice >= 5.0:
                    print(
                        f"[WAIT] 无法连接 {self.status_url}: {exc}. "
                        "请先启动服务: ./start_streaming.sh",
                        flush=True,
                    )
                    last_notice = now
            await asyncio.sleep(self.args.status_interval)
        detail = f"; last error: {last_error}" if last_error else ""
        raise TimeoutError(
            f"等待超时: {description}{detail}. "
            f"服务地址: {self.base_url}"
        )

    async def wait_ready(self) -> dict[str, Any]:
        self.log(
            "STARTUP",
            "模拟器不会启动模型服务，正在等待已有服务",
            status_url=self.status_url,
        )
        print(
            "提示：请先在另一个终端运行 ./start_streaming.sh；"
            f"如服务在虚拟机/宿主机，请确认 {self.base_url} 可访问。",
            flush=True,
        )
        status = await self.wait_status(
            lambda item: item.get("status") == "ready",
            "服务进入 ready",
            self.args.startup_timeout,
        )
        mode = status.get("input_mode")
        if mode not in ("stream", "hybrid"):
            raise RuntimeError(
                f"当前 input_mode={mode!r}，请使用 stream 或 hybrid"
            )
        if status.get("viewer_connected") and status.get("playback_started"):
            return status
        self.log(
            "PREFLIGHT",
            "请在浏览器打开直播页面并点击“进入直播”",
        )
        return await self.wait_status(
            lambda item: (
                item.get("viewer_connected")
                and item.get("playback_started")
            ),
            "浏览器观众进入直播",
            self.args.viewer_timeout,
        )

    def load_pcm(self, path: Path) -> tuple[bytes, float, int, int]:
        samples, sample_rate = librosa.load(str(path), sr=16000, mono=True)
        samples = np.asarray(samples, dtype=np.float32)
        pcm = np.clip(np.rint(samples * 32767.0), -32768, 32767).astype(
            "<i2"
        )
        return pcm.tobytes(), len(samples) / 16000.0, 16000, len(samples)

    async def send_answer(
        self,
        path: Path,
        label: str,
        max_duration: float | None = None,
        send_delay_ms: float | None = None,
        interrupt_target: int | None = None,
    ) -> SendResult:
        raw, duration, sample_rate, sample_count = await asyncio.to_thread(
            self.load_pcm,
            path,
        )
        if sample_rate != 16000:
            raise RuntimeError("PCM resampling failed")
        source_duration = duration
        if max_duration is not None and 0 < max_duration < duration:
            sample_count = min(
                sample_count,
                max(1, round(max_duration * sample_rate)),
            )
            raw = raw[: sample_count * 2]
            duration = sample_count / sample_rate
            self.log(
                "UPSTREAM",
                f"{label} 按打断测试上限截取",
                source_duration=round(source_duration, 3),
                sent_duration=round(duration, 3),
            )
        self.log(
            "UPSTREAM",
            f"{label} 开始发送",
            file=str(path),
            duration=round(duration, 3),
            source_duration=round(source_duration, 3),
            samples=sample_count,
            send_delay_ms=(
                self.args.send_delay_ms
                if send_delay_ms is None
                else send_delay_ms
            ),
        )
        before = await self.status()
        segments_before = int(before.get("total_segments", 0))
        delay_ms = (
            self.args.send_delay_ms
            if send_delay_ms is None
            else send_delay_ms
        )
        windows = 0
        acked_windows = 0
        first_ack_at: float | None = None
        interrupted = False
        cancelled = False
        done = asyncio.Event()
        receive_error: list[BaseException] = []

        async with websockets.connect(
            self.ws_url,
            max_size=None,
            open_timeout=self.args.connect_timeout,
        ) as websocket:
            meta = json.loads(await asyncio.wait_for(websocket.recv(), 10))
            if meta.get("type") != "audio_meta":
                raise RuntimeError(f"音频上行未就绪: {meta}")
            await websocket.send(
                json.dumps(
                    {
                        "type": "audio_start",
                        "sample_rate": 16000,
                        "channels": 1,
                        "format": "s16le",
                    }
                )
            )
            started = json.loads(await asyncio.wait_for(websocket.recv(), 10))
            if started.get("type") != "audio_started":
                raise RuntimeError(f"音频上行启动失败: {started}")
            sent_at = time.perf_counter()

            async def receiver() -> None:
                nonlocal windows, acked_windows, first_ack_at, cancelled
                while not done.is_set():
                    try:
                        message = await websocket.recv()
                    except websockets.ConnectionClosed:
                        if not done.is_set():
                            receive_error.append(
                                RuntimeError("音频上行连接提前关闭")
                            )
                        return
                    if isinstance(message, bytes):
                        continue
                    data = json.loads(message)
                    kind = data.get("type")
                    if kind == "audio_ack":
                        acked_windows = int(data.get("windows", 0))
                        if first_ack_at is None and acked_windows > 0:
                            first_ack_at = time.perf_counter()
                            self.log(
                                "UPSTREAM",
                                f"{label} 首个窗口已接受",
                                delay_s=round(first_ack_at - sent_at, 3),
                            )
                    elif kind in ("audio_done", "audio_cancelled"):
                        windows = int(data.get("windows", 0))
                        cancelled = kind == "audio_cancelled"
                        done.set()
                        return
                    elif kind == "error":
                        receive_error.append(
                            RuntimeError(
                                f"音频上行错误: "
                                f"{data.get('code')} {data.get('message')}"
                            )
                        )
                        done.set()
                        return

            receiver_task = asyncio.create_task(receiver())
            last_interrupt_poll = 0.0
            try:
                for offset in range(0, len(raw), self.chunk_bytes):
                    if interrupt_target is not None:
                        now = time.monotonic()
                        if (
                            now - last_interrupt_poll
                            >= self.args.interrupt_poll_interval
                        ):
                            last_interrupt_poll = now
                            try:
                                status = await self.status()
                            except Exception:
                                status = {}
                            count = int(status.get("interrupt_count", 0))
                            if count >= interrupt_target:
                                interrupted = True
                                self.log(
                                    "INTERRUPT",
                                    f"{label} 发送期间检测到提前打断，"
                                    "已停止该回答剩余 PCM",
                                    source=status.get("interrupt_source"),
                                    remaining_s=status.get(
                                        "interrupt_remaining_seconds"
                                    ),
                                )
                                break
                    await websocket.send(raw[offset : offset + self.chunk_bytes])
                    if delay_ms > 0:
                        await asyncio.sleep(delay_ms / 1000)
                if interrupted:
                    await websocket.send(
                        json.dumps({"type": "audio_cancel"})
                    )
                else:
                    await websocket.send(json.dumps({"type": "audio_end"}))
                await asyncio.wait_for(
                    done.wait(),
                    timeout=self.args.send_timeout,
                )
            finally:
                done.set()
                receiver_task.cancel()
                await asyncio.gather(receiver_task, return_exceptions=True)
            if receive_error:
                raise receive_error[0]

        if first_ack_at is None and not interrupted:
            raise RuntimeError(f"{label} 没有任何窗口被服务端接受")
        window_count = windows or acked_windows
        first_segment_id = segments_before
        last_segment_id = (
            segments_before + window_count - 1
            if window_count > 0
            else segments_before - 1
        )
        if window_count > 0:
            await self.wait_status(
                lambda item: int(item.get("total_segments", 0))
                >= last_segment_id + 1,
                f"{label} 音频窗口完成注册",
                self.args.segment_timeout,
            )
        if interrupted and window_count == 0:
            raise RuntimeError(
                f"{label} 在首窗口注册前就被打断，无法测试自动恢复；"
                "请稍等播放开始后再打断"
            )
        result = SendResult(
            path=str(path),
            duration=duration,
            source_duration=source_duration,
            windows=window_count,
            samples=sample_count,
            first_ack_delay=(
                first_ack_at - sent_at
                if first_ack_at is not None
                else -1.0
            ),
            first_segment_id=first_segment_id,
            last_segment_id=last_segment_id,
            send_delay_ms=delay_ms,
            interrupted=interrupted,
        )
        if cancelled:
            self.log(
                "UPSTREAM",
                f"{label} 上行已中止",
                windows=window_count,
            )
        self.report.sends.append(result.__dict__)
        self.log(
            "UPSTREAM",
            f"{label} 发送提前结束" if interrupted else f"{label} 发送完成",
            windows=result.windows,
            first_ack_s=round(result.first_ack_delay, 3),
        )
        return result

    async def wait_answer_playback(
        self,
        label: str,
        last_segment_id: int,
    ) -> dict[str, Any]:
        """Wait until every frame of this answer has been played.

        In stream mode one answer is split into many short segments, so the
        global ``playback_complete`` flag can briefly become true between two
        windows of the same answer. Tie completion to the last segment that
        this answer produced instead.
        """

        def finished(item: dict[str, Any]) -> bool:
            latest_segment = int(
                item.get("latest_media_segment_id", -1)
            )
            ack_frame = int(item.get("media_ack_frame", -1))
            end_frame = int(item.get("latest_media_end_frame", -1))
            return (
                bool(item.get("playback_started"))
                and not item.get("interrupt_active")
                and int(item.get("total_segments", 0)) > last_segment_id
                and latest_segment >= last_segment_id
                and end_frame >= 0
                and ack_frame >= end_frame
            )

        status = await self.wait_status(
            finished,
            f"{label} 最后窗口播放完成",
            self.args.playback_timeout,
        )
        self.log(
            "PLAYBACK",
            f"{label} 播放完成",
            ack_frame=status.get("media_ack_frame"),
            latest_media_segment_id=status.get(
                "latest_media_segment_id"
            ),
            latest_media_end_frame=status.get("latest_media_end_frame"),
        )
        return status

    async def wait_answer_started(
        self,
        label: str,
        first_segment_id: int,
        last_segment_id: int,
        ack_frame_before: int,
    ) -> dict[str, Any]:
        """Wait for the first played frame of an answer.

        ``playback_started`` only says that the viewer entered the room. Use a
        media ACK beyond the previous answer's ACK watermark to prove that this
        answer is actually on screen.
        """

        deadline = time.monotonic() + self.args.segment_timeout
        while time.monotonic() < deadline:
            status = await self.status()
            latest_segment = int(
                status.get("latest_media_segment_id", -1)
            )
            ack_frame = int(status.get("media_ack_frame", -1))
            end_frame = int(status.get("latest_media_end_frame", -1))
            if (
                latest_segment >= first_segment_id
                and ack_frame > ack_frame_before
            ):
                if (
                    latest_segment >= last_segment_id
                    and end_frame >= 0
                    and ack_frame >= end_frame
                ):
                    raise RuntimeError(
                        f"{label} 在打断提示前已经播放完成；"
                        "请使用更小的 --answer2-send-delay-ms，"
                        "或增大 live.max_prefetch_seconds 后重试"
                    )
                return status
            await asyncio.sleep(self.args.status_interval)
        raise TimeoutError(f"等待 {label} 开始播放超时")

    async def wait_interrupt(
        self,
        target_count: int,
        prompt: str,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        self.log("ACTION", prompt)
        deadline = time.monotonic() + (
            self.args.interrupt_detect_timeout
            if timeout is None
            else timeout
        )
        expected_label = target_count - self.interrupt_base
        while time.monotonic() < deadline:
            status = await self.status()
            count = int(status.get("interrupt_count", 0))
            if count > target_count:
                raise RuntimeError(
                    f"检测到额外打断: expected={target_count}, actual={count}. "
                    "请重启服务和脚本后，严格按提示只执行两次打断"
                )
            if count == target_count:
                if status.get("interrupt_active"):
                    self.log(
                        "INTERRUPT",
                        f"检测到第 {expected_label} 次打断",
                        source=status.get("interrupt_source"),
                        remaining_s=status.get("interrupt_remaining_seconds"),
                    )
                    return status
                raise RuntimeError(
                    f"第 {expected_label} 次打断已结束，脚本错过了等待窗口；"
                    "请重启服务和脚本后重试"
                )
            await asyncio.sleep(self.args.status_interval)
        raise TimeoutError(
            f"等待第 {expected_label} 次打断超时: "
            f"expected interrupt_count={target_count}"
        )

    async def wait_first_resume(
        self,
        latest_segment_before: int,
        max_interrupt_count: int,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + self.args.resume_timeout
        while time.monotonic() < deadline:
            status = await self.status()
            count = int(status.get("interrupt_count", 0))
            if count > max_interrupt_count:
                raise RuntimeError(
                    f"等待自动恢复期间又检测到打断: "
                    f"expected<={max_interrupt_count}, actual={count}. "
                    "请重启服务和脚本后，严格按提示只执行两次打断"
                )
            if (
                not status.get("interrupt_active")
                and int(status.get("resume_count", 0))
                >= self.resume_base + 1
                and int(status.get("latest_media_segment_id", -1))
                > latest_segment_before
            ):
                self.log(
                    "RESUME",
                    "被打断音视频已开始恢复",
                    latest_media_segment_id=status.get(
                        "latest_media_segment_id"
                    ),
                )
                return status
            await asyncio.sleep(self.args.status_interval)
        raise TimeoutError("等待第一次打断后的自动恢复超时")

    async def wait_idle_exit(self) -> dict[str, Any]:
        timeout = self.args.idle_timeout_override
        if timeout <= 0:
            status = await self.status()
            timeout = float(status.get("idle_timeout_seconds", 30)) + 15
        deadline = time.monotonic() + timeout
        last_status: dict[str, Any] = {}
        while time.monotonic() < deadline:
            try:
                status = await self.status()
                last_status = status
                if status.get("status") == "exiting" or status.get("exit_reason"):
                    self.log(
                        "IDLE",
                        "服务端进入空闲退出",
                        reason=status.get("exit_reason"),
                    )
                    return status
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                self.log("IDLE", "服务端已关闭，空闲退出完成")
                return last_status
            await asyncio.sleep(self.args.status_interval)
        raise TimeoutError("等待空闲退出超时")

    async def run(self) -> None:
        status = await self.wait_ready()
        self.log(
            "PREFLIGHT",
            "环境检查通过",
            input_mode=status.get("input_mode"),
            interrupt_wait_s=status.get("interrupt_wait_seconds"),
            idle_timeout_s=status.get("idle_timeout_seconds"),
            answer2_send_delay_ms=self.args.answer2_send_delay_ms,
        )
        if status.get("stream_connected"):
            raise RuntimeError("已有其他上游音频连接")
        self.interrupt_base = int(status.get("interrupt_count", 0))
        self.resume_base = int(status.get("resume_count", 0))
        self.log(
            "PREFLIGHT",
            "记录运行前事件计数",
            interrupt_count=self.interrupt_base,
            resume_count=self.resume_base,
        )

        self.log("QUESTION", "模拟用户问题 1")
        answer1 = await self.send_answer(
            Path(self.args.answer_1),
            "回答 1 / sun.wav",
        )
        await self.wait_answer_playback("回答 1", answer1.last_segment_id)
        status = await self.status()
        media_ack_before = int(status.get("media_ack_frame", -1))

        self.log("QUESTION", "模拟用户问题 2")
        answer2 = await self.send_answer(
            Path(self.args.answer_2),
            "回答 2 / eng.wav",
            max_duration=self.args.answer2_seconds_before_interrupt,
            send_delay_ms=self.args.answer2_send_delay_ms,
            interrupt_target=self.interrupt_base + 1,
        )
        if answer2.interrupted:
            status = await self.status()
            if not status.get("interrupt_active"):
                raise RuntimeError(
                    "回答 2 发送期间的提前打断已被取消；"
                    "请确认服务和脚本均为最新版本后重试"
                )
        else:
            status = await self.wait_answer_started(
                "回答 2",
                answer2.first_segment_id,
                answer2.last_segment_id,
                media_ack_before,
            )
        latest_before = int(
            status.get("latest_media_segment_id", -1)
        )
        if answer2.interrupted:
            first_interrupt = status
            self.log(
                "ACTION",
                "已检测到回答 2 的提前打断，保持上游静默并等待自动恢复",
                interrupt_count=status.get("interrupt_count"),
                source=status.get("interrupt_source"),
                remaining_s=status.get("interrupt_remaining_seconds"),
            )
        else:
            ack_frame = int(status.get("media_ack_frame", -1))
            end_frame = int(status.get("latest_media_end_frame", -1))
            fps = float(status.get("fps", 24) or 24)
            remaining_s = max(0.0, end_frame - ack_frame) / fps
            if remaining_s < 1.0:
                raise RuntimeError(
                    "回答 2 可打断的播放窗口不足 1 秒；请使用更小的 "
                    "--answer2-send-delay-ms，或增大 "
                    "live.max_prefetch_seconds 后重试"
                )
            first_interrupt = await self.wait_interrupt(
                self.interrupt_base + 1,
                f"回答 2 播放中（剩余约 {remaining_s:.1f}s），"
                "请立即在前端点击“打断”；"
                "之后不要操作，等待自动恢复",
                timeout=remaining_s + 1.0,
            )
        interrupt_wait = float(
            first_interrupt.get("interrupt_wait_seconds", 10)
        )
        self.log(
            "INTERRUPT",
            "保持上游静默，等待自动恢复",
            wait_s=interrupt_wait,
        )
        await self.wait_first_resume(
            latest_before,
            max_interrupt_count=self.interrupt_base + 1,
        )
        await self.wait_interrupt(
            self.interrupt_base + 2,
            "恢复播放中，请再次在前端点击“打断”",
        )

        self.log("QUESTION", "模拟用户问题 3")
        third = await self.send_answer(
            Path(self.args.answer_3),
            "回答 3 / musetalk_test_audio.wav",
        )
        if third.first_ack_delay > interrupt_wait:
            raise RuntimeError(
                f"新回答首窗口超过打断窗口: "
                f"{third.first_ack_delay:.3f}s > {interrupt_wait:.3f}s"
            )
        await self.wait_status(
            lambda item: (
                not item.get("interrupt_active")
                and int(item.get("interrupt_count", 0))
                >= self.interrupt_base + 2
            ),
            "新回答取消旧内容恢复",
            self.args.segment_timeout,
        )
        await self.wait_answer_playback("回答 3", third.last_segment_id)
        self.log("IDLE", "不再发送新回答，等待服务端空闲退出")
        await self.wait_idle_exit()
        self.report.passed = True
        self.log("DONE", "模拟流程完成")

    def write_report(self, error: Exception | None = None) -> None:
        if error is not None:
            self.report.errors.append(
                f"{type(error).__name__}: {error}"
            )
        if not self.args.report:
            return
        path = Path(self.args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "passed": self.report.passed,
            "started_at": self.report.started_at,
            "base_url": self.report.base_url,
            "events": self.report.events,
            "sends": self.report.sends,
            "errors": self.report.errors,
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"[REPORT] {path}", flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Simulate upstream PCM answers for interrupt testing",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--answer-1", default="data/audio/sun.wav")
    parser.add_argument("--answer-2", default="data/audio/eng.wav")
    parser.add_argument(
        "--answer-3",
        default="data/audio/musetalk_test_audio.wav",
    )
    parser.add_argument(
        "--answer2-seconds-before-interrupt",
        type=float,
        default=15.0,
        help="回答 2 在提示打断前发送的秒数；0 表示发送完整文件",
    )
    parser.add_argument("--chunk-ms", type=int, default=20)
    parser.add_argument(
        "--send-delay-ms",
        type=float,
        default=20.0,
        help="每个 PCM 块之后的发送间隔；20 表示实时 20ms 流",
    )
    parser.add_argument(
        "--answer2-send-delay-ms",
        type=float,
        default=5.0,
        help=(
            "回答 2 的 PCM 块发送间隔。流式输入会边发边播，"
            "加速预送可在提示打断时保留播放尾段"
        ),
    )
    parser.add_argument("--status-interval", type=float, default=0.2)
    parser.add_argument(
        "--interrupt-poll-interval",
        type=float,
        default=0.05,
        help="回答 2 发送期间检查打断事件的间隔",
    )
    parser.add_argument("--connect-timeout", type=float, default=10.0)
    parser.add_argument("--startup-timeout", type=float, default=180.0)
    parser.add_argument("--viewer-timeout", type=float, default=600.0)
    parser.add_argument("--send-timeout", type=float, default=180.0)
    parser.add_argument("--segment-timeout", type=float, default=60.0)
    parser.add_argument("--playback-timeout", type=float, default=300.0)
    parser.add_argument("--interrupt-detect-timeout", type=float, default=180.0)
    parser.add_argument("--resume-timeout", type=float, default=60.0)
    parser.add_argument(
        "--idle-timeout-override",
        type=float,
        default=0.0,
        help="0 uses /api/status idle_timeout_seconds + 15",
    )
    parser.add_argument(
        "--report",
        default="output/stream_interrupt_test.json",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.chunk_ms <= 0:
        raise SystemExit("--chunk-ms must be > 0")
    if not math.isfinite(args.status_interval) or args.status_interval <= 0:
        raise SystemExit("--status-interval must be > 0")
    if (
        not math.isfinite(args.interrupt_poll_interval)
        or args.interrupt_poll_interval <= 0
    ):
        raise SystemExit("--interrupt-poll-interval must be > 0")
    if (
        not math.isfinite(args.answer2_seconds_before_interrupt)
        or args.answer2_seconds_before_interrupt < 0
    ):
        raise SystemExit(
            "--answer2-seconds-before-interrupt must be >= 0"
        )
    if args.answer2_seconds_before_interrupt == 0:
        args.answer2_seconds_before_interrupt = None
    if not math.isfinite(args.send_delay_ms) or args.send_delay_ms < 0:
        raise SystemExit("--send-delay-ms must be >= 0")
    if (
        not math.isfinite(args.answer2_send_delay_ms)
        or args.answer2_send_delay_ms < 0
    ):
        raise SystemExit("--answer2-send-delay-ms must be >= 0")
    simulator = Simulator(args)
    error: Exception | None = None
    try:
        asyncio.run(simulator.run())
        return 0
    except KeyboardInterrupt:
        error = KeyboardInterrupt()
        print("\n[ABORT] 用户中断测试", file=sys.stderr)
        return 130
    except Exception as exc:
        error = exc
        print(f"\n[FAILED] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        simulator.write_report(error)


if __name__ == "__main__":
    raise SystemExit(main())
