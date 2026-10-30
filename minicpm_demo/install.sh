#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${DEMO_VENV:-${PROJECT_DIR}/.venv/base}"
PYTHON="${DEMO_PYTHON:-python3.10}"
FFMPEG_BIN_DIR="${DEMO_FFMPEG_BIN_DIR:-${PROJECT_DIR}/.bin}"
START_AFTER_INSTALL=1

for arg in "$@"; do
    case "${arg}" in
        --no-start)
            START_AFTER_INSTALL=0
            ;;
        --help|-h)
            echo "Usage: ./install.sh [--no-start]"
            echo
            echo "Environment overrides:"
            echo "  DEMO_PYTHON       Python executable (default: python3.10)"
            echo "  DEMO_VENV         Reuse or install into an existing venv"
            echo "  DEMO_FFMPEG_BIN_DIR  Directory for the local ffmpeg wrapper"
            echo "  TORCH_INDEX_URL   PyTorch wheel index (default: cu128 index)"
            echo "  PIP_INDEX_URL     Index used for remaining dependencies"
            echo "  DEMO_PORT         Gateway port (default: 8006)"
            exit 0
            ;;
        *)
            echo "Unknown argument: ${arg}" >&2
            exit 2
            ;;
    esac
done

cd "${PROJECT_DIR}"

echo "[1/6] Checking bundled model files"
required_model_files=(
    model/config.json
    model/model.safetensors.index.json
    model/model-00001-of-00004.safetensors
    model/model-00002-of-00004.safetensors
    model/model-00003-of-00004.safetensors
    model/model-00004-of-00004.safetensors
    model/assets/token2wav/campplus.onnx
    model/assets/token2wav/flow.pt
    model/assets/token2wav/hift.pt
    model/assets/token2wav/speech_tokenizer_v2_25hz.onnx
)
for path in "${required_model_files[@]}"; do
    if [[ ! -f "${path}" ]]; then
        echo "Missing required bundled file: ${path}" >&2
        exit 1
    fi
done

echo "[2/6] Preparing Python environment: ${VENV_DIR}"
CONFIGURED_INDEX="${PIP_INDEX_URL:-}"
if [[ -z "${CONFIGURED_INDEX}" ]] && command -v pip >/dev/null 2>&1; then
    CONFIGURED_INDEX="$(
        pip config list 2>/dev/null \
            | sed -n "s/^global.index-url='\([^']*\)'.*/\1/p" \
            | head -n 1
    )"
fi

if [[ -x "${VENV_DIR}/bin/python" ]] && \
   ! "${VENV_DIR}/bin/python" -m pip --version >/dev/null 2>&1; then
    echo "Existing environment has no usable pip; recreating ${VENV_DIR}"
    rm -rf "${VENV_DIR}"
fi

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    if ! command -v "${PYTHON}" >/dev/null 2>&1; then
        if command -v python3 >/dev/null 2>&1; then
            PYTHON="python3"
        else
            echo "Python 3 not found. Install Python 3.10+ or set DEMO_PYTHON." >&2
            exit 1
        fi
    fi

    mkdir -p "$(dirname "${VENV_DIR}")"
    if ! "${PYTHON}" -m venv "${VENV_DIR}"; then
        echo "Standard venv creation failed; trying uv..."
        rm -rf "${VENV_DIR}"
        if command -v uv >/dev/null 2>&1; then
            if [[ -n "${CONFIGURED_INDEX}" ]]; then
                UV_DEFAULT_INDEX="${CONFIGURED_INDEX}" uv venv "${VENV_DIR}" \
                    --python "${PYTHON}" --seed
            else
                uv venv "${VENV_DIR}" --python "${PYTHON}" --seed
            fi
        else
            "${PYTHON}" -m pip install --user virtualenv
            "${PYTHON}" -m virtualenv "${VENV_DIR}"
        fi
    fi
fi

PY="${VENV_DIR}/bin/python"
if [[ ! -x "${PY}" ]]; then
    echo "Python environment is not usable: ${PY}" >&2
    exit 1
