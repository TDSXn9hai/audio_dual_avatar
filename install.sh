#!/usr/bin/env bash
# One-click setup for the full-duplex digital human (MiniCPM-o 4.5 + MuseTalk).
#
#   1. Python 3.10 venv at ./.venv with the pinned package set
#      (requirements-avatar.lock.txt: torch 2.8.0+cu128, transformers 4.51.0 ...)
#   2. Separate mmpose/mmcv preprocessing venv for avatar creation
#   3. ffmpeg links, runtime directories, self-signed TLS certificate
#   4. Model weights: MiniCPM-o 4.5 (~19 GB) and MuseTalk runtime (~4 GB),
#      checked against weights.sha256
#
# Afterwards start everything with ./start_avatar.sh
#
# Usage: ./install.sh [--skip-env] [--skip-preprocess-env] [--skip-weights] [--no-verify] [--hf-mirror]
#
# Environment overrides:
#   AVATAR_PYTHON        Python 3.10 executable (default: python3.10, else uv-managed 3.10)
#   AVATAR_VENV          venv directory (default: ./.venv)
#   MUSETALK_VENV        MuseTalk preprocessing venv (default: ./musetalk_project/venv)
#   PIP_INDEX_URL        package index (default: pip config global.index-url, else PyPI)
#   HF_ENDPOINT          Hugging Face endpoint (default: https://huggingface.co)
#   HF_TOKEN             optional Hugging Face token
#   AVATAR_WEIGHTS_FROM  root of an existing installation; weights are hard-linked
#                        (or copied) from there instead of downloaded
#   S3FD_MODEL_URL       mirror for s3fd-619a316812.pth
#   http_proxy / https_proxy are honored by uv, pip, curl and Hugging Face.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${AVATAR_VENV:-${ROOT}/.venv}"
LOCK_FILE="${ROOT}/requirements-avatar.lock.txt"
WEIGHTS_MANIFEST="${ROOT}/weights.sha256"
MINICPM_DIR="${ROOT}/minicpm_demo"
MUSETALK_DIR="${ROOT}/musetalk_project/MuseTalk"
MUSETALK_VENV_DIR="${MUSETALK_VENV:-${ROOT}/musetalk_project/venv}"
MUSETALK_PY="${MUSETALK_VENV_DIR}/bin/python"
MUSETALK_PREPROCESS_LOCK="${ROOT}/requirements-musetalk-preprocess.lock.txt"
AVATAR_CACHE="${MUSETALK_DIR}/results/v15/avatars/musetalk_test_avatar"
S3FD_REL="musetalk_project/MuseTalk/musetalk/utils/face_detection/detection/sfd/s3fd.pth"
S3FD_REPO_PATH="n0x1103/s3fd/resolve/main/s3fd-619a316812.pth"
S3FD_SHA256="619a31681264d3f7f7fc7a16a42cbbe8b23f31a256f75a366e5a1bcd59b33543"
# Hugging Face revision whose files match weights.sha256.
MINICPM_REPO="openbmb/MiniCPM-o-4_5"
MINICPM_REVISION="eb4141de24550a3a1a4082f718be58f46b761728"

DO_ENV=1
DO_PREPROCESS_ENV=1
DO_WEIGHTS=1
DO_VERIFY=1
for arg in "$@"; do
    case "${arg}" in
        --skip-env) DO_ENV=0 ;;
        --skip-preprocess-env) DO_PREPROCESS_ENV=0 ;;
        --skip-weights) DO_WEIGHTS=0 ;;
        --no-verify) DO_VERIFY=0 ;;
        --hf-mirror) export HF_ENDPOINT="https://hf-mirror.com" ;;
        -h|--help)
            sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) echo "Unknown argument: ${arg}" >&2; exit 2 ;;
    esac
done

