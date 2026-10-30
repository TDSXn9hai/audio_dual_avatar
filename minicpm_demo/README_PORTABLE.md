# Portable MiniCPM-o 4.5 Demo Bundle

This directory is self-contained: it includes the official PyTorch demo source,
the Hugging Face model weights, Token2Wav assets, and sample media. Installation
only needs Python dependencies; it does not download model weights or demo code.

## Requirements

- Linux
- NVIDIA GPU with driver compatible with CUDA 12.8
- At least 28 GB GPU VRAM; 30+ GB recommended
- Python 3.10+ (3.10 or 3.12 recommended)
- Network access to a Python package index
- `openssl` and `curl`
- `ffmpeg` is installed automatically when absent

## One-click install and start

```bash
cd /path/to/demo_project
chmod +x install.sh start_all.sh stop_all.sh status.sh
./install.sh
```

The installer:

1. validates bundled model files;
2. creates `.venv/base`;
3. installs torch 2.8.0 + torchaudio 2.8.0 unless already available;
4. installs the official demo dependencies;
5. installs a static ffmpeg through `imageio-ffmpeg` when the system has none;
6. generates a self-signed TLS certificate if needed;
7. starts Gateway, Backend, and Worker.

The official `minicpmo-utils` and demo requirements currently disagree about
the librosa version (`==0.9.0` versus `>=0.10.2`). The installer follows the
official Dockerfile's two-stage approach and allows the newer librosa version.
This produces a pip conflict warning but has been verified with the demo.

Install only:

```bash
./install.sh --no-start
```

## Package index overrides

```bash
PIP_INDEX_URL=http://your-pip-index/simple ./install.sh

PIP_INDEX_URL=http://your-pip-index/simple \
TORCH_INDEX_URL=https://download.pytorch.org/whl/cu128 \
./install.sh

# Optional: choose where install.sh writes the local ffmpeg wrapper
DEMO_FFMPEG_BIN_DIR=/path/to/bin ./install.sh
```

For a CPU-only PyTorch mirror, point `TORCH_INDEX_URL` to that mirror. GPU
inference still requires a CUDA-capable torch build.

## Access

The default gateway listens on `0.0.0.0:8006`.

```text
https://<host>:8006/
https://<host>:8006/omni
https://<host>:8006/audio_duplex
https://<host>:8006/turnbased
```

The certificate is self-signed. Accept it explicitly in the browser. Camera and
microphone access requires HTTPS or localhost.

## Lifecycle

```bash
./status.sh
./stop_all.sh
./start_all.sh
```

## Packaging

Use the provided script so the machine-specific `.venv`, logs, recordings, and
temporary files are excluded:

```bash
./package.sh /path/to/minicpm-o-demo-project.tar
```

The archive contains the official code, model weights, Token2Wav resources, and
sample media. It does not contain the virtual environment.

On another Linux image:

```bash
tar -xf minicpm-o-demo-project.tar
cd demo_project
./install.sh
```

Verify bundled files before or after transfer:

```bash
sha256sum -c MANIFEST.sha256
```

Logs:

```text
.run-logs/gateway.log
.run-logs/backend.log
.run-logs/worker.log
```

## Included sample data

- `sample_data/Skiing.mp4`
- `sample_data/video_question.wav`
- `sample_data/video_followup_1.wav`
- `sample_data/video_followup_2.wav`

## Included model data

- four safetensors shards (~18.7 GB total)
- tokenizer/config/model code
- `model/assets/token2wav/` Token2Wav resources

The unnecessary source archive and unrelated experiment outputs are not included.

The installer was verified in a fresh environment with:

- torch 2.8.0+cu128, CUDA available
- Transformers 4.51.0
- FastAPI / Uvicorn / WebSockets
- Gateway, PyTorch backend, and Worker healthy
- a real full-duplex video+speech WebSocket request completed successfully

## Notes

- The demo expects one GPU per Worker. The default scripts start one Worker on
  GPU 0.
- `config.json` refers to `model` relative to this directory.
- `start_all.sh` supports `DEMO_VENV=/path/to/venv` for images that provide a
  prepared environment.