fi

echo "[3/6] Checking PyTorch"
if "${PY}" -c "import torch, torchaudio; assert torch.__version__.startswith('2.8')" >/dev/null 2>&1; then
    "${PY}" - <<'PY'
import torch
print(f"Using existing torch {torch.__version__}, CUDA available={torch.cuda.is_available()}")
PY
else
    if [[ -n "${TORCH_INDEX_URL:-}" ]]; then
        echo "Installing torch 2.8.0 / torchaudio 2.8.0 from ${TORCH_INDEX_URL}"
        "${PY}" -m pip install \
            --index-url "${TORCH_INDEX_URL}" \
            "torch==2.8.0" "torchaudio==2.8.0"
    else
        echo "Installing torch 2.8.0 / torchaudio 2.8.0 from configured pip index"
        "${PY}" -m pip install "torch==2.8.0" "torchaudio==2.8.0"
    fi
fi

echo "[4/6] Installing demo dependencies"
if ! "${PY}" -c \
    "import minicpmo.utils, decord, moviepy, onnxruntime, hyperpyyaml" \
    >/dev/null 2>&1; then
    "${PY}" -m pip install "minicpmo-utils[all]>=1.0.5,<=1.0.6"
fi
grep -vE '^[[:space:]]*minicpmo-utils(\[all\])?>=' requirements.txt \
    > /tmp/minicpmo-demo-requirements.txt
"${PY}" -m pip install -r /tmp/minicpmo-demo-requirements.txt

echo "[5/6] Preparing ffmpeg"
if command -v ffmpeg >/dev/null 2>&1 && ffmpeg -version >/dev/null 2>&1; then
    FFMPEG_EXE="$(type -P ffmpeg)"
    echo "Using system ffmpeg: ${FFMPEG_EXE}"
else
    echo "System ffmpeg not found; installing imageio-ffmpeg"
    "${PY}" -m pip install "imageio-ffmpeg>=0.6.0"
    FFMPEG_EXE="$(
        "${PY}" - <<'PY'
import imageio_ffmpeg
print(imageio_ffmpeg.get_ffmpeg_exe())
PY
    )"
fi

if [[ ! -x "${FFMPEG_EXE}" ]]; then
    echo "ffmpeg installation failed: ${FFMPEG_EXE} is not executable" >&2
    exit 1
fi

mkdir -p "${FFMPEG_BIN_DIR}"
ln -sfn "${FFMPEG_EXE}" "${FFMPEG_BIN_DIR}/ffmpeg"
export PATH="${FFMPEG_BIN_DIR}:${PATH}"
if ! "${FFMPEG_BIN_DIR}/ffmpeg" -version >/dev/null 2>&1; then
    echo "ffmpeg validation failed: ${FFMPEG_BIN_DIR}/ffmpeg" >&2
    exit 1
fi
echo "ffmpeg ready: ${FFMPEG_BIN_DIR}/ffmpeg"

echo "[6/6] Preparing runtime directories and TLS certificate"
mkdir -p data tmp/pids .run-logs
if [[ ! -f certs/cert.pem || ! -f certs/key.pem ]] || \
   ! openssl x509 -checkend 86400 -noout -in certs/cert.pem >/dev/null 2>&1; then
    mkdir -p certs
    openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
        -keyout certs/key.pem -out certs/cert.pem \
        -subj "/CN=minicpmo45-demo"
fi

echo
echo "Installation complete."
echo "  Project: ${PROJECT_DIR}"
echo "  Python:  ${PY}"
echo "  Model:   ${PROJECT_DIR}/model"

if [[ "${START_AFTER_INSTALL}" -eq 1 ]]; then
    echo
    echo "Starting services..."
    DEMO_VENV="${VENV_DIR}" "${PROJECT_DIR}/start_all.sh"
else
    echo "Start later with: cd ${PROJECT_DIR} && ./start_all.sh"
fi