cd "${ROOT}"
step() { echo; echo "==> $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

step "[1/6] Checking the host"
for cmd in curl openssl sha256sum; do
    command -v "${cmd}" >/dev/null 2>&1 || die "missing required command: ${cmd}"
done
[[ -f "${LOCK_FILE}" ]] || die "missing ${LOCK_FILE}"
[[ -f "${MUSETALK_PREPROCESS_LOCK}" ]] || die "missing ${MUSETALK_PREPROCESS_LOCK}"
[[ -f "${WEIGHTS_MANIFEST}" ]] || die "missing ${WEIGHTS_MANIFEST}"
if command -v nvidia-smi >/dev/null 2>&1; then
    nvidia-smi --query-gpu=index,name,memory.total,memory.used --format=csv,noheader || true
else
    echo "WARNING: nvidia-smi not found; both services need an NVIDIA GPU (CUDA 12.8 driver, ~45 GB free VRAM on GPU 0)."
fi
free_gb="$(df -Pk "${ROOT}" | awk 'NR==2 {printf "%d", $4 / 1024 / 1024}')"
echo "Free disk at ${ROOT}: ${free_gb} GB (needs ~35 GB: venv ~8 GB + weights ~23 GB)"

# Package index shared by uv and pip.
INDEX_URL="${PIP_INDEX_URL:-}"
if [[ -z "${INDEX_URL}" ]] && command -v pip >/dev/null 2>&1; then
    INDEX_URL="$(pip config list 2>/dev/null \
        | sed -n "s/^global.index-url='\([^']*\)'.*/\1/p" | head -n 1)"
