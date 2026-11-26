import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
CHECK = ROOT / 'native/doodle-gpu/gpu_check.js'


class GpuCheckTests(unittest.TestCase):
    def probe(self, chunks, close=True):
        with tempfile.TemporaryDirectory(prefix='dj-gpu-', dir='/tmp') as directory:
            endpoint = str(Path(directory) / 'render.sock')
            server = socket.socket(socket.AF_UNIX)
            server.bind(endpoint)
            server.listen(1)
            server.settimeout(2)
            received = bytearray()

            def respond():
                try:
                    connection, _ = server.accept()
                    with connection:
                        connection.settimeout(2)
                        while len(received) < 44:
                            data = connection.recv(4096)
                            if not data:
                                return
                            received.extend(data)
                        for chunk in chunks:
                            connection.sendall(chunk)
                        if not close:
                            connection.recv(4096)
                except (BrokenPipeError, ConnectionResetError, TimeoutError, socket.timeout):
                    pass

            thread = threading.Thread(target=respond)
            thread.start()
            env = dict(os.environ, DOODLE_GPU_CHECK_SOCKET=endpoint, DOODLE_GPU_CHECK_TIMEOUT_MS='120')
            result = subprocess.run(['node', str(CHECK)], env=env, capture_output=True, text=True, timeout=3)
            thread.join(3)
            server.close()
            return result, bytes(received)

    def test_fragmented_frame_answer_passes_without_showing_a_surface(self):
        data = json.dumps(dict(path='/dev/shm/mgu-video-doodle-test', width=408, height=720, sequence=1)).encode()+b'\n'
        result, sent = self.probe([data[:3], data[3:19], data[19:]])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(struct.unpack_from('<II', sent), (0x4d520008, 28))
        self.assertEqual(struct.unpack_from('<II', sent, 36), (0x444a0001, 0))

    def test_invalid_frame_truncated_answer_and_silence_fail(self):
        cases = [([b'{}\n'], True, 'invalid'),
                 ([b'not json\n'], True, 'GPU worker'),
                 ([b'{'], True, 'closed'),
                 ([b'x'*4097], False, 'large'),
                 ([], False, 'timeout')]
        for chunks, close, reason in cases:
            with self.subTest(chunks=chunks, close=close):
                result, _ = self.probe(chunks, close)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(reason, result.stderr)


class GpuPackageTests(unittest.TestCase):
    def module(self):
        try:
            return importlib.import_module('build_gpu_renderer')
        except ModuleNotFoundError:
            self.fail('GPU renderer packaging is missing')

    def test_package_owns_only_worker_units_and_keeps_sources_and_licenses(self):
        build = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            component = root/'native/doodle-gpu'
            shutil.copytree(ROOT/'native/doodle-gpu', component, ignore=shutil.ignore_patterns('build'))
            (component/'build/target').mkdir(parents=True)
            binary = b'\x7fELF' + b'fixture worker'
            (component/'build/target/doodle-gpu').write_bytes(binary)
            with patch.object(build, 'ROOT', root):
                artifact, sources = build.package()
                first = artifact.read_bytes()
                build.package()
                self.assertEqual(first, artifact.read_bytes(), 'packages are reproducible')
            with tarfile.open(artifact) as archive:
                manifest = json.load(archive.extractfile('component.json'))
                self.assertEqual(manifest['id'], 'doodle-gpu')
                self.assertEqual(manifest.get('dropins', []), [])
                self.assertEqual(manifest['restart'], ['doodle-gpu.service'])
                self.assertEqual({entry['name'] for entry in manifest['units']}, {'doodle-gpu.service', 'doodle-gpu.socket'})
                for entry in manifest['files']:
                    self.assertEqual(hashlib.sha256(archive.extractfile('files/'+entry['path']).read()).hexdigest(), entry['sha256'])
                self.assertEqual(archive.extractfile('files/doodle-gpu').read(), binary)
                service = archive.extractfile('units/doodle-gpu.service').read().decode()
                self.assertIn('/var/opt/doodle-gpu/current/doodle-gpu', service)
                self.assertNotIn('/var/opt/mgu-render/', service)
                self.assertNotIn('mgu-render.service', service)
                socket_unit = archive.extractfile('units/doodle-gpu.socket').read().decode()
                self.assertIn('/run/doodle-gpu/render.sock', socket_unit)
                self.assertIn('SocketGroup=tm_oap_app', socket_unit)
                self.assertIn('files/LICENSE', archive.getnames())
                self.assertIn('files/LICENSE.mgu-renderd', archive.getnames())
            with tarfile.open(sources) as archive:
                names=archive.getnames()
                for source in ('scene.c', 'scene.h', 'worker.c', 'video.c', 'video.h', 'Makefile', 'LICENSE'):
                    self.assertTrue(any(name.endswith('/'+source) for name in names), source)
                self.assertFalse(any('/build/' in name for name in names))


if __name__ == '__main__':
    unittest.main()
