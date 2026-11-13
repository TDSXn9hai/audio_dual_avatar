import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts" / "check_musetalk_preprocess.py"


def test_checker_reports_missing_assets(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "--muse-dir",
            str(tmp_path),
            "--skip-imports",
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    report = json.loads(result.stdout)
    missing = "\n".join(report["missing_files"])
    assert "models/dwpose/dw-ll_ucoco_384.pth" in missing
    assert (
        "musetalk/utils/face_detection/detection/sfd/s3fd.pth"
        in missing
    )


def test_checker_uses_the_selected_virtualenv() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    report = json.loads(result.stdout)
    assert report["missing_imports"] == []
    assert report["missing_files"] == []


def test_add_avatar_checks_environment_before_runtime_writes(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    env = os.environ.copy()
    env["MUSETALK_VENV"] = str(tmp_path / "missing-venv")
    env["AVATAR_RUNTIME_ROOT"] = str(runtime_root)
    result = subprocess.run(
        [
            str(ROOT / "add_avatar.sh"),
            "--runtime",
            "minicpm_demo/static/test_demo.mp4",
            "preflight_failure_audit",
            "minicpm_demo/assets/ref_audio/ref_avatar_default.wav",
            "minicpm_demo/assets/prompts/avatar/knowledge_host.txt",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "missing MuseTalk venv activation script" in result.stderr
    assert not runtime_root.exists()
