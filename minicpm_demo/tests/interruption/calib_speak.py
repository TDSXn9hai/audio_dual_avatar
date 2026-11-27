"""p_speak calibration: raw P(<|speak|>) at unit start in listen chunks, by context.

Usage: python calib_speak.py <backend.log> <json_dir>
Sessions in the log are paired with JSON transcripts in mtime order, so the log must contain
exactly the sessions in <json_dir> (use a dedicated test instance, see test_instance.sh).
Backend chunk index is aligned to transcript time with the first speaking chunk.
"""
import json, math, re, sys
from collections import defaultdict
from pathlib import Path

import soundfile as sf

INPUTS = Path(__file__).resolve().parent / "inputs"
log, jdir = Path(sys.argv[1]), Path(sys.argv[2])
SR = 16000
DUR = {}  # user label -> number of 1s chunks sent (audio + 0.5s pad)
for label, wav in [("什么是认知", "q1.wav"), ("如何提升认知", "q3.wav"),
                   ("认知和记忆有什么关系", "q2.wav"), ("我第二个问题问的是什么", "probe_second_question.wav")]:
    DUR[label] = math.ceil(sf.info(INPUTS / wav).duration + 0.5)

sessions, cur = [], None
for l in log.read_text("utf-8", errors="replace").splitlines():
    if "force_listen=True (call #1)" in l:
        cur = []
        sessions.append(cur)
        continue
    m = re.search(r"\[TokenTrace\] t=(\d+) is_listen=(\w+).* p_speak=(\S+)", l)
    if m and cur is not None:
        cur.append((int(m[1]), m[2] == "True", None if m[3] == "-" else float(m[3])))

jsons = sorted(jdir.glob("*.json"), key=lambda p: p.stat().st_mtime)
assert len(jsons) == len(sessions), (len(jsons), len(sessions))
buckets = defaultdict(list)
for sess, jp in zip(sessions, jsons):
    ev = json.loads(jp.read_text("utf-8"))["events"]
    first_text = next(e["t"] for e in ev if e["kind"] == "text")
    first_speak = next(t for t, lis, _ in sess if not lis)
    off = first_text - first_speak  # transcript time ~= chunk index + off
    users = [(e["t"], e["t"] + DUR.get(e["text"], 3)) for e in ev if e["kind"] == "user"]
    ends = [e["t"] for e in ev if e["kind"] == "turn_end"]
    # barge-in stops: turn_end within 3s after a user start while the model was speaking
    stops = []
    for us, _ in users:
        nxt = [t for t in ends if us <= t <= us + 3]
        prev_text = [e["t"] for e in ev if e["kind"] == "text" and e["t"] <= us]
        prev_end = [t for t in ends if t <= us]
        if nxt and prev_text and (not prev_end or prev_text[-1] > prev_end[-1]):
            stops.append(nxt[0])
    for t, lis, ps in sess:
        if not lis or ps is None:
            continue
        tt = t + off
        speaking_user = any(a <= tt <= b for a, b in users)
        last_end = max((e for e in ends if e <= tt), default=None)
        if speaking_user:
            key = "user_speaking"
        elif last_end is not None and last_end in stops:
            key = "after_barge_stop_user_done"
        elif last_end is not None:
            key = "after_natural_end_idle"
        else:
            key = "startup_idle"
        buckets[key].append((ps, jp.stem, t))

for k, v in buckets.items():
    xs = sorted(p for p, _, _ in v)
    q = lambda f: xs[min(len(xs) - 1, int(f * len(xs)))]
    print(f"{k:28s} n={len(xs):4d} p50={q(.5):.4f} p90={q(.9):.4f} p99={q(.99):.4f} max={xs[-1]:.4f}")
import sys as _s; _s.exit(0) if False else None
print("\nuser_speaking with p>=0.02:")
for p, s, t in buckets["user_speaking"]:
    if p >= 0.02: print(f"  {s:20s} t={t:3d} p_speak={p:.4f}")
print("\nafter_barge_stop_user_done detail:")
for p, s, t in buckets["after_barge_stop_user_done"]:
    print(f"  {s:20s} t={t:3d} p_speak={p:.4f}")
