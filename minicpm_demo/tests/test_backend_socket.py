import errno
import socket

import pytest

from py_backend.server import _create_server_socket


def test_server_socket_reserves_port_before_model_startup():
    listener = _create_server_socket("127.0.0.1", 0)
    try:
        port = listener.getsockname()[1]
        competing = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(OSError) as exc_info:
                competing.bind(("127.0.0.1", port))
            assert exc_info.value.errno == errno.EADDRINUSE
        finally:
            competing.close()
    finally:
        listener.close()
