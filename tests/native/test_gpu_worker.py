"""Exercise the GPU worker through its Unix socket and standard I420 frame file."""
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest

BINARY = Path(os.environ['DOODLE_GPU_WORKER']).resolve() if 'DOODLE_GPU_WORKER' in os.environ else None
if __name__ == '__main__' and len(sys.argv) > 1 and not sys.argv[1].startswith('-'):
    BINARY = Path(sys.argv.pop(1)).resolve()
TEXTURE = 0x4D520007
SCENE = 0x4D520008
END = 0x444A0001


def packet(kind, data=b''):
    return struct.pack('<II', kind, len(data)) + data


def draw(color, x, y, width, height, texture=0):
    corners = ((x, y, 0, 0), (x + width, y, 1, 0), (x, y + height, 0, 1),
               (x, y + height, 0, 1), (x + width, y, 1, 0), (x + width, y + height, 1, 1))
    return struct.pack('<7I', 2, texture, 0, 1, 0, 0, len(corners)) + b''.join(
        struct.pack('<8f', *point, *color) for point in corners)


def scene(commands):
    return packet(SCENE, struct.pack('<II', 544, 960) + commands)


def clear(color):
    return struct.pack('<I4f', 1, *color)


@unittest.skipUnless(BINARY, 'pass the built worker path or set DOODLE_GPU_WORKER')
class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='doodle-gpu-')
        self.endpoint = Path(self.directory.name) / 'worker.sock'
        self.process = subprocess.Popen([str(BINARY), '--socket', str(self.endpoint)],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 10
        while not self.endpoint.exists() and self.process.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertIsNone(self.process.poll(), self.process.communicate() if self.process.poll() is not None else '')
        self.connection = socket.socket(socket.AF_UNIX)
        self.connection.settimeout(10)
        self.connection.connect(str(self.endpoint))
        self.reader = self.connection.makefile('rb')
        self.files = set()

    def tearDown(self):
        self.reader.close()
        self.connection.close()
        self.process.terminate()
        _, stderr = self.process.communicate(timeout=10)
        self.assertEqual(self.process.returncode, 0, stderr.decode())
        self.assertFalse(self.endpoint.exists())
        for path in self.files:
            self.assertFalse(path.exists(), 'disconnect removes the frame file')
        self.directory.cleanup()

    def frame(self, commands, before=b'', after=b'', fragmented=False):
        data = before + scene(commands) + after + packet(END)
        if fragmented:
            for offset in range(0, len(data), 7):
                self.connection.sendall(data[offset:offset+7])
        else:
            self.connection.sendall(data)
        reply = json.loads(self.reader.readline())
        self.assertEqual((reply['width'], reply['height']), (408, 720))
        path = Path(reply['path'])
        self.assertTrue(path.name.startswith('mgu-video-doodle-'))
        self.files.add(path)
        data = path.read_bytes()
        header = struct.unpack_from('<16I', data)
        self.assertEqual(header[:4], (0x4656474D, 1, 408, 720))
        self.assertEqual(header[4], reply['sequence'])
        self.assertEqual(header[6:10], (3, 0, 1, 0))
        self.assertEqual(header[10:], (0,) * 6)
        size = 408 * 720 * 3 // 2
        self.assertEqual(len(data), 64 + 3 * size)
        at = 64 + header[5] * size
        return reply, data[at:at+size]

    def rgb(self, data, x, y):
        width, height = 408, 720
        y_size = width * height
        uv_size = y_size // 4
        yy = data[y * width + x]
        index = y // 2 * (width // 2) + x // 2
        u = data[y_size + index] - 128
        v = data[y_size + uv_size + index] - 128
        return tuple(round(max(0, min(255, value))) for value in
                     (yy + 1.402 * v, yy - .344136 * u - .714136 * v, yy + 1.772 * u))

    def assertColor(self, data, x, y, color):
        actual = self.rgb(data, x, y)
        self.assertTrue(all(abs(a-b) <= 3 for a, b in zip(actual, color)), (actual, color))

    def test_fragmented_frames_keep_orientation_color_and_sequence(self):
        colors = ((1, 0, 0, 1), (0, 1, 0, 1), (0, 0, 1, 1), (1, 1, 1, 1))
        commands = clear((0, 0, 0, 1)) + b''.join(
            draw(color, index % 2 * 272, index // 2 * 480, 272, 480)
            for index, color in enumerate(colors))
        reply, data = self.frame(commands, fragmented=True)
        self.assertEqual(reply['sequence'], 1)
        for x, y, color in ((40, 60, (255, 0, 0)), (350, 60, (0, 255, 0)),
                            (40, 650, (0, 0, 255)), (350, 650, (255, 255, 255))):
            self.assertColor(data, x, y, color)
        newer, data = self.frame(clear((.5, .5, .5, 1)))
        self.assertEqual(newer['path'], reply['path'])
        self.assertEqual(newer['sequence'], 2)
        self.assertColor(data, 204, 360, (128, 128, 128))

    def test_sequence_wrap_keeps_zero_reserved_for_an_unwritten_frame(self):
        reply, _ = self.frame(clear((0, 0, 0, 1)))
        with Path(reply['path']).open('r+b') as frame:
            frame.seek(16)
            frame.write(struct.pack('<I', 0xFFFFFFFF))
        wrapped, data = self.frame(clear((1, 1, 1, 1)))
        self.assertEqual(wrapped['sequence'], 1)
        self.assertColor(data, 204, 360, (255, 255, 255))

    def test_texture_retirement_after_scene_preserves_finished_frame(self):
        upload = packet(TEXTURE, struct.pack('<III', 1, 2, 2) + bytes((255, 0, 0, 255)) * 4)
        deletion = packet(TEXTURE, struct.pack('<III', 1, 0, 0))
        commands = clear((0, 0, 0, 1)) + draw((1, 1, 1, 1), 0, 0, 544, 960, texture=1)
        _, data = self.frame(commands, before=upload, after=deletion)
        self.assertColor(data, 204, 360, (255, 0, 0))
        _, data = self.frame(commands)
        self.assertColor(data, 204, 360, (0, 0, 0))

    def test_systemd_socket_activation_validates_and_serves_inherited_listener(self):
        endpoint = Path(self.directory.name) / 'activated.sock'
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(endpoint))
        listener.listen(1)
        program = """
import os, sys
os.dup2(int(sys.argv[2]), 3, inheritable=True)
env = dict(os.environ, LISTEN_PID=str(os.getpid()), LISTEN_FDS='1')
os.execve(sys.argv[1], [sys.argv[1]], env)
"""
        worker = subprocess.Popen([sys.executable, '-c', program, str(BINARY), str(listener.fileno())],
                                  pass_fds=(listener.fileno(),), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            with socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(10)
                connection.connect(str(endpoint))
                connection.sendall(scene(clear((0, 0, 1, 1))) + packet(END))
                with connection.makefile('rb') as reader:
                    reply = json.loads(reader.readline())
                self.assertEqual(reply['sequence'], 1)
                self.files.add(Path(reply['path']))
        finally:
            worker.terminate()
            _, stderr = worker.communicate(timeout=10)
            self.assertEqual(worker.returncode, 0, stderr.decode())
            listener.close()
        self.assertTrue(endpoint.exists(), 'systemd owns the activated socket path')
        endpoint.unlink()
        invalid = subprocess.run([str(BINARY)], env=dict(os.environ, LISTEN_PID='0', LISTEN_FDS='1'),
                                 capture_output=True, timeout=5)
        self.assertEqual(invalid.returncode, 1)

    def test_hard_restart_reclaims_frame_files_owned_by_the_dead_worker(self):
        reply, _ = self.frame(clear((1, 0, 0, 1)))
        abandoned = Path(reply['path'])
        self.process.kill()
        self.process.communicate(timeout=10)
        self.assertTrue(abandoned.exists())
        self.reader.close()
        self.connection.close()
        self.endpoint.unlink()
        self.process = subprocess.Popen([str(BINARY), '--socket', str(self.endpoint)],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 10
        while not self.endpoint.exists() and self.process.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        self.connection = socket.socket(socket.AF_UNIX)
        self.connection.settimeout(10)
        self.connection.connect(str(self.endpoint))
        self.reader = self.connection.makefile('rb')
        _, data = self.frame(clear((0, 1, 0, 1)))
        self.assertColor(data, 204, 360, (0, 255, 0))
        self.assertFalse(abandoned.exists())

    def test_invalid_client_does_not_prevent_reconnection(self):
        self.connection.sendall(struct.pack('<II', SCENE, 0xFFFFFFFF))
        self.assertEqual(self.reader.readline(), b'')
        self.reader.close()
        self.connection.close()
        self.connection = socket.socket(socket.AF_UNIX)
        self.connection.settimeout(10)
        self.connection.connect(str(self.endpoint))
        self.reader = self.connection.makefile('rb')
        _, data = self.frame(clear((0, 1, 0, 1)))
        self.assertColor(data, 204, 360, (0, 255, 0))


if __name__ == '__main__':
    if BINARY is None:
        raise SystemExit('usage: test_gpu_worker.py PATH_TO_WORKER')
    unittest.main()
