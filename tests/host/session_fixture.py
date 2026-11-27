"""Real native session process and a bounded, faithful Unix GPU protocol peer."""
from collections import Counter, deque
import copy
import hashlib
import json
import os
from pathlib import Path
import queue
import select
import socket
import struct
import subprocess
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
HOST = Path(os.environ.get('DOODLE_HOST', ROOT / '.cache/rust-host/target/release/doodle-host'))
TEXTURE, SCENE = 0x4D520007, 0x4D520008
END, BIND, VIEW, CURSOR_IMAGE, CURSOR = range(0x444A0001, 0x444A0006)


def packet(stream, timeout=10):
    deadline = time.monotonic() + timeout
    def exact(size):
        result = bytearray()
        while len(result) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([stream], [], [], max(0, remaining))[0]:
                raise AssertionError('Native session did not produce its expected packet')
            chunk = os.read(stream.fileno(), size - len(result))
            if not chunk:
                raise EOFError('Native session exited')
            result.extend(chunk)
        return bytes(result)
    header = exact(8)
    size = struct.unpack_from('<I', header, 4)[0]
    if size > 64 << 20:
        raise AssertionError('Native session emitted an oversized packet')
    body = exact(size)
    return header[:4], json.loads(body) if header[:4] == b'EVT1' else body


class FakeGpu:
    def __init__(self, path, native=False):
        self.path, self.native_supported = path, native
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(str(path))
        self.listener.listen(2)
        self.listener.settimeout(.1)
        self.stop = threading.Event()
        self.lock, self.send_lock = threading.RLock(), threading.Lock()
        self.connection = None
        self.connection_count = 0
        self.binds, self.views, self.frames, self.uploads = [], [], [], []
        self.bind_failures = deque()
        self.cursor_images, self.cursors = [], []
        self.textures = {}
        self.pending = None
        self.hold_next = False
        self.error = None
        self.thread = threading.Thread(target=self._run, name='session-test-gpu', daemon=True)
        self.thread.start()

    def _send(self, value):
        with self.send_lock:
            connection = self.connection
            if connection is not None:
                connection.sendall(json.dumps(value).encode() + b'\n')

    def hold_frame(self):
        with self.lock:
            self.hold_next = True

    def release_frame(self):
        with self.lock:
            assert self.pending is not None, 'No GPU frame is waiting for its completion'
            reply, self.pending = self.pending, None
            if reply.get('mode') == 'native' and not self.visible:
                reply['presented'] = False
        self._send(reply)

    def disconnect(self):
        with self.lock:
            connection = self.connection
            if connection is not None:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()

    def _message(self, kind, body, frame):
        with self.lock:
            if kind == TEXTURE:
                assert len(body) >= 12
                texture, width, height = struct.unpack_from('<III', body)
                assert texture and (width == height == 0 or 0 < width <= 2048 and 0 < height <= 2048)
                assert len(body) == 12 + width * height * 4
                if width:
                    record = dict(id=texture, width=width, height=height, bytes=len(body)-12,
                                  sha256=hashlib.sha256(body[12:]).hexdigest())
                    self.textures[texture] = record
                    self.uploads.append(record)
                else:
                    self.textures.pop(texture, None)
            elif kind == SCENE:
                assert frame is None and self.pending is None, 'Only one scene may be outstanding'
                assert len(body) >= 8
                width, height = struct.unpack_from('<II', body)
                assert (width, height) in ((408, 720), (544, 960))
                offset = 8
                references = []
                while offset < len(body):
                    operation = struct.unpack_from('<I', body, offset)[0]
                    if operation == 1:
                        offset += 20
                    elif operation == 2:
                        assert offset + 28 <= len(body)
                        texture, flags, source, destination, replace, count = struct.unpack_from('<6I', body, offset + 4)
                        assert flags <= 15 and source in (0, 1, 770, 771) and destination in (0, 1, 768, 770, 771)
                        assert replace <= 2 and 0 < count <= 65536 and count % 3 == 0
                        if texture:
                            assert texture in self.textures, 'Scene references a missing worker texture'
                            references.append(texture)
                        offset += 28 + count * 32
                    else:
                        raise AssertionError('Unknown scene operation')
                    assert offset <= len(body)
                frame = dict(width=width, height=height, references=references)
            elif kind == END:
                assert not body and frame is not None
                self.frames.append(frame)
                sequence = self.sequence = 1 if self.sequence == 0xffffffff else self.sequence + 1
                reply = dict(sequence=sequence, width=frame['width'], height=frame['height'])
                if self.native:
                    reply.update(mode='native', presented=True, timestamp=sequence)
                else:
                    reply.update(mode='video', path='/dev/shm/mgu-video-doodle-session-fixture')
                if self.hold_next:
                    self.hold_next = False
                    self.pending = reply
                else:
                    self._send(reply)
                frame = None
            elif kind == BIND:
                assert frame is None and self.pending is None and len(body) >= 18 and body[-1] == 0
                rectangle = struct.unpack_from('<4i', body)
                endpoint = body[16:-1].decode()
                assert endpoint.startswith('/')
                self.binds.append(dict(rectangle=rectangle, path=endpoint, frames=len(self.frames)))
                fail = bool(self.bind_failures)
                reason = self.bind_failures.popleft() if fail else 'presenter_unavailable'
                self.native = self.native_supported and not fail
                reply = dict(event='bind', native=self.native)
                if not self.native and reason is not None:
                    reply['reason'] = reason
                self._send(reply)
            elif kind == VIEW:
                assert len(body) == 20
                self.views.append(struct.unpack('<5i', body))
                self.visible = bool(self.views[-1][4])
            elif kind == CURSOR_IMAGE:
                width, height = struct.unpack_from('<II', body)
                assert 1 <= width <= 512 and 1 <= height <= 512 and len(body) == 8 + width * height * 4
                self.cursor_images.append((width, height))
            elif kind == CURSOR:
                assert len(body) == 20
                self.cursors.append(struct.unpack('<4fI', body))
            else:
                raise AssertionError(f'Unknown GPU worker message {kind:x}')
        return frame

    def _run(self):
        while not self.stop.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with self.lock:
                self.connection = connection
                self.connection_count += 1
                self.sequence, self.native, self.visible, self.pending = 0, False, False, None
                self.textures = {}
            connection.settimeout(.1)
            buffer, frame = bytearray(), None
            try:
                while not self.stop.is_set():
                    try:
                        data = connection.recv(65536)
                    except socket.timeout:
                        continue
                    if not data:
                        break
                    buffer.extend(data)
                    while len(buffer) >= 8:
                        kind, size = struct.unpack_from('<II', buffer)
                        assert size <= 24 << 20, 'GPU peer message exceeds the native worker limit'
                        if len(buffer) < 8 + size:
                            break
                        body = bytes(buffer[8:8+size])
                        del buffer[:8+size]
                        frame = self._message(kind, body, frame)
            except (OSError, EOFError):
                pass
            except BaseException as error:
                self.error = error
            finally:
                connection.close()
                with self.lock:
                    if self.connection is connection:
                        self.connection = None

    def close(self):
        self.stop.set()
        self.disconnect()
        self.listener.close()
        self.thread.join(timeout=2)


