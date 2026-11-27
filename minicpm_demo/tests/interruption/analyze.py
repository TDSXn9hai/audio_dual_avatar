"""Aggregate <root>/<variant>/*.json (duplex_probe.py output) into barge-in and answer-length metrics.

Usage: python analyze.py <root>   (each subdirectory of <root> is one variant)
"""
import json, sys
from collections import defaultdict
from pathlib import Path

root = Path(sys.argv[1])
STOP_WINDOW = 3.0


def segments(events):
    """Split events into USER-delimited spans: (user_event, [events until next user])."""
    spans, cur = [], None
    for e in events:
        if e["kind"] == "user":
            cur = (e, [])
            spans.append(cur)
        elif cur is not None:
            cur[1].append(e)
    return spans


def speaking_at(events, t):
    """Model is mid-turn at time t: last text before t comes after the last turn_end before t."""
    last_text = max((e["t"] for e in events if e["kind"] == "text" and e["t"] <= t), default=None)
    last_end = max((e["t"] for e in events if e["kind"] == "turn_end" and e["t"] <= t), default=-1)
    return last_text is not None and last_text > last_end


SENT_END = set("。！？!?")


def late_outcome(events, t_user, after):
    """Classify the reply to q2 ("如何提升认知") after a barge-in near a sentence end.

    complete_after_stop / complete_inline: answer to q2 present and its turn ends after
              sentence-final punctuation; the model first stopped (turn_eos within 3s) or
              switched to q2 inside the same turn
    cut:      answer to q2 present, its turn ends mid-sentence and nothing follows
    ongoing:  answer to q2 still in progress when the next user question arrives
    stop_silent: stopped within 3s of the barge-in and never spoke again before the next question
    ignored:  kept talking about q1, never answered q2
    not_barge: model was not speaking when q2 started
    """
    if not speaking_at(events, t_user):
        return "not_barge"
    turn, saw_answer, outcome = "", False, None
    for e in after:
        if e["kind"] == "text":
            turn += e["text"]
            saw_answer = saw_answer or "提升" in turn
        elif e["kind"] == "turn_end":
            if saw_answer:
                ended = turn.rstrip("”’\"'）) ")[-1:] in SENT_END
                outcome = "complete" if ended else "cut"
            turn = ""
    if outcome is None and saw_answer:
        outcome = "ongoing"
    ends = [e["t"] for e in after if e["kind"] == "turn_end"]
    stopped_first = bool(ends) and ends[0] - t_user <= STOP_WINDOW
    if outcome is None:
        later_text = any(e["kind"] == "text" and e["t"] > ends[0] for e in after) if ends else False
        outcome = "stop_silent" if stopped_first and not later_text else "ignored"
    elif outcome == "complete":
        outcome = "complete_after_stop" if stopped_first else "complete_inline"
    return outcome


rows = defaultdict(lambda: defaultdict(list))
for d in sorted(root.iterdir()):
    for p in sorted(d.glob("*.json")):
        j = json.loads(p.read_text("utf-8"))
        ev, sc, r = j["events"], j["scenario"], rows[d.name]
        spans = segments(ev)
        for i, (u, after) in enumerate(spans):
            if sc in ("probe", "barge") and i > 0 and speaking_at(ev, u["t"]):
                ends = [e["t"] for e in after if e["kind"] == "turn_end"]
                stopped = bool(ends) and ends[0] - u["t"] <= STOP_WINDOW
                r[f"{sc}_stopped"].append(stopped)
                if stopped:
                    reply = "".join(e["text"] for e in after if e["kind"] == "text" and e["t"] > ends[0])
                    r[f"{sc}_answered_after_stop"].append(bool(reply))
            if sc == "probe" and i == len(spans) - 1:
                reply = "".join(e["text"] for e in after if e["kind"] == "text")
                r["probe_recall_ok"].append("提升" in reply)
                r["probe_final_replied"].append(bool(reply))
            if sc == "wait":
                reply = "".join(e["text"] for e in after if e["kind"] == "text")
                r["wait_replied"].append(bool(reply))
                if reply:
                    r["wait_chars"].append(len(reply))
            if sc == "late" and i == 1:
                r["late_outcome"].append(late_outcome(ev, u["t"], after))
            if i == len(spans) - 1:
                # speech after the final reply ended, with no new user input (tail idle)
                first_text = next((e["t"] for e in after if e["kind"] == "text"), None)
                end = next((e["t"] for e in after if e["kind"] == "turn_end"
                            and first_text is not None and e["t"] > first_text), None)
                if end is not None:
                    r["tail_unprompted"].append(any(e["kind"] == "text" and e["t"] > end for e in after))
            if sc in ("probe", "late") and i == 1 and speaking_at(ev, u["t"]):
                # stopped within 3s of the barge-in: did the model speak again before the next question?
                ends = [e["t"] for e in after if e["kind"] == "turn_end"]
                if ends and ends[0] - u["t"] <= STOP_WINDOW:
                    last_end = max((e["t"] for e in ev if e["kind"] == "turn_end" and e["t"] < u["t"]), default=-1)
                    turn = "".join(e["text"] for e in ev if e["kind"] == "text" and last_end < e["t"] <= ends[0])
                    pos = "end" if turn.rstrip("”’\"'）) ")[-1:] in SENT_END else "mid"
                    replied = any(e["kind"] == "text" and e["t"] > ends[0] for e in after)
                    r["stop_followup"].append(f"{pos}_{'answered' if replied else 'silent'}")

print(f"{'variant':16s} {'probe_stop':>11s} {'probe_ans':>10s} {'probe_recall':>12s} "
      f"{'barge_stop':>11s} {'barge_ans':>10s} {'wait_reply':>10s} {'wait_chars':>10s}")


def rate(xs):
    return f"{sum(xs)}/{len(xs)}" if xs else "-"


for name, r in rows.items():
    wc = r["wait_chars"]
    print(f"{name:16s} {rate(r['probe_stopped']):>11s} {rate(r['probe_answered_after_stop']):>10s} "
          f"{rate(r['probe_recall_ok']):>12s} {rate(r['barge_stopped']):>11s} "
          f"{rate(r['barge_answered_after_stop']):>10s} {rate(r['wait_replied']):>10s} "
          f"{(sum(wc) / len(wc)) if wc else 0:>10.1f}")

print("\nlate barge-in outcome ")
for name, r in rows.items():
    lo = r["late_outcome"]
    if lo:
        print(f"{name:16s} " + " ".join(f"{k}={lo.count(k)}" for k in ("complete_after_stop", "complete_inline", "ongoing", "cut", "stop_silent", "ignored", "not_barge")))

print("\nunprompted speech after the final reply (sessions)")
for name, r in rows.items():
    if r["tail_unprompted"]:
        print(f"{name:16s} {rate(r['tail_unprompted'])}")

print("\nprobe+late: stopped within 3s of barge-in, then (stop position _ follow-up)")
for name, r in rows.items():
    sf = r["stop_followup"]
    if sf:
        silent = sum(x.endswith("silent") for x in sf)
        print(f"{name:16s} silent={silent}/{len(sf)} " + " ".join(
            f"{k}={sf.count(k)}" for k in ("mid_answered", "mid_silent", "end_answered", "end_silent")))