fi
INDEX_URL="${INDEX_URL:-https://pypi.org/simple}"
INDEX_HOST="$(echo "${INDEX_URL}" | sed -E 's#^[a-z]+://([^/:]+).*#\1#')"
# uv does not read pip.conf; point every uv call (venv seeding included) at the index.
export UV_INDEX_URL="${INDEX_URL}"
PIP_INDEX_ARGS=(--index-url "${INDEX_URL}")
if [[ "${INDEX_URL}" == http://* ]]; then
    export UV_INSECURE_HOST="${INDEX_HOST}"
    PIP_INDEX_ARGS+=(--trusted-host "${INDEX_HOST}")
fi

find_python() {
    if [[ -n "${AVATAR_PYTHON:-}" ]]; then
        echo "${AVATAR_PYTHON}"
    elif command -v python3.10 >/dev/null 2>&1; then
        command -v python3.10
    elif command -v uv >/dev/null 2>&1; then
        uv python find 3.10 2>/dev/null || { uv python install 3.10 >&2 && uv python find 3.10; }
    else
        return 1
    fi
}

if [[ "${DO_ENV}" -eq 1 ]]; then
    step "[2/6] Python environment: ${VENV_DIR}"
    if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
        BASE_PY="$(find_python)" || die "Python 3.10 not found; install python3.10 or uv, or set AVATAR_PYTHON"
        "${BASE_PY}" -c 'import sys; assert sys.version_info[:2] == (3, 10), sys.version' \
            || die "${BASE_PY} is not Python 3.10 (the lock file pins cp310 wheels)"
        if command -v uv >/dev/null 2>&1; then
            uv venv "${VENV_DIR}" --python "${BASE_PY}" --seed
        else
            "${BASE_PY}" -m venv "${VENV_DIR}"
        fi
    fi
    PY="${VENV_DIR}/bin/python"
    echo "Installing pinned packages from ${INDEX_URL}"
    # --no-deps: the lock is a known-good set; minicpmo-utils declares librosa==0.9.0
    # but runs with the pinned 0.11.0, so a resolver would reject it.
    if command -v uv >/dev/null 2>&1; then
        UV_LINK_MODE="${UV_LINK_MODE:-copy}" uv pip install --python "${PY}" --no-deps -r "${LOCK_FILE}"
    else
        "${PY}" -m pip install "${PIP_INDEX_ARGS[@]}" --upgrade pip
        "${PY}" -m pip install "${PIP_INDEX_ARGS[@]}" --no-deps -r "${LOCK_FILE}"
    fi
    "${PY}" - <<'PY'
import torch, torchvision, transformers, diffusers, cv2, soxr, fastapi
print(f"torch {torch.__version__} (CUDA {torch.version.cuda}, available={torch.cuda.is_available()}), "
      f"transformers {transformers.__version__}, diffusers {diffusers.__version__}, opencv {cv2.__version__}")
PY
else
    step "[2/6] Skipping Python environment (--skip-env)"
fi
PY="${VENV_DIR}/bin/python"
[[ -x "${PY}" ]] || die "Python environment is not usable: ${PY}"

if [[ "${DO_PREPROCESS_ENV}" -eq 1 ]]; then
    step "[3/6] MuseTalk preprocessing environment: ${MUSETALK_VENV_DIR}"
    MUSETALK_PY="${MUSETALK_VENV_DIR}/bin/python"
    if [[ ! -x "${MUSETALK_PY}" ]]; then
        BASE_PY="$(find_python)" || die "Python 3.10 not found for the MuseTalk preprocessing environment"
        "${BASE_PY}" -c 'import sys; assert sys.version_info[:2] == (3, 10), sys.version' \
            || die "${BASE_PY} is not Python 3.10"
        if command -v uv >/dev/null 2>&1; then
            uv venv "${MUSETALK_VENV_DIR}" --python "${BASE_PY}" --seed
        else
            "${BASE_PY}" -m venv "${MUSETALK_VENV_DIR}"
        fi
    fi
    MUSETALK_PY="${MUSETALK_VENV_DIR}/bin/python"
    [[ -x "${MUSETALK_PY}" ]] || die "MuseTalk Python environment is not usable: ${MUSETALK_PY}"

    MUSETALK_BASE_REQUIREMENTS=(
        "torch==2.1.0"
        "torchvision==0.16.0"
        "numpy==1.26.4"
        "mmengine==0.10.7"
        "mmcv==2.1.0"
        "mmdet==3.3.0"
        "mmpose==1.3.2"
    )
    if command -v uv >/dev/null 2>&1; then
        UV_LINK_MODE="${UV_LINK_MODE:-copy}" uv pip install \
            --python "${MUSETALK_PY}" "${MUSETALK_BASE_REQUIREMENTS[@]}"
        UV_LINK_MODE="${UV_LINK_MODE:-copy}" uv pip install \
            --python "${MUSETALK_PY}" --no-deps -r "${MUSETALK_PREPROCESS_LOCK}"
    else
        "${MUSETALK_PY}" -m pip install "${PIP_INDEX_ARGS[@]}" --upgrade pip
        "${MUSETALK_PY}" -m pip install "${PIP_INDEX_ARGS[@]}" "${MUSETALK_BASE_REQUIREMENTS[@]}"
        "${MUSETALK_PY}" -m pip install "${PIP_INDEX_ARGS[@]}" --no-deps -r "${MUSETALK_PREPROCESS_LOCK}"
    fi
    "${MUSETALK_PY}" -m pip check
    "${MUSETALK_PY}" - <<'PY'
import torch, torchvision, numpy, mmcv, mmpose
print(
    f"MuseTalk preprocess: torch {torch.__version__}, "
    f"torchvision {torchvision.__version__}, numpy {numpy.__version__}, "
    f"mmcv {mmcv.__version__}, mmpose {mmpose.__version__}"
)
PY
else
    step "[3/6] Skipping MuseTalk preprocessing environment (--skip-preprocess-env)"
fi

step "[4/6] ffmpeg, runtime directories and TLS certificate"
if command -v ffmpeg >/dev/null 2>&1 && ffmpeg -version >/dev/null 2>&1; then
    FFMPEG_EXE="$(command -v ffmpeg)"
else
    FFMPEG_EXE="$("${PY}" -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())')"
fi
"${FFMPEG_EXE}" -version >/dev/null 2>&1 || die "ffmpeg is not usable: ${FFMPEG_EXE}"
FFMPEG_ENCODERS="$("${FFMPEG_EXE}" -hide_banner -encoders 2>/dev/null)" \
    || die "failed to inspect ffmpeg encoders: ${FFMPEG_EXE}"
[[ "${FFMPEG_ENCODERS}" == *libx264* ]] \
    || die "ffmpeg has no libx264 encoder; H.264 avatar streaming requires it"
# MiniCPM-o's start_all.sh puts .bin on PATH; MuseTalk looks in ffmpeg-4.4-amd64-static.
mkdir -p "${MINICPM_DIR}/.bin" "${MUSETALK_DIR}/ffmpeg-4.4-amd64-static"
ln -sfn "${FFMPEG_EXE}" "${MINICPM_DIR}/.bin/ffmpeg"
ln -sfn "${FFMPEG_EXE}" "${MUSETALK_DIR}/ffmpeg-4.4-amd64-static/ffmpeg"
echo "ffmpeg: ${FFMPEG_EXE}"

mkdir -p "${ROOT}/.run/logs" "${ROOT}/.run/pids" \
    "${MINICPM_DIR}/data" "${MINICPM_DIR}/tmp/pids" "${MINICPM_DIR}/.run-logs"
CERT_DIR="${MINICPM_DIR}/certs"
if [[ ! -f "${CERT_DIR}/cert.pem" || ! -f "${CERT_DIR}/key.pem" ]] || \
   ! openssl x509 -checkend 86400 -noout -in "${CERT_DIR}/cert.pem" >/dev/null 2>&1; then
    mkdir -p "${CERT_DIR}"
    openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
        -keyout "${CERT_DIR}/key.pem" -out "${CERT_DIR}/cert.pem" \
        -subj "/CN=minicpmo45-avatar" >/dev/null 2>&1
    chmod 600 "${CERT_DIR}/key.pem"
    echo "Generated self-signed certificate in ${CERT_DIR}"
fi

# Weight files listed in weights.sha256, relative to ROOT.
mapfile -t WEIGHT_FILES < <(awk '{print $2}' "${WEIGHTS_MANIFEST}")

missing_under() {  # missing_under PREFIX -> prints missing manifest files under PREFIX
    local rel
    for rel in "${WEIGHT_FILES[@]}"; do
        [[ "${rel}" == "$1"* && ! -s "${ROOT}/${rel}" ]] && echo "${rel}"
    done
    return 0
}

hf_download() {  # hf_download REPO LOCAL_DIR [--revision REV] FILE...
    local repo="$1" dest="$2"; shift 2
    local rev=()
    if [[ "${1:-}" == "--revision" ]]; then rev=(--revision "$2"); shift 2; fi
    echo "Downloading ${repo} -> ${dest#"${ROOT}/"} ($# files) from ${HF_ENDPOINT:-https://huggingface.co}"
    mkdir -p "${dest}"
    "${VENV_DIR}/bin/huggingface-cli" download "${repo}" ${rev[@]+"${rev[@]}"} \
        --local-dir "${dest}" --include "$@"
}

file_sha256_matches() {
    local path="$1"
    local expected="$2"
    [[ -s "${path}" ]] || return 1
    [[ "$(sha256sum "${path}" | awk '{print $1}')" == "${expected}" ]]
}

ensure_s3fd() {
    local target="${ROOT}/${S3FD_REL}"
    local partial="${target}.part"
    local source_file
    local url="${S3FD_MODEL_URL:-${HF_ENDPOINT:-https://huggingface.co}/${S3FD_REPO_PATH}}"

    if file_sha256_matches "${target}" "${S3FD_SHA256}"; then
        echo "Present: ${S3FD_REL}"
        return 0
    fi
    mkdir -p "$(dirname "${target}")"
    if [[ -n "${AVATAR_WEIGHTS_FROM:-}" ]]; then
        source_file="${AVATAR_WEIGHTS_FROM}/${S3FD_REL}"
        [[ -s "${source_file}" ]] \
            || die "not found in AVATAR_WEIGHTS_FROM: ${S3FD_REL}"
        file_sha256_matches "${source_file}" "${S3FD_SHA256}" \
            || die "checksum mismatch in AVATAR_WEIGHTS_FROM: ${S3FD_REL}"
        ln -f "${source_file}" "${target}" 2>/dev/null \
            || cp -f "${source_file}" "${target}"
        echo "Linked: ${S3FD_REL}"
        return 0
    fi

    echo "Downloading S3FD face detector from ${url}"
    curl -L --fail --retry 5 --retry-delay 2 --continue-at - \
        --output "${partial}" "${url}"
    file_sha256_matches "${partial}" "${S3FD_SHA256}" \
        || die "S3FD checksum mismatch; partial file kept at ${partial}"
    mv -f "${partial}" "${target}"
    echo "Downloaded: ${S3FD_REL}"
}
fetch_group() {  # fetch_group MATCH_REL DEST_REL REPO [REVISION]
    # Missing manifest files under MATCH_REL are fetched from REPO into DEST_REL;
    # their repo paths are the manifest paths relative to DEST_REL.
    local match_rel="$1" dest_rel="$2" repo="$3" revision="${4:-}"
    local missing=() rel
    mapfile -t missing < <(missing_under "${match_rel}/")
    [[ ${#missing[@]} -eq 0 ]] && { echo "Present: ${match_rel}"; return 0; }
    if [[ -n "${AVATAR_WEIGHTS_FROM:-}" ]]; then
        for rel in "${missing[@]}"; do
            [[ -s "${AVATAR_WEIGHTS_FROM}/${rel}" ]] || die "not found in AVATAR_WEIGHTS_FROM: ${rel}"
            mkdir -p "$(dirname "${ROOT}/${rel}")"
            ln -f "${AVATAR_WEIGHTS_FROM}/${rel}" "${ROOT}/${rel}" 2>/dev/null \
                || cp -f "${AVATAR_WEIGHTS_FROM}/${rel}" "${ROOT}/${rel}"
        done
        echo "Linked ${#missing[@]} files into ${match_rel} from ${AVATAR_WEIGHTS_FROM}"
        return 0
    fi
    local files=()
    for rel in "${missing[@]}"; do files+=("${rel#"${dest_rel}/"}"); done
    if [[ -n "${revision}" ]]; then
        hf_download "${repo}" "${ROOT}/${dest_rel}" --revision "${revision}" "${files[@]}"
    else
        hf_download "${repo}" "${ROOT}/${dest_rel}" "${files[@]}"
    fi
}

if [[ "${DO_WEIGHTS}" -eq 1 ]]; then
    step "[5/6] Model weights"
    export HF_HOME="${HF_HOME:-${ROOT}/.cache/huggingface}"
    export HF_HUB_DISABLE_TELEMETRY=1
    MT="musetalk_project/MuseTalk/models"
    fetch_group "minicpm_demo/model" "minicpm_demo/model" "${MINICPM_REPO}" "${MINICPM_REVISION}"
    # MuseTalk weights needed at runtime with the bundled avatar cache.
    fetch_group "${MT}/musetalkV15" "${MT}" "TMElyralab/MuseTalk"
    fetch_group "${MT}/sd-vae" "${MT}/sd-vae" "stabilityai/sd-vae-ft-mse"
    fetch_group "${MT}/whisper" "${MT}/whisper" "openai/whisper-tiny"
    fetch_group "${MT}/face-parse-bisent" "${MT}/face-parse-bisent" "ManyOtherFunctions/face-parse-bisent"
    ensure_s3fd
else
    step "[5/6] Skipping weight download (--skip-weights)"
fi
step "[6/6] Checking weights and avatar cache"
mapfile -t still_missing < <(missing_under "")
if [[ ${#still_missing[@]} -gt 0 ]]; then
    printf '  missing: %s\n' "${still_missing[@]}" >&2
    die "${#still_missing[@]} weight files are missing; rerun ./install.sh (downloads resume)"
fi
if [[ "${DO_VERIFY}" -eq 1 ]]; then
    echo "Verifying sha256 of ${#WEIGHT_FILES[@]} weight files (takes a minute or two)..."
    sha256sum --quiet -c "${WEIGHTS_MANIFEST}" \
        || die "checksum mismatch; delete the files listed above and rerun ./install.sh"
    echo "Checksums OK"
fi
# The avatar cache is shipped in the package: preparing a new avatar needs
# mmpose/mmcv, which have no torch 2.8 build and are not in this venv.
for f in latents.pt coords.pkl mask_coords.pkl avator_info.json; do
    [[ -s "${AVATAR_CACHE}/${f}" ]] || die "avatar cache incomplete: ${AVATAR_CACHE}/${f}"
done
echo "Avatar cache OK: ${AVATAR_CACHE#"${ROOT}/"}"
if [[ "${DO_PREPROCESS_ENV}" -eq 1 ]]; then
    "${PY}" "${ROOT}/scripts/check_musetalk_preprocess.py" \
        --python "${MUSETALK_PY}" \
        --muse-dir "${MUSETALK_DIR}"
fi

chmod +x "${ROOT}/start_avatar.sh" "${ROOT}/add_avatar.sh" \
    "${MINICPM_DIR}/start_all.sh" "${MINICPM_DIR}/stop_all.sh" 2>/dev/null || true
echo
echo "Installation complete."
echo "  Python: ${PY}"
echo "  Start:  ./start_avatar.sh        (then open https://<host>:8006/avatar)"
echo "  Avatar: ./add_avatar.sh          (add an avatar from a source video)"
echo "  Stop:   ./start_avatar.sh stop"
