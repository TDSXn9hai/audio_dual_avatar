"""Barge-in probe client for the MiniCPM-o duplex worker (direct, no avatar bridge).

Streams TTS question audio in real-time 1s chunks and records model text / turn ends.
The session payload has the same shape as gateway_modules/avatar_session_api.py.

Scenarios:
  wait  - ask each question, wait for the model to finish (listen) before the next
  barge - ask the next question once the model has spoken --barge-after seconds of audio
  probe - q1, barge in with q3 after --barge-after seconds (mid-sentence), then ask --probe-wav
  late  - like probe, but barge in right after a chunk containing a sentence end

Output: <out>/<scenario>_lp<lp>_<tag>.json (+ .wav of the model's audio)
"""
import argparse, asyncio, base64, json, time
from pathlib import Path

import librosa, numpy as np, soundfile as sf, websockets

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]  # minicpm_demo
INPUTS = HERE / "inputs"
SR, CHUNK = 16000, 16000
SILENCE = base64.b64encode(np.zeros(CHUNK, "<f4").tobytes()).decode()


def chunks_of(path):
    audio, _ = librosa.load(str(path), sr=SR, mono=True)
    audio = np.concatenate([audio, np.zeros(SR // 2, np.float32)])
    out = []
    for i in range(0, len(audio), CHUNK):
        c = audio[i:i + CHUNK]
        c = np.pad(c, (0, CHUNK - len(c))).astype("<f4")
        out.append(base64.b64encode(c.tobytes()).decode())
    return out


def init_payload(lp, extra=None):
    prompt = (ROOT / "assets/prompts/avatar/knowledge_host.txt").read_text("utf-8").strip()
    if not prompt.startswith("Streaming"):
        prompt = f"Streaming Audio Conversation. {prompt}"
    voice, _ = librosa.load(str(ROOT / "assets/ref_audio/ref_avatar_default.wav"), sr=SR, mono=True)
    v64 = base64.b64encode(voice.astype(np.float32).tobytes()).decode()
    # Same shape as gateway_modules/avatar_session_api.py (the /avatar-v2 path).
    return {"system_prompt": prompt, "config": {"length_penalty": lp, **(extra or {})}, "use_tts": True,
            "ref_audio_base64": v64, "tts_ref_audio_base64": v64}


class Run:
    def __init__(self, ws):
        self.ws, self.t0 = ws, time.perf_counter()
        self.events, self.speaking, self.spoken_s = [], False, 0.0
        self.turn_end = asyncio.Event()
        self.audio = []
        self.last_text = ""

    def now(self):
        return round(time.perf_counter() - self.t0, 2)

    async def recv(self):
        async for raw in self.ws:
            m = json.loads(raw)
            if m.get("type") != "response.output.delta":
                if m.get("type") in ("session.closed", "error"):
                    self.events.append({"t": self.now(), "kind": m.get("type"), "msg": str(m)[:200]})
                    return
                continue
            kind = m.get("kind")
            if kind == "text" and m.get("text"):
                self.events.append({"t": self.now(), "kind": "text", "text": m["text"]})
                self.last_text = m["text"]
                self.speaking = True
            elif kind == "audio" and m.get("audio"):
                a = np.frombuffer(base64.b64decode(m["audio"]), "<f4")
                self.audio.append(a)
                self.spoken_s += len(a) / 24000
                self.speaking = True
            elif kind == "listen":
                if self.speaking:
                    self.events.append({"t": self.now(), "kind": "turn_end"})
                    self.turn_end.set()
                self.speaking = False

    async def send(self, b64):
        await self.ws.send(json.dumps({"type": "input.append", "input": {"audio": b64}}))

    async def play(self, chunks, label):
        self.events.append({"t": self.now(), "kind": "user", "text": label})
        for c in chunks:
            await self.send(c)
            await asyncio.sleep(1.0)

    async def idle_until(self, cond, timeout):
        end = time.perf_counter() + timeout
        while not cond() and time.perf_counter() < end:
            await self.send(SILENCE)
            await asyncio.sleep(1.0)


async def session(url, lp, scenario, questions, barge_after, extra=None, tail_idle=2):
    async with websockets.connect(url, max_size=None) as ws:
        await ws.send(json.dumps({"type": "session.init", "payload": init_payload(lp, extra)}))
        created = json.loads(await asyncio.wait_for(ws.recv(), 60))
        r = Run(ws)
        rx = asyncio.create_task(r.recv())
        await r.idle_until(lambda: False, 4)  # settle the 3 forced-listen chunks
        if scenario in ("probe", "late"):
            # q1, barge in with q2 mid-answer, give it time, then ask what was asked.
            (l1, c1), (l2, c2), (l3, c3) = questions
            r.turn_end.clear()
            start_spoken = r.spoken_s
            await r.play(c1, l1)
            if scenario == "probe":
                await r.idle_until(lambda: r.turn_end.is_set()
                                   or r.spoken_s - start_spoken >= barge_after, 40)
            else:  # late: barge in right after a chunk containing a sentence end, once barge_after seconds were spoken
                await r.idle_until(lambda: r.turn_end.is_set()
                                   or (r.spoken_s - start_spoken >= barge_after
                                       and any(c in r.last_text for c in "。！？!?")), 60)
            await r.play(c2, l2)
            await r.idle_until(lambda: False, 15)
            await r.idle_until(lambda: not r.speaking, 40)
            r.turn_end.clear()
            await r.play(c3, l3)
            await r.idle_until(lambda: r.speaking, 15)
            if r.speaking:
                await r.idle_until(lambda: r.turn_end.is_set(), 60)
            await r.idle_until(lambda: False, tail_idle)
            questions = []
        for i, (label, chunks) in enumerate(questions):
            r.turn_end.clear()
            start_spoken = r.spoken_s
            await r.play(chunks, label)
            last = i == len(questions) - 1
            if scenario == "wait" or last:
                await r.idle_until(lambda: r.speaking, 15)
                if r.speaking:
                    await r.idle_until(lambda: r.turn_end.is_set(), 60)
                await r.idle_until(lambda: False, tail_idle if last else 2)
            else:
                await r.idle_until(lambda: r.turn_end.is_set()
                                   or r.spoken_s - start_spoken >= barge_after, 40)
        await ws.send(json.dumps({"type": "session.close", "reason": "interruption_probe"}))
        await asyncio.sleep(0.5)
        rx.cancel()
        return created, r


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="ws://127.0.0.1:22400/v1/worker/duplex")
    p.add_argument("--lp", type=float, default=1.05, help="length_penalty")
    p.add_argument("--scenario", choices=["wait", "barge", "probe", "late"], required=True)
    p.add_argument("--tag", default="r1")
    p.add_argument("--barge-after", type=float, default=6.0)
    p.add_argument("--qdir", default=str(INPUTS))
    p.add_argument("--out", default=str(ROOT / "tests/results/interruption"))
    p.add_argument("--probe-wav", default=str(INPUTS / "probe_second_question.wav"))
    p.add_argument("--probe-label", default="我第二个问题问的是什么")
    p.add_argument("--variant", default="", help="JSON dict merged into duplex config")
    p.add_argument("--nq", type=int, default=5, help="number of questions for wait/barge")
    p.add_argument("--tail-idle", type=float, default=2, help="silence after the last answer (s)")
    a = p.parse_args()
    labels = ["什么是认知", "认知和记忆有什么关系", "如何提升认知", "认知提升和思维拓展的关系", "如何实现自我成长"]
    qs = [(l, chunks_of(Path(a.qdir) / f"q{i + 1}.wav")) for i, l in enumerate(labels)]
    if a.scenario in ("probe", "late"):
        qs = [qs[0], qs[2], (a.probe_label, chunks_of(Path(a.probe_wav)))]
    else:
        qs = qs[:a.nq]
    extra = json.loads(a.variant) if a.variant else {}
    created, r = asyncio.run(session(a.url, a.lp, a.scenario, qs, a.barge_after, extra, a.tail_idle))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    name = f"{a.scenario}_lp{a.lp}_{a.tag}"
    (out / f"{name}.json").write_text(json.dumps(
        {"lp": a.lp, "scenario": a.scenario, "variant": extra, "created": created, "events": r.events},
        ensure_ascii=False, indent=1), "utf-8")
    if r.audio:
        sf.write(out / f"{name}.wav", np.concatenate(r.audio), 24000)
    print(name, "events", len(r.events), "spoken_s", round(r.spoken_s, 1))


if __name__ == "__main__":
    main()
