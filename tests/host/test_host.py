"""Run the native host against the pinned Python release, not a second host implementation."""
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
HOST = Path(os.environ.get('DOODLE_HOST', ROOT / '.cache/rust-host/target/release/doodle-host'))
import sys
sys.path.insert(0, str(ROOT / 'tools'))
from prepare_reference import activate
REFERENCE = activate(current=False)


def packet(stream):
    header = stream.read(8)
    if len(header) != 8:
        raise AssertionError('Native host stopped before completing a frame')
    size = struct.unpack_from('<I', header, 4)[0]
    if size > 64 << 20:
        raise AssertionError('Native host emitted an invalid packet size')
    data = stream.read(size)
    if len(data) != size:
        raise AssertionError('Native host truncated a packet')
    return header[:4], data


class NativeHostTests(unittest.TestCase):
    def test_native_frames_match_the_pinned_release(self):
        self.assertTrue(HOST.is_file(), 'The standalone Rust host is not built')
        from game import Game
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as left, tempfile.TemporaryDirectory() as right:
            with patch('libc.time.time', return_value=1700000000):
                reference = Game(REFERENCE / 'original', Path(left), render_size=(408, 720), backend='aot')
            with subprocess.Popen([str(HOST), '--original', str(REFERENCE / 'original'), '--data', right,
                                   '--no-audio', '--epoch', '1700000000'], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE) as child:
                try:
                    while True:
                        kind, body = packet(child.stdout)
                        if kind == b'EVT1' and json.loads(body).get('type') == 'ready':
                            break
                    for i in range(8):
                        command = dict(frame=True, gpu=True, gpu_reset=i == 0, dt=1/60)
                        child.stdin.write(json.dumps(command).encode() + b'\n')
                        child.stdin.flush()
                        reference.graphics.begin_gpu_frame(True, reset=i == 0)
                        reference.step(readback=False)
                        while True:
                            kind, body = packet(child.stdout)
                            if kind == b'EVT1':
                                self.assertNotEqual(json.loads(body).get('type'), 'error', body)
                            if kind == b'GPF1':
                                self.assertEqual(body, reference.graphics.gpu_frame(), f'frame {i}')
                                break
                finally:
                    child.stdin.close()
                    child.wait(timeout=10)
                self.assertEqual(child.returncode, 0)


if __name__ == '__main__':
    unittest.main()
