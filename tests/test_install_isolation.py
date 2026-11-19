from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_root_installer_provisions_project_local_gpen_environment_and_env_only_mode():
    text = (ROOT / "install.sh").read_text()
    assert 'GPEN_VENV' in text
    assert 'gpen-venv' in text
    assert '--env-only' in text
    assert 'imageio_ffmpeg' in text


def test_root_installer_does_not_select_system_ffmpeg_first():
    text = (ROOT / "install.sh").read_text()
    assert 'command -v ffmpeg' not in text


def test_minicpm_installer_delegates_to_root_installer():
    text = (ROOT / "minicpm_demo/install.sh").read_text()
    assert 'ROOT_INSTALLER' in text
    assert 'install.sh' in text
    assert 'DEMO_VENV' in text


def test_gpen_alignment_does_not_inject_runtime_site_packages():
    text = (ROOT / "add_avatar.sh").read_text()
    assert 'GPEN_SITE_PACKAGES' not in text
    assert 'PYTHONPATH="${SHARED_VENV}/lib/python3.10/site-packages"' not in text


def test_gpen_alignment_tool_is_kept_under_gpen_namespace():
    assert (ROOT / "musetalk_project/MuseTalk/muse_stream/inference/gpen/prepare_alignment.py").is_file()
    assert not (ROOT / "deployment/prepare_gpen_alignment.py").exists()
    assert "parents[3]" in (ROOT / "musetalk_project/MuseTalk/muse_stream/inference/gpen/prepare_alignment.py").read_text()
