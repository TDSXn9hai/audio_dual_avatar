# MuseTalk and its GPEN child share device visibility, not the interpreter.
# Keep MuseTalk devices first so gpu_id retains its meaning.
configure_gpen_gpu() {
    local muse="$1" gpen="${2:-}" legacy="${3:-}" token index=0
    local -a devices
    MUSETALK_PROCESS_GPUS="$muse"
    MUSETALK_GPEN_DEVICE="$legacy"
    [[ -n "$gpen" ]] || return 0
    if [[ ! "$gpen" =~ ^([0-9]+|GPU-[A-Za-z0-9-]+|MIG-[A-Za-z0-9/-]+)$ ]]; then
        echo 'GPEN_CUDA_VISIBLE_DEVICES must select exactly one GPU index or UUID' >&2
        return 2
    fi
    IFS=',' read -r -a devices <<< "$muse"
    for token in "${devices[@]}"; do
        if [[ "$token" == "$gpen" ]]; then
            MUSETALK_GPEN_DEVICE="cuda:$index"
            return 0
        fi
        index=$((index + 1))
    done
    MUSETALK_PROCESS_GPUS="${muse},${gpen}"
    MUSETALK_GPEN_DEVICE="cuda:$index"
}
