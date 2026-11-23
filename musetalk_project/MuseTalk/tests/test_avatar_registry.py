from __future__ import annotations

import json
from argparse import Namespace
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from muse_stream.api.app import create_app
from muse_stream.avatar_modes import AvatarBaseBundle, AvatarBaseController
from muse_stream.avatar_registry import (
    AvatarBusyError,
    AvatarActivationError,
    AvatarCacheInfo,
    AvatarInvalidError,
    activate_avatar,
    ensure_avatar_base_caches,
    inspect_avatar_cache,
    list_avatar_caches,
)
from muse_stream.config import AvatarVariantConfig
from muse_stream.models import cache_dir_for
from muse_stream.state import AppState


def _require_cache(avatar_id: str) -> None:
    if not cache_dir_for(avatar_id, "v15").is_dir():
        pytest.skip(f"local avatar cache is unavailable: {avatar_id}")


def _state() -> AppState:
    state = AppState(
        args=Namespace(version="v15", batch_size=28),
    )
    state.service.ready = True
    state.model.runtime = SimpleNamespace()
    state.model.default_avatar_id = "knowledge_host_720p"
    return state


def test_existing_local_avatar_caches_are_discoverable() -> None:
    _require_cache("knowledge_host_720p")
    _require_cache("musetalk_test_avatar")

    caches = list_avatar_caches("v15", "knowledge_host_720p")
    by_id = {cache.id: cache for cache in caches}

    assert by_id["knowledge_host_720p"].valid
    assert by_id["knowledge_host_720p"].width == 768
    assert by_id["knowledge_host_720p"].height == 1344
    assert by_id["knowledge_host_720p"].frame_count == 690

    assert by_id["musetalk_test_avatar"].valid
    assert by_id["musetalk_test_avatar"].width == 480
    assert by_id["musetalk_test_avatar"].height == 864
    assert by_id["musetalk_test_avatar"].frame_count == 690
    assert caches[0].id == "knowledge_host_720p"


def test_unknown_cache_is_reported_invalid() -> None:
    info = inspect_avatar_cache("does-not-exist", "v15")
    assert not info.valid
    assert "not found" in (info.error or "").lower()


def test_activation_uses_named_cache_and_default_fallback(monkeypatch) -> None:
    _require_cache("knowledge_host_720p")
    _require_cache("musetalk_test_avatar")
    state = _state()
    loaded: list[str] = []

    def fake_load(args, runtime, avatar_id):
        loaded.append(avatar_id)
        size = (864, 480, 3) if avatar_id == "musetalk_test_avatar" else (1344, 768, 3)
        return SimpleNamespace(frame_list_cycle=[np.zeros(size, dtype=np.uint8)])

    monkeypatch.setattr(
        "muse_stream.avatar_registry.load_cached_avatar",
        fake_load,
    )

    selected = activate_avatar(state, "musetalk_test_avatar")
    assert selected.id == "musetalk_test_avatar"
    assert state.model.avatar_id == "musetalk_test_avatar"
    assert state.model.avatar_revision == 1
    assert (state.model.width, state.model.height) == (480, 864)

    selected = activate_avatar(state, None)
    assert selected.id == "knowledge_host_720p"
    assert state.model.avatar_id == "knowledge_host_720p"
    assert state.model.avatar_revision == 2
    assert (state.model.width, state.model.height) == (768, 1344)
    assert loaded == ["musetalk_test_avatar", "knowledge_host_720p"]


def test_activation_rejects_switch_while_viewer_is_connected(monkeypatch) -> None:
    _require_cache("knowledge_host_720p")
    _require_cache("musetalk_test_avatar")
    state = _state()
    state.model.avatar_id = "knowledge_host_720p"
    state.model.avatar = SimpleNamespace(
        frame_list_cycle=[np.zeros((1344, 768, 3), dtype=np.uint8)]
    )
    state.model.avatar_ready = True
    state.viewer.connected = True

    monkeypatch.setattr(
        "muse_stream.avatar_registry.load_cached_avatar",
        lambda *args: pytest.fail("must not load while viewer is connected"),
    )

    with pytest.raises(AvatarBusyError):
        activate_avatar(state, "musetalk_test_avatar")

    assert state.model.avatar_id == "knowledge_host_720p"


