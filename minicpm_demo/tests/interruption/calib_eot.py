"""Extract per-step raw P(turn_eos) at sentence ends from backend.log, per duplex session.

Usage: python calib_eot.py <backend.log> <session_json_dir> [start_marker]
Pairs sessions in log order with JSON files sorted by mtime; prints sentence-end steps
(previous text token ends with 。！？) with p_eot, flagging those within 4 chunks after barge-in.
"""
import json, re, sys
from pathlib import Path

log, jdir = Path(sys.argv[1]), Path(sys.argv[2])
marker = sys.argv[3] if len(sys.argv) > 3 else None
lines = log.read_text("utf-8", errors="replace").splitlines()
if marker:
    lines = lines[max(i for i, l in enumerate(lines) if marker in l):]

sessions, cur = [], None
for l in lines:
    if "force_listen=True (call #1)" in l:
        cur = []
        sessions.append(cur)
        continue
    if cur is None:
        continue
    m = re.search(r"\[TokenTrace\] t=(\d+)", l)
    if m:
        t = int(m[1])
        continue
    m = re.search(r"j=(\d+) TEXT id=\d+ '(.*?)'.* p_eot=([\d.]+) lp=([\d.]+)", l)
    if m:
        cur.append((t, int(m[1]), m[2], float(m[3]), float(m[4])))

jsons = sorted(jdir.glob("*.json"), key=lambda p: p.stat().st_mtime)
SENT_END = set("。！？!?")
for sess, jp in zip(sessions, jsons):
    users = [e["t"] for e in json.loads(jp.read_text("utf-8"))["events"] if e["kind"] == "user"]
    barge = users[1] + 1 if len(users) > 1 else None  # chunk index ~ wall seconds + 1
    print(f"== {jp.stem} barge_chunk~{barge}")
    prev = ""
    for t, j, tok, p, lp in sess:
        if j > 0 and prev.rstrip("”’\"'）) ")[-1:] in SENT_END:
            flag = "BARGE" if barge and barge <= t <= barge + 4 else ""
            print(f"   t={t:3d} after '{prev}' next='{tok}' p_eot={p:.4f} {flag}")
        if not tok.startswith("<|"):
            prev = tok
        elif tok == "<|turn_eos|>":
            print(f"   t={t:3d} TURN_EOS after '{prev}' p_eot={p:.4f} lp={lp}")
