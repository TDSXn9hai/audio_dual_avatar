"""Print transcripts of duplex_probe.py output: per-question answer text and turn behavior.

Usage: python summarize.py <json_dir>
"""
import json, sys
from pathlib import Path

out_dir = Path(sys.argv[1])
for path in sorted(out_dir.glob("*.json")):
    d = json.loads(path.read_text("utf-8"))
    print(f"\n===== {path.stem} =====")
    seg, turn_ends, answers = "", 0, []
    for e in d["events"]:
        if e["kind"] == "text":
            seg += e["text"]
            continue
        if seg:
            print(f"    {seg}")
            answers.append(seg)
            seg = ""
        if e["kind"] == "user":
            print(f"[{e['t']:7.2f}s] USER: {e['text']}")
        elif e["kind"] == "turn_end":
            turn_ends += 1
            print(f"[{e['t']:7.2f}s] -- turn_eos/listen --")
        else:
            print(f"[{e['t']:7.2f}s] {e['kind']}")
    if seg:
        print(f"    {seg}")
        answers.append(seg)
    total = sum(len(a) for a in answers)
    print(f"  turn_ends={turn_ends} chars={total}")