def test_same_avatar_is_idempotent_even_with_viewer(monkeypatch) -> None:
    _require_cache("knowledge_host_720p")
    state = _state()
    state.model.avatar_id = "knowledge_host_720p"
    state.model.avatar = SimpleNamespace(
        frame_list_cycle=[np.zeros((1344, 768, 3), dtype=np.uint8)]
    )
    state.model.avatar_ready = True
    state.model.avatar_revision = 7
    state.viewer.connected = True

    monkeypatch.setattr(
        "muse_stream.avatar_registry.load_cached_avatar",
        lambda *args: pytest.fail("same avatar must not be reloaded"),
    )

    info = activate_avatar(state, "knowledge_host_720p")
    assert info.valid
    assert state.model.avatar_revision == 7


def test_avatar_http_api_lists_and_selects_cache(monkeypatch) -> None:
    _require_cache("knowledge_host_720p")
    _require_cache("musetalk_test_avatar")
    state = _state()
    state.model.avatar_id = "knowledge_host_720p"
    state.model.avatar = SimpleNamespace(
        frame_list_cycle=[np.zeros((1344, 768, 3), dtype=np.uint8)]
    )
    state.model.avatar_ready = True
    state.model.avatar_revision = 1

    monkeypatch.setattr(
        "muse_stream.avatar_registry.load_cached_avatar",
        lambda args, runtime, avatar_id: SimpleNamespace(
            frame_list_cycle=[
                np.zeros(
                    (864, 480, 3)
                    if avatar_id == "musetalk_test_avatar"
                    else (1344, 768, 3),
                    dtype=np.uint8,
                )
            ]
        ),
    )
    client = TestClient(create_app(state))

    listed = client.get("/api/avatars")
    assert listed.status_code == 200
    payload = listed.json()
    assert payload["default_avatar_id"] == "knowledge_host_720p"
    assert {item["id"] for item in payload["avatars"]} >= {
        "knowledge_host_720p",
        "musetalk_test_avatar",
    }

    selected = client.post(
        "/api/avatar/select",
        json={"avatar_id": "musetalk_test_avatar"},
    )
    assert selected.status_code == 200
    assert selected.json()["active_avatar_id"] == "musetalk_test_avatar"
    assert selected.json()["width"] == 480
    assert selected.json()["height"] == 864

    restored = client.post("/api/avatar/select")
    assert restored.status_code == 200
    assert restored.json()["active_avatar_id"] == "knowledge_host_720p"
    assert restored.json()["width"] == 768

    missing = client.post(
        "/api/avatar/select",
        json={"avatar_id": "missing_avatar"},
    )
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "avatar_not_found"


def test_duplex_avatar_status_and_poster_use_active_listen_base(tmp_path) -> None:
    state = _state()
    state.args = Namespace(
        version="v15",
        batch_size=28,
        audio_dir="",
        idle_timeout_seconds=30,
        interrupt_wait_seconds=10,
        interrupt_resume_mode="restart",
        max_prefetch_seconds=4,
        input_mode="stream",
        stream_context_left_ms=200,
        stream_context_right_ms=167,
        stream_overlap_frames=4,
        stream_pause_overlap_frames=4,
        stream_pause_boundary_frames=6,
        feature_window_mode="legacy",
        feature_window_left=4,
        feature_window_right=6,
    )
    poster = tmp_path / "full_imgs" / "00000000.png"
    poster.parent.mkdir()
    Image.new("RGB", (8, 12), color=(12, 34, 56)).save(poster)
    avatar = SimpleNamespace(full_imgs_path=str(poster.parent))
    listen = AvatarBaseBundle(
        base="listen",
        variant_id="listen-1",
        cycle_length=107,
        avatar=avatar,
    )
    speak = AvatarBaseBundle(
        base="speak",
        variant_id="speak-1",
        cycle_length=107,
        avatar=SimpleNamespace(full_imgs_path=str(tmp_path / "speak")),
    )
    state.model.avatar_base_controller = AvatarBaseController(
        (listen, speak),
        initial_base="listen",
    )
    state.model.avatar_ready = True
    state.model.avatar_id = "duplex_listen_1"
    client = TestClient(create_app(state))

    status = client.get("/api/status")
    assert status.status_code == 200
    assert status.json()["active_base"] == "listen"
    assert status.json()["active_variant_id"] == "listen-1"
    assert status.json()["pending_base"] is None

    response = client.get("/api/avatar/poster")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/png")


