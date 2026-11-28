"""Native presentation through a stock IVI layer, with Mesa and a mock compositor."""
import json
import os
from pathlib import Path
import queue
import socket
import statistics
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from test_gpu_worker import clear, draw, packet, scene, TEXTURE, END

BIND, VIEW, CURSOR_IMAGE, CURSOR_POSITION = range(0x444A0002, 0x444A0006)
CHALLENGE = (-9373, -18421, 1920, 720)
BINARY = MOCK = None
if __name__ == '__main__' and len(sys.argv) > 2 and not sys.argv[1].startswith('-'):
    BINARY, MOCK = (Path(sys.argv.pop(1)).resolve() for _ in range(2))


class PresenterHarness:
    """Run a real worker against a compositor that owns the existing game layer."""

    def __init__(self, binary, mock, capture=True, activated=False):
        self.directory = tempfile.TemporaryDirectory(prefix='doodle-present-')
        self.root = Path(self.directory.name)
        self.renderer = self.root / 'renderer.sock'
        self.endpoint = self.root / 'worker.sock'
        self.capture = self.root / 'frame.pam' if capture else Path('/dev/null')
        self.lines = []
        self.events = queue.Queue()
        self.reader = self.connection = self.worker = self.compositor = None
        self.env = dict(os.environ, XDG_RUNTIME_DIR=str(self.root), WAYLAND_DISPLAY='wayland-doodle',
                        EGL_PLATFORM='wayland', LIBGL_ALWAYS_SOFTWARE='1')
        listener = None
        if activated:
            listener = socket.socket(socket.AF_UNIX)
            listener.bind(str(self.renderer))
            listener.listen(4)
            self.env['TEST_RENDERER_FD'] = str(listener.fileno())
        self.compositor = subprocess.Popen([str(mock), self.env['WAYLAND_DISPLAY'],
                                            str(self.renderer), str(self.capture)], env=self.env,
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, text=True, bufsize=1,
                                           pass_fds=(listener.fileno(),) if listener else ())
        if listener:
            listener.close()
        self.log_thread = threading.Thread(target=self._read_log, daemon=True)
        self.log_thread.start()
        self.wait_log(lambda line: line.startswith('ready '))
        self.worker = subprocess.Popen([str(binary), '--socket', str(self.endpoint)], env=self.env,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 10
        while not self.endpoint.exists() and self.worker.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        if self.worker.poll() is not None:
            raise RuntimeError(self.worker.communicate()[1].decode())
        self.connect()

    def _read_log(self):
        for line in self.compositor.stdout:
            self.lines.append(line.rstrip())
            self.events.put(line.rstrip())

    def wait_log(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.events.get(timeout=max(.001, deadline - time.monotonic()))
            if predicate(line):
                return line
        raise TimeoutError('compositor event missing')

    def command(self, text):
        self.compositor.stdin.write(text + '\n')
        self.compositor.stdin.flush()
        self.wait_log(lambda line: line == 'command ' + text)

    def connect(self):
        if self.reader:
            self.reader.close()
            self.connection.close()
        self.connection = socket.socket(socket.AF_UNIX)
        self.connection.settimeout(5)
        deadline = time.monotonic() + 5
        while True:
            try:
                self.connection.connect(str(self.endpoint))
                break
            except ConnectionRefusedError:
                if self.worker.poll() is not None or time.monotonic() >= deadline:
                    raise
                time.sleep(.01)
        self.reader = self.connection.makefile('rb')

    def send_bind(self, challenge=CHALLENGE):
        self.connection.sendall(packet(BIND, struct.pack('<4i', *challenge) +
                                       os.fsencode(self.renderer) + b'\0'))

    def receive(self):
        line = self.reader.readline()
        if not line:
            raise EOFError('worker disconnected')
        return json.loads(line)

    def bind(self, challenge=CHALLENGE):
        self.send_bind(challenge)
        return self.receive()

    def view(self, show=True, width=408, height=720):
        self.connection.sendall(packet(VIEW, struct.pack('<5i', 756, 0, width, height, show)))

    def frame(self, commands, before=b'', after=b''):
        self.connection.sendall(before + scene(commands) + packet(END) + after)
        return self.receive()

    def snapshot(self):
        header, data = self.capture.read_bytes().split(b'ENDHDR\n', 1)
        fields = dict(line.split() for line in header.splitlines()[1:] if len(line.split()) == 2)
        width, height = int(fields[b'WIDTH']), int(fields[b'HEIGHT'])
        if len(data) != width * height * 4:
            raise ValueError('incomplete compositor snapshot')
        return width, height, data

    def close(self):
        if self.reader:
            self.reader.close()
            self.connection.close()
        errors = []
        for process in (self.worker, self.compositor):
            if process:
                process.terminate()
                _, stderr = process.communicate(timeout=10)
                if process.returncode:
                    errors.append((process.returncode, stderr))
        self.log_thread.join(timeout=2)
        self.directory.cleanup()
        if errors:
            raise AssertionError(errors)


@unittest.skipUnless(BINARY and MOCK, 'pass built worker and mock compositor paths')
class PresenterTests(unittest.TestCase):
    def test_socket_activation_binds_the_process_holding_the_listener(self):
        self.h.close()
        self.h = PresenterHarness(BINARY, MOCK, activated=True)
        self.assertTrue(self.h.bind()['native'])
        self.h.command('pid 999999')
        self.assertFalse(self.h.bind()['native'])

    def setUp(self):
        self.h = PresenterHarness(BINARY, MOCK)

    def tearDown(self):
        self.h.close()
        self.assertFalse([line for line in self.h.lines if 'VIOLATION' in line], self.h.lines)

    def native(self):
        self.assertEqual(self.h.bind(), {'event': 'bind', 'native': True})
        self.h.command('rect 0 0 1920 720')
        self.h.view()

    def assert_pixel(self, x, y, expected):
        width, _, data = self.h.snapshot()
        actual = tuple(data[(y * width + x) * 4:(y * width + x) * 4 + 4])
        self.assertTrue(all(abs(a - b) <= 1 for a, b in zip(actual, expected)), (actual, expected))

    def test_rgba_orientation_and_shared_layer_preserve_existing_surface(self):
        self.native()
        colors = ((1, 0, 0, 1), (0, 1, 0, 1), (0, 0, 1, 1), (1, 1, 1, 1))
        commands = clear((0, 0, 0, 1)) + b''.join(
            draw(color, i % 2 * 272, i // 2 * 480, 272, 480) for i, color in enumerate(colors))
        reply = self.h.frame(commands)
        self.assertEqual(reply['mode'], 'native')
        self.assertTrue(reply['presented'])
        self.assertGreater(reply['timestamp'], 0)
        self.assertNotIn('path', reply)
        self.assertEqual(self.h.snapshot()[:2], (408, 720))
        for point in ((40, 60, (255, 0, 0, 255)), (350, 60, (0, 255, 0, 255)),
                      (40, 650, (0, 0, 255, 255)), (350, 650, (255, 255, 255, 255))):
            self.assert_pixel(*point)
        self.assertTrue(any(line.startswith('members 1097 4007 ') for line in self.h.lines))
        self.assertTrue(any(line.startswith('input-empty ') for line in self.h.lines))

    def test_cursor_uses_premultiplied_alpha_and_game_coordinates(self):
        self.native()
        cursor = packet(CURSOR_IMAGE, struct.pack('<2I', 2, 2) + bytes((128, 0, 0, 128)) * 4)
        position = packet(CURSOR_POSITION, struct.pack('<4fI', 272, 480, 0, 0, 1))
        self.h.frame(clear((0, 0, 1, 1)), before=cursor + position)
        self.assert_pixel(205, 361, (128, 0, 127, 255))
        self.assert_pixel(240, 361, (0, 0, 255, 255))

    def test_frame_ack_is_paced_and_hide_cancels_pending_callback(self):
        self.native()
        stamps = []
        started = time.monotonic()
        for _ in range(8):
            reply = self.h.frame(clear((1, 1, 1, 1)))
            self.assertTrue(reply['presented'])
            stamps.append(reply['timestamp'])
        self.assertGreaterEqual(time.monotonic() - started, .1)
        self.assertTrue(all(12 <= b - a <= 1000 for a, b in zip(stamps, stamps[1:])), stamps)
        hide = packet(VIEW, struct.pack('<5i', 756, 0, 408, 720, 0))
        cancelled = self.h.frame(clear((1, 0, 0, 1)), after=hide)
        self.assertFalse(cancelled['presented'])
        self.h.view()
        self.assertTrue(self.h.frame(clear((0, 1, 0, 1)))['presented'])

    def test_callbacks_follow_display_cadence_after_client_draw_time(self):
        self.h.close()
        self.h = PresenterHarness(BINARY, MOCK, capture=False)
        self.native()
        stamps = []
        for _ in range(24):
            # Time spent producing a game frame must fit inside the refresh period.
            time.sleep(.007)
            stamps.append(self.h.frame(clear((0, 0, 1, 1)))['timestamp'])
        intervals = [b - a for a, b in zip(stamps, stamps[1:])]
        self.assertLess(statistics.median(intervals), 22, intervals)

    def test_hidden_parent_skips_draw_and_destroy_never_creates_new_layer(self):
        self.native()
        self.h.frame(clear((0, 0, 1, 1)))
        before = self.h.capture.read_bytes()
        self.h.command('visible 0')
        time.sleep(.025)
        self.assertFalse(self.h.frame(clear((1, 0, 0, 1)))['presented'])
        self.assertEqual(self.h.capture.read_bytes(), before)
        self.h.command('visible 1')
        time.sleep(.025)
        self.assertTrue(self.h.frame(clear((0, 1, 0, 1)))['presented'])
        self.h.command('replace')
        self.assertEqual(self.h.bind(), {'event': 'bind', 'native': True})
        self.h.view()
        self.assertTrue(self.h.frame(clear((1, 0, 0, 1)))['presented'])
        self.assertTrue(any(line.startswith('members 1098 ') for line in self.h.lines), self.h.lines)

    def test_delayed_marker_is_waited_for_across_separate_socket_writes(self):
        self.h.command('rect 0 0 1920 720')
        self.h.send_bind()
        time.sleep(.1)
        self.h.command('rect ' + ' '.join(map(str, CHALLENGE)))
        self.assertEqual(self.h.receive(), {'event': 'bind', 'native': True})

    def test_challenge_and_server_pid_must_both_match_and_be_unique(self):
        started = time.monotonic()
        self.assertEqual(self.h.bind((0, 0, 1920, 720)),
                         {'event': 'bind', 'native': False, 'reason': 'layer_not_ready'})
        self.assertLess(time.monotonic() - started, 2)
        self.h.command('pid 1')
        self.assertFalse(self.h.bind()['native'])
        self.h.command('pid ' + str(self.h.compositor.pid))
        self.h.command('ambiguous')
        self.assertEqual(self.h.bind(), {'event': 'bind', 'native': False, 'reason': 'bind_failed'})
        self.assertFalse(any(line.startswith('commit ') for line in self.h.lines))

    def test_occupied_native_surface_id_preserves_the_existing_surface(self):
        reserved = 0x444A0000 | (self.h.worker.pid & 0xFFFF)
        self.h.command('reserve ' + str(reserved))
        self.native()
        self.h.frame(clear((0, 0, 1, 1)))
        expected = f'members 1097 4007 {reserved} {reserved + 1}'
        self.assertIn(expected, self.h.lines)

    @unittest.skipIf(os.environ.get('DOODLE_NO_READBACK') or os.environ.get('LD_PRELOAD'),
                     'VIDEO fallback requires readback')
    def test_native_video_native_handoff_preserves_sequence_and_releases_video_file(self):
        self.native()
        first = self.h.frame(clear((1, 0, 0, 1)))
        self.assertEqual((first['mode'], first['sequence']), ('native', 1))
        # The parent has its normal rectangle again, so the old challenge fails.
        self.assertFalse(self.h.bind()['native'])
        fallback = self.h.frame(clear((0, 1, 0, 1)))
        self.assertEqual(fallback['sequence'], 2)
        path = Path(fallback['path'])
        data = path.read_bytes()
        header = struct.unpack_from('<16I', data)
        self.assertEqual(header[:5], (0x4656474D, 1, 408, 720, 2))
        self.assertEqual(len(data), 64 + 3 * 408 * 720 * 3 // 2)
        slot = 64 + header[5] * (408 * 720 * 3 // 2)
        self.assertAlmostEqual(data[slot + 360 * 408 + 204], 150, delta=1)
        self.h.command('rect ' + ' '.join(map(str, CHALLENGE)))
        self.assertTrue(self.h.bind()['native'])
        self.h.view()
        final = self.h.frame(clear((0, 0, 1, 1)))
        self.assertEqual((final['mode'], final['sequence']), ('native', 3))
        self.assert_pixel(204, 360, (0, 0, 255, 255))
        self.assertFalse(path.exists())

    def test_repeated_connections_and_rebind_keep_first_textured_frame_valid(self):
        for i, color in enumerate(((255, 0, 0, 255), (0, 255, 0, 255), (0, 0, 255, 255))):
            if i:
                self.h.connect()
            self.assertTrue(self.h.bind()['native'])
            self.h.view()
            upload = packet(TEXTURE, struct.pack('<3I', 7, 2, 2) + bytes(color) * 4)
            commands = clear((0, 0, 0, 1)) + draw((1, 1, 1, 1), 0, 0, 544, 960, texture=7)
            reply = self.h.frame(commands, before=upload)
            self.assertEqual(reply['sequence'], 1)
            self.assert_pixel(204, 360, color)
            self.assertTrue(self.h.bind()['native'])
            self.h.view()
            self.assertTrue(self.h.frame(commands)['presented'])
            self.assert_pixel(204, 360, color)


if __name__ == '__main__':
    unittest.main()
