#!/bin/bash
# Interleaved A/B over duplex config variants (see variants.json), one session at a time.
#
# Usage:
#   bash run_ab.sh --out DIR --variants AC,ACB [--late N] [--probe N] [--wait N] [--barge N]
#                  [--lp 1.05] [--url ws://127.0.0.1:22401/v1/worker/duplex]
# Output: DIR/<variant>_lp<lp>/<scenario>_lp<lp>_r<rep>.json, then: python analyze.py DIR
# Defaults reproduce the 2026-11-27 run (ab6): late 20, probe 15, wait 5 (2 questions + 20s idle).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${PYTHON:-$HERE/../../../.venv/bin/python}"
URL=ws://127.0.0.1:22401/v1/worker/duplex
OUT="" VARIANTS="" LP=1.05 LATE=20 PROBE=15 WAIT=5 BARGE=0
while [[ $# -gt 0 ]]; do
    case $1 in
        --out) OUT=$2; shift 2 ;;
        --variants) VARIANTS=$2; shift 2 ;;
        --lp) LP=$2; shift 2 ;;
        --url) URL=$2; shift 2 ;;
        --late) LATE=$2; shift 2 ;;
        --probe) PROBE=$2; shift 2 ;;
        --wait) WAIT=$2; shift 2 ;;
        --barge) BARGE=$2; shift 2 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done
[[ -n "$OUT" && -n "$VARIANTS" ]] || { sed -n '2,8p' "$0"; exit 2; }

variant_json() {
    "$PY" -c 'import json,sys; v=json.load(open(sys.argv[1]))[sys.argv[2]]; print(json.dumps(v))' \
        "$HERE/variants.json" "$1"
}

IFS=, read -ra VS <<< "$VARIANTS"
declare -A V
for v in "${VS[@]}"; do V[$v]="$(variant_json "$v")" || exit 1; done
MAX=$(printf '%s\n' "$LATE" "$PROBE" "$WAIT" "$BARGE" | sort -n | tail -1)
probe() { "$PY" "$HERE/duplex_probe.py" --url "$URL" --lp "$LP" "$@"; }

for rep in $(seq 1 "$MAX"); do
    for v in "${VS[@]}"; do
        out="$OUT/${v}_lp${LP}"
        common=(--out "$out" --tag "r$rep" --variant "${V[$v]}")
        [[ $rep -le $LATE ]] && probe --scenario late --barge-after 10 --tail-idle 15 "${common[@]}"
        [[ $rep -le $PROBE ]] && probe --scenario probe --tail-idle 15 "${common[@]}"
        [[ $rep -le $WAIT ]] && probe --scenario wait --nq 2 --tail-idle 20 "${common[@]}"
        [[ $rep -le $BARGE ]] && probe --scenario barge "${common[@]}"
    done
done
echo ABDONE
