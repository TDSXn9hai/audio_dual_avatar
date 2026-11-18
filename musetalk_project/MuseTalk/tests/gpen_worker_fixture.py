"""CPU-only model substitute used with the real worker protocol."""
import argparse
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import socket
import struct
import time

import numpy as np

from muse_stream.inference.gpen_process import _send_message, _serve_worker

parser = argparse.ArgumentParser()
parser.add_argument('--fd', type=int, required=True)
parser.add_argument('--mode', default='normal')
parser.add_argument('--parent-pid', type=int, required=True)
args = parser.parse_args()
connection = socket.socket(fileno=args.fd)


class Model:
    def __init__(self, path, device):
        if args.mode == 'startup_slow':
            time.sleep(30)
        if args.mode == 'startup_error':
            raise RuntimeError('fixture startup failed')

    def infer_aligned(self, image):
        if args.mode == 'error':
            raise RuntimeError('fixture model failed')
        if args.mode == 'exit':
            os._exit(7)
        if args.mode == 'hang':
            time.sleep(30)
        if args.mode == 'slow':
            time.sleep(.5)
        return np.minimum(image.astype(np.uint16) + 1, 255).astype(np.uint8)

    def status(self):
        return {'device': 'cpu', 'operators': 'native', 'pid': os.getpid()}


if args.mode in ('no_read', 'partial'):
    _send_message(connection, {'type': 'ready', 'status': Model('', '').status()}, time.monotonic() + 5)
    if args.mode == 'partial':
        # Read request before stopping halfway through a deliberately incomplete reply.
        from muse_stream.inference.gpen_process import _receive_message
        _receive_message(connection, time.monotonic() + 5)
        connection.sendall(struct.pack('!Q', 1000000) + b'partial')
    time.sleep(30)
else:
    _serve_worker(connection, '', 'cpu', Model, args.parent_pid)
