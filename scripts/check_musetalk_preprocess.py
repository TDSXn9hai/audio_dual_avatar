#!/usr/bin/env python3
"""Validate the MuseTalk avatar-preprocessing environment without loading models."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


REQUIRED_IMPORTS = (
    "accelerate",
    "cv2",
    "diffusers",
    "einops",
    "face_detection",
    "ffmpeg",
    "huggingface_hub",
    "librosa",
    "mmcv",
    "mmpose",
    "omegaconf",
    "soundfile",
    "torch",
    "transformers",
)

REQUIRED_FILES = (
    "models/dwpose/dw-ll_ucoco_384.pth",
    "models/face-parse-bisent/79999_iter.pth",
    "models/face-parse-bisent/resnet18-5c106cde.pth",
    "models/musetalkV15/musetalk.json",
    "models/musetalkV15/unet.pth",
    "models/sd-vae/config.json",
    "models/sd-vae/diffusion_pytorch_model.bin",
    "models/whisper/config.json",
    "models/whisper/preprocessor_config.json",
    "models/whisper/pytorch_model.bin",
    "musetalk/utils/face_detection/detection/sfd/s3fd.pth",
)


def _probe_imports(python: Path, muse_dir: Path) -> list[str]:
    code = """
import importlib.util
import json
import sys

missing = [name for name in sys.argv[1:] if importlib.util.find_spec(name) is None]
print(json.dumps(missing))
"""
    env = os.environ.copy()
    python_path = [str(muse_dir), str(muse_dir / "musetalk" / "utils")]
    if env.get("PYTHONPATH"):
        python_path.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(python_path)
    result = subprocess.run(
        [str(python), "-c", code, *REQUIRED_IMPORTS],
        cwd=muse_dir,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"dependency probe failed: {detail}")
    return json.loads(result.stdout)


def _report(
    *,
    muse_dir: Path,
    python: Path,
    skip_imports: bool,
) -> dict[str, object]:
    missing_files = [
        path for path in REQUIRED_FILES if not (muse_dir / path).is_file()
    ]
    missing_imports: list[str] = []
    probe_error: str | None = None
    if not skip_imports:
        try:
            missing_imports = _probe_imports(python, muse_dir)
        except Exception as exc:
            probe_error = str(exc)
    return {
        "ok": not missing_files and not missing_imports and probe_error is None,
        "python": str(python),
        "muse_dir": str(muse_dir),
        "missing_files": missing_files,
        "missing_imports": missing_imports,
        "probe_error": probe_error,
    }


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--python",
        type=Path,
        default=root / "musetalk_project" / "venv" / "bin" / "python",
    )
    parser.add_argument(
        "--muse-dir",
        type=Path,
        default=root / "musetalk_project" / "MuseTalk",
    )
    parser.add_argument("--skip-imports", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    report = _report(
        muse_dir=args.muse_dir.resolve(),
        python=args.python.absolute(),
        skip_imports=args.skip_imports,
    )
    if args.json:
        print(json.dumps(report, indent=2))
    elif report["ok"]:
        print("MuseTalk preprocessing environment OK")
    else:
        print("MuseTalk preprocessing environment is incomplete:", file=sys.stderr)
        for path in report["missing_files"]:
            print(f"  missing file: {path}", file=sys.stderr)
        for name in report["missing_imports"]:
            print(f"  missing import: {name}", file=sys.stderr)
        if report["probe_error"]:
            print(f"  probe error: {report['probe_error']}", file=sys.stderr)

    raise SystemExit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
