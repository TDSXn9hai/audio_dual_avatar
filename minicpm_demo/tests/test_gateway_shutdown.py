"""Gateway SIGTERM must finish even when a client leaves a request unfinished."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("unfinished_request", [False, True])
def test_gateway_sigterm(tmp_path, unfinished_request):
    reservations = [socket.socket(), socket.socket()]
    for reservation in reservations:
        reservation.bind(("127.0.0.1", 0))
    public_port, internal_port = [s.getsockname()[1] for s in reservations]
    for reservation in reservations:
        reservation.close()
    env = os.environ.copy()
    env["AVATAR_CUSTOMIZATION_ENABLED"] = "false"
    env["AVATAR_BRIDGE_URL"] = "ws://127.0.0.1:8000/ws/minicpm"
    env["AVATAR_HTTP_URL"] = "http://127.0.0.1:8000"
    log_path = tmp_path / "gateway.log"
    client = None
    with log_path.open("w") as log:
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "gateway.py"), "--http",
             "--host", "127.0.0.1", "--port", str(public_port),
             "--internal-port", str(internal_port)],
            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            for port in (public_port, internal_port):
                deadline = time.monotonic() + 20
                while True:
                    try:
                        with urllib.request.urlopen(
                            f"http://127.0.0.1:{port}/health", timeout=0.5
                        ) as response:
                            assert response.status == 200
                        break
                    except OSError:
                        assert process.poll() is None, log_path.read_text()
                        assert time.monotonic() < deadline, log_path.read_text()
                        time.sleep(0.05)
            if unfinished_request:
                client = socket.create_connection(("127.0.0.1", public_port))
                client.sendall(
                    b"POST /v1/avatar/sessions HTTP/1.1\r\n"
                    b"Host: localhost\r\nContent-Type: application/json\r\n"
                    b"Content-Length: 1000000\r\n\r\n{"
                )
                # A completed health request on the same event loop ensures the
                # preceding request has reached the server before SIGTERM.
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{public_port}/health", timeout=2
                ):
                    pass
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                pytest.fail("Gateway did not stop within 15s:\n" + log_path.read_text())
            assert "Gateway stopped" in log_path.read_text()
            for port in (public_port, internal_port):
                with socket.socket() as probe:
                    assert probe.connect_ex(("127.0.0.1", port)) != 0
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            if client:
                client.close()
