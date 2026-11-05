from __future__ import annotations

import importlib.util
import json
import ssl
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient


APP_DIR = Path(__file__).resolve().parents[1]


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "minimal_platform_app",
        APP_DIR / "app.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


platform_app = _load_module()


def _settings(**overrides):
    values = {
        "avatar_api_base_url": "https://avatar.internal:8006",
        "avatar_api_key": "upstream-secret",
        "avatar_ca_bundle": None,
        "upstream_timeout_seconds": 10,
        "max_request_body_bytes": 65536,
        "websocket_max_message_bytes": 16 * 1024 * 1024,
        "allowed_origins": (),
    }
    values.update(overrides)
    return platform_app.Settings(**values)


def test_presets_proxy_injects_api_key_and_preserves_json() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "default_preset_id": "knowledge_host_720p",
                "presets": [{"id": "knowledge_host_720p", "available": True}],
            },
        )

    app = platform_app.create_app(_settings(), transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = client.get("/api/avatar/presets")

    assert response.status_code == 200
    assert response.json()["presets"][0]["available"] is True
    assert seen == {
        "method": "GET",
        "path": "/v1/avatar/presets",
        "authorization": "Bearer upstream-secret",
    }


def test_create_session_rewrites_stream_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/avatar/sessions"
        assert json.loads(request.content)["preset_id"] == "knowledge_host_720p"
        return httpx.Response(
            201,
            json={
                "session_id": "avs_test123",
                "stream_url": (
                    "/v1/avatar/sessions/avs_test123/stream?token=secret-token"
                ),
                "preset": {"id": "knowledge_host_720p"},
            },
        )

    app = platform_app.create_app(_settings(), transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = client.post(
            "/api/avatar/sessions",
            json={"preset_id": "knowledge_host_720p"},
        )

    assert response.status_code == 201
    assert response.json()["stream_url"] == (
        "/api/avatar/sessions/avs_test123/stream?token=secret-token"
    )


def test_upstream_tls_error_is_mapped_to_502() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        tls_error = ssl.SSLCertVerificationError("self-signed certificate")
        error = httpx.ConnectError("TLS validation failed", request=request)
        error.__cause__ = tls_error
        raise error

    app = platform_app.create_app(_settings(), transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = client.get("/api/avatar/presets")

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "upstream_tls_error"


def test_invalid_upstream_session_id_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "session_id": "../../escape",
                "stream_url": "https://invalid.example/",
            },
        )

    app = platform_app.create_app(_settings(), transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = client.post("/api/avatar/sessions", json={})

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "invalid_upstream_response"


def test_request_body_limit() -> None:
    app = platform_app.create_app(
        _settings(max_request_body_bytes=16),
        transport=httpx.MockTransport(
            lambda request: pytest.fail("upstream must not be called")
        ),
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/avatar/sessions",
            content=b'{"preset_id":"too-long"}',
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "request_too_large"


def test_health_and_static_page() -> None:
    app = platform_app.create_app(_settings())
    with TestClient(app) as client:
        assert client.get("/healthz").json()["status"] == "ok"
        page = client.get("/")
        assert page.status_code == 200
        assert "Minimal Avatar Platform" in page.text
        sdk = client.get("/static/avatar-session-client.js")
        assert sdk.status_code == 200
        assert "AvatarSessionClient" in sdk.text


def test_vendored_sdk_matches_public_source() -> None:
    project_root = APP_DIR.parents[1]
    canonical = (
        project_root
        / "minicpm_demo"
        / "static"
        / "sdk"
        / "avatar-session-client.js"
    )
    vendored = APP_DIR / "static" / "avatar-session-client.js"
    assert vendored.read_bytes() == canonical.read_bytes()