def test_failed_activation_rolls_back_previous_avatar(monkeypatch) -> None:
    state = _state()
    state.model.avatar_id = "knowledge_host_720p"
    state.model.avatar = SimpleNamespace(
        frame_list_cycle=[np.zeros((1344, 768, 3), dtype=np.uint8)]
    )
    state.model.avatar_ready = True
    state.model.avatar_revision = 3

    def fake_inspect(avatar_id, version):
        return AvatarCacheInfo(
            id=avatar_id,
            valid=True,
            width=480,
            height=864,
            frame_count=690,
            source_frame_count=345,
        )

    def fake_load(args, runtime, avatar_id):
        if avatar_id == "broken_avatar":
            raise RuntimeError("synthetic cache failure")
        return SimpleNamespace(
            frame_list_cycle=[np.zeros((1344, 768, 3), dtype=np.uint8)]
        )

    monkeypatch.setattr(
        "muse_stream.avatar_registry.inspect_avatar_cache",
        fake_inspect,
    )
    monkeypatch.setattr(
        "muse_stream.avatar_registry.load_cached_avatar",
        fake_load,
    )

    with pytest.raises(AvatarActivationError):
        activate_avatar(state, "broken_avatar")

    assert state.model.avatar_id == "knowledge_host_720p"
    assert state.model.avatar_ready
    assert state.model.avatar_revision == 3


def test_duplex_preset_can_switch_to_knowledge_and_back(monkeypatch, tmp_path) -> None:
    state = _state()
    state.model.default_avatar_id = "duplex_motion"
    state.model.avatar_base_configs = {
        "listen": (AvatarVariantConfig("listen", "listen-1", "duplex_listen_1", loop_mode="forward"),),
        "speak": (AvatarVariantConfig("speak", "speak-1", "duplex_speak_1", loop_mode="forward"),),
    }

    def inspect(cache_id, version):
        return AvatarCacheInfo(cache_id, True, 768, 1344, 107, 107)

    def load(args, runtime, cache_id):
        if cache_id == "duplex_motion":
            pytest.fail("The duplex preset must load its component caches")
        return SimpleNamespace(
            avatar_id=cache_id,
            frame_list_cycle=[np.zeros((12, 8, 3), dtype=np.uint8)],
        )

    monkeypatch.setattr("muse_stream.avatar_registry.inspect_avatar_cache", inspect)
    monkeypatch.setattr("muse_stream.avatar_registry.load_cached_avatar", load)
    monkeypatch.setattr("muse_stream.models.load_cached_avatar", load)
    monkeypatch.setattr("muse_stream.models.cache_dir_for", lambda avatar_id, version: tmp_path / avatar_id)
    monkeypatch.setattr("muse_stream.avatar_registry.cache_dir_for", lambda avatar_id, version: tmp_path / avatar_id)
    for cache_id in ("duplex_listen_1", "duplex_speak_1"):
        _write_cache_info(tmp_path / cache_id, cache_id)
    monkeypatch.setattr("muse_stream.api.routes.list_avatar_caches", lambda *args: [])
    client = TestClient(create_app(state))

    listing = client.get("/api/avatars").json()
    assert listing["avatars"][0]["id"] == "duplex_motion"
    assert listing["avatars"][0]["valid"]

    def select(avatar_id, mode):
        payload = {} if avatar_id is None else {"avatar_id": avatar_id}
        selected = client.post("/api/avatar/select", json=payload)
        assert selected.status_code == 200, selected.text
        assert client.get("/api/avatar").json()["avatar_mode"] == mode
        return selected.json()["active_avatar_id"]

    assert select(None, "duplex") == "duplex_motion"
    controller = state.model.avatar_base_controller
    controller.observe_model_mode("speak")
    assert controller.next_frame(1, speech=True).variant_id == "speak-1"
    assert select("knowledge_host_720p", "single") == "knowledge_host_720p"
    assert state.model.avatar.avatar_id == "knowledge_host_720p"
    assert select(None, "duplex") == "duplex_motion"
    assert client.get("/api/avatar").json()["active_variant_id"] == "listen-1"


