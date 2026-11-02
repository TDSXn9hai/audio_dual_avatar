"""Lightweight contracts for the digital-human entry and frame codec."""

from pathlib import Path
import sys

import cv2
import numpy as np
import yaml

DEMO_ROOT = Path(__file__).resolve().parents[1]
if str(DEMO_ROOT) not in sys.path:
    sys.path.insert(0, str(DEMO_ROOT))

from gateway_modules.app_registry import AppRegistry


def test_avatar_is_the_first_enabled_entry():
    apps = AppRegistry().get_enabled_apps()
    assert [(app.app_id, app.route) for app in apps[:2]] == [
        ("avatar", "/avatar"),
        ("omni", "/omni"),
    ]


def test_avatar_entry_assets_exist():
    avatar_dir = DEMO_ROOT / "static" / "avatar"
    for name in (
        "avatar.html",
        "avatar.css",
        "avatar-app.js",
        "avatar-player.js",
        "speech-interruptor.js",
        "avatar-poster.jpg",
    ):
        assert (avatar_dir / name).is_file(), name


def test_opencv_webp_round_trip():
    frame = np.zeros((864, 480, 3), dtype=np.uint8)
    ok, encoded = cv2.imencode(".webp", frame, [cv2.IMWRITE_WEBP_QUALITY, 88])
    assert ok
    assert encoded.tobytes()[8:12] == b"WEBP"
    assert cv2.imdecode(encoded, cv2.IMREAD_COLOR) is not None


def test_avatar_latency_parameters_stay_at_existing_values():
    root = DEMO_ROOT.parent
    config = yaml.safe_load(
        (root / "musetalk_project" / "MuseTalk" / "configs" / "streaming_duplex.yaml").read_text()
    )
    assert config["server"]["prebuffer"] == 32
    assert config["server"]["jpeg_quality"] == 88
    assert config["live"]["max_prefetch_seconds"] == 4
    player = (DEMO_ROOT / "static" / "avatar" / "avatar-player.js").read_text()
    assert "const CLIENT_PREBUFFER_FRAMES = 8;" in player
    assert "export const INTERRUPT_FLUSH_TIMEOUT_MS = 2500;" in player


def test_avatar_default_prompt_is_chinese_knowledge_sharing():
    app = (DEMO_ROOT / "static" / "avatar" / "avatar-app.js").read_text()
    page = (DEMO_ROOT / "static" / "avatar" / "avatar.html").read_text()
    assert "中文知识传播助手" in app
    assert "中文知识传播助手" in page
