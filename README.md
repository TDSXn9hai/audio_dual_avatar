# audio_dual_avatar

Based on MuseTalk and MiniCPM.

Full-duplex digital human: [MiniCPM-o 4.5](https://huggingface.co/openbmb/MiniCPM-o-4_5) listens and speaks in real time, and [MuseTalk](https://github.com/TMElyralab/MuseTalk) renders a lip-synced avatar from its streaming voice output.

This repository contains code only. Model weights (~23 GB) are downloaded by `install.sh`.

## Requirements

- Linux x86_64, NVIDIA GPU with a CUDA 12.8 driver and ~45 GB free VRAM on GPU 0
- Python 3.10 (or `uv`, which can provide it), `curl`, `openssl`
- ~35 GB free disk; access to a Python package index and Hugging Face (or a mirror)

## Install and run

```bash
./install.sh          # venv + pinned packages, ffmpeg, TLS cert, weights + sha256 check
                      # options: --hf-mirror --skip-env --skip-weights --no-verify
./start_avatar.sh     # then open https://<host>:8006/avatar
./start_avatar.sh stop
```

### Default 720p knowledge-sharing avatar

`./start_avatar.sh` now starts the 720p knowledge-sharing avatar by default.
The original `musetalk_test_avatar` configuration and cache remain available as
a fallback.

```bash
# Build the separate avatar cache once with the mmpose-capable venv.
cd musetalk_project/MuseTalk
CUDA_VISIBLE_DEVICES=2 PYTHONPATH=. PATH="../../minicpm_demo/.bin:$PATH" \
  ../venv/bin/python scripts/realtime_inference.py \
  --version v15 --gpu_id 0 \
  --unet_config ./models/musetalkV15/musetalk.json \
  --unet_model_path ./models/musetalkV15/unet.pth \
  --whisper_dir ./models/whisper \
  --inference_config configs/inference/prepare_knowledge_host.yaml \
  --parsing_mode jaw --left_cheek_width 90 --right_cheek_width 90

# Start the full stack with the new default avatar.
cd ../..
./start_avatar.sh start

# Start with the original avatar cache as a fallback.
MUSETALK_CONFIG=configs/streaming_duplex.yaml ./start_avatar.sh start
```

The cache is generated under
`musetalk_project/MuseTalk/results/v15/avatars/knowledge_host_720p/`.

## Layout

- `minicpm_demo/` — MiniCPM-o 4.5 full-duplex demo (gateway, worker, backend, web UI) with the avatar bridge and the `/avatar` page
- `musetalk_project/MuseTalk/` — streaming MuseTalk server (`muse_stream/`) with the `/ws/minicpm` input
- `install.sh`, `start_avatar.sh` — one-click setup and launcher
- `requirements-avatar.lock.txt`, `weights.sha256` — pinned packages and weight checksums

Design notes, scheduling, bridge and frontend details (Chinese): [DIGITAL_HUMAN_INTEGRATION.md](DIGITAL_HUMAN_INTEGRATION.md).

Platform integration and API handoff guide (Chinese): [PLATFORM_INTEGRATION_GUIDE.md](PLATFORM_INTEGRATION_GUIDE.md).