class NativeSession:
    def __init__(self, gpu=False, native=False, auto_bind=True):
        self.temporary = tempfile.TemporaryDirectory(prefix='doodle-session-')
        self.directory = Path(self.temporary.name)
        data = self.directory / 'game'
        data.mkdir()
        (data / 'appdata.i3d').write_text('<DoodleJumpHScore value="10000" />\n<DoodleJumpActiveTheme value="1" />\n<DoodleJumpDefaultTheme value="1" />\n')
        self.gpu = FakeGpu(self.directory / 'gpu.sock', native) if gpu else None
        self.ui = None
        self.auto_bind = auto_bind
        self.ui_lock, self.send_lock = threading.RLock(), threading.Lock()
        self.heartbeat_stop, self.reader_stop = threading.Event(), threading.Event()
        self.closing = False
        self.queue = queue.Queue(maxsize=16)
        self.history = deque(maxlen=128)
        self.counts = Counter()
        self.stderr = (self.directory / 'stderr').open('w+b')
        environment = dict(os.environ, DOODLE_GPU_SOCKET=str(self.directory / ('gpu.sock' if gpu else 'missing.sock')))
        self.child = subprocess.Popen([str(HOST), '--session', '--original', str(ROOT / 'original'),
                                      '--runtime', str(ROOT / 'runtime/bundle'), '--data', str(data),
                                      '--no-audio', '--epoch', '1700000000'], env=environment,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr, bufsize=0)
        self.reader = threading.Thread(target=self._read, name='session-test-output', daemon=True)
        self.reader.start()
        self.heartbeat = threading.Thread(target=self._heartbeat, name='session-test-heartbeat', daemon=True)
        self.heartbeat.start()

    def __enter__(self):
        try:
            self.wait_event('ready')
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, *_):
        self.close()

    def _read(self):
        try:
            while not self.reader_stop.is_set():
                item = packet(self.child.stdout)
                while not self.reader_stop.is_set():
                    try:
                        self.queue.put(item, timeout=.05)
                        break
                    except queue.Full:
                        if self.closing:
                            break
        except BaseException as error:
            while not self.reader_stop.is_set():
                try:
                    self.queue.put(error, timeout=.05)
                    break
                except queue.Full:
                    if self.closing:
                        break

    def _heartbeat(self):
        while not self.heartbeat_stop.wait(.2):
            with self.ui_lock:
                if self.ui is not None:
                    try:
                        self.send(ui=copy.deepcopy(self.ui))
                    except (BrokenPipeError, OSError, ValueError):
                        return

    def send(self, **command):
        data = json.dumps(command, separators=(',', ':')).encode() + b'\n'
        assert len(data) <= 4096
        with self.send_lock:
            self.child.stdin.write(data)
            self.child.stdin.flush()

    def set_ui(self, **changes):
        with self.ui_lock:
            if self.ui is None:
                self.ui = dict(shown=True, focused=True, active=True, sheet=False, parkedOnly=False, showFps=True,
                               place=dict(x=0, y=0, w=1920, h=720, scale=1.164, ox=71, oy=-38, full=True),
                               renderer=dict(ready=bool(self.gpu), native=bool(self.gpu), video=bool(self.gpu),
                                             visible=True, onScreen=True, generation=1, source=1, socket=1,
                                             path='/tmp/session-renderer.sock'))
            for key, value in changes.items():
                if key in ('renderer', 'place'):
                    self.ui[key].update(value)
                else:
                    self.ui[key] = value
            self.send(ui=copy.deepcopy(self.ui))

    def binding_reply(self, request, stage, ok=True):
        self.send(binding=dict(id=request['id'], stage=stage, ok=ok))

    def next(self, timeout=10):
        try:
            item = self.queue.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError('Native session output timed out')
        if isinstance(item, BaseException):
            self.stderr.seek(0)
            raise AssertionError(f'{item}; exit={self.child.poll()}; stderr={self.stderr.read().decode(errors="replace")}')
        kind, body = item
        self.counts[kind] += 1
        if kind == b'GPF1':
            raise AssertionError('A native session sent its full GPU scene through stdout')
        if kind == b'EVT1':
            if body.get('type') == 'error':
                raise AssertionError('Native session error: ' + str(body.get('message')))
            self.history.append(body)
            if body.get('type') == 'binding' and self.auto_bind:
                if body['stage'] == 'prepare':
                    self.set_ui(renderer=dict(visible=False))
                    self.binding_reply(body, 'prepared')
                elif body['stage'] == 'restore':
                    self.set_ui(renderer=dict(visible=True))
                    self.binding_reply(body, 'restored')
        if self.gpu and self.gpu.error:
            raise AssertionError(f'GPU fixture rejected the native wire: {self.gpu.error}')
        return kind, body

    def wait_event(self, name, condition=lambda _: True, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            kind, body = self.next(max(.01, deadline-time.monotonic()))
            if kind == b'EVT1' and body.get('type') == name and condition(body):
                return body
        raise AssertionError(f'Native session never emitted {name}')

    def pump_until(self, condition, timeout=15):
        deadline = time.monotonic() + timeout
        while not condition():
            self.next(max(.01, deadline-time.monotonic()))
            if time.monotonic() >= deadline and not condition():
                raise AssertionError('Native session condition timed out')

    def pump_for(self, seconds):
        deadline = time.monotonic() + seconds
        result = []
        while time.monotonic() < deadline:
            try:
                result.append(self.next(min(.05, deadline-time.monotonic())))
            except AssertionError as error:
                if str(error) != 'Native session output timed out':
                    raise
        return result

    def close_input(self):
        self.heartbeat_stop.set()
        self.heartbeat.join(timeout=1)
        with self.send_lock:
            if not self.child.stdin.closed:
                self.child.stdin.close()

    def close(self):
        self.closing = True
        self.close_input()
        try:
            self.child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.child.kill()
            self.child.wait(timeout=5)
        self.reader_stop.set()
        self.child.stdout.close()
        self.reader.join(timeout=1)
        if self.gpu:
            self.gpu.close()
        self.stderr.close()
        self.temporary.cleanup()