def _base_state(tmp_path, preparation: str, count: int = 3) -> AppState:
    state = _state()
    state.model.default_avatar_id = "duplex_motion"
    state.model.avatar_base_configs = {
        base: tuple(
            AvatarVariantConfig(
                base,
                f"{base}-{index}",
                f"duplex_{base}_{index}",
                video_path=f"data/video/duplex_{base}_{index}.mp4",
                preparation=preparation,
                loop_mode="forward",
            )
            for index in range(1, count + 1)
        )
        for base in ("listen", "speak")
    }
    return state


def _write_cache_info(cache_dir, cache_id: str, loop_mode: str = "forward") -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "avator_info.json").write_text(
        json.dumps({
            "avatar_id": cache_id,
            "video_path": f"data/video/{cache_id}.mp4",
            "bbox_shift": 0,
            "version": "v15",
            "loop_mode": loop_mode,
        }),
        encoding="utf-8",
    )


def test_ensure_base_caches_generates_only_unusable_variants(
    monkeypatch, tmp_path
) -> None:
    state = _base_state(tmp_path, "auto")
    ready = {"duplex_listen_1", "duplex_speak_2"}
    # A cache with the wrong loop_mode must be regenerated too.
    _write_cache_info(tmp_path / "duplex_listen_1", "duplex_listen_1")
    _write_cache_info(tmp_path / "duplex_speak_2", "duplex_speak_2")
    _write_cache_info(tmp_path / "duplex_speak_3", "duplex_speak_3", "pingpong")
    ready.add("duplex_speak_3")
    prepared: list[str] = []

    def inspect(cache_id, version):
        return AvatarCacheInfo(cache_id, cache_id in ready, 8, 12, 10, 10,
                               None if cache_id in ready else "missing")

    def prepare(args, runtime, variant):
        prepared.append(variant.cache_id)
        ready.add(variant.cache_id)
        _write_cache_info(tmp_path / variant.cache_id, variant.cache_id)

    monkeypatch.setattr("muse_stream.avatar_registry.inspect_avatar_cache", inspect)
    monkeypatch.setattr("muse_stream.avatar_registry.prepare_avatar_base", prepare)
    monkeypatch.setattr(
        "muse_stream.avatar_registry.cache_dir_for",
        lambda avatar_id, version: tmp_path / avatar_id,
    )

    ensure_avatar_base_caches(state)
    assert prepared == [
        "duplex_listen_2", "duplex_listen_3", "duplex_speak_1", "duplex_speak_3",
    ]

    prepared.clear()
    ensure_avatar_base_caches(state)
    assert prepared == []


def test_ensure_base_caches_rejects_missing_reuse_variant(
    monkeypatch, tmp_path
) -> None:
    state = _base_state(tmp_path, "reuse", count=1)
    monkeypatch.setattr(
        "muse_stream.avatar_registry.inspect_avatar_cache",
        lambda cache_id, version: AvatarCacheInfo(cache_id, False, error="missing"),
    )
    monkeypatch.setattr(
        "muse_stream.avatar_registry.prepare_avatar_base",
        lambda *args: pytest.fail("reuse variants must not be generated"),
    )

    with pytest.raises(AvatarInvalidError, match="listen-1"):
        ensure_avatar_base_caches(state)
